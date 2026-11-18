"""Страница «Финансы → Платежи» дашборда: разнос входящих платежей по сделкам Synergy (решение владельца 18.11.2026).

Зачем. В самом Synergy нельзя привязать платёж к сделке из списка платежей, не открывая платёж, а в карточке сделки
платёж не привязывается вовсе. Платежей до 100 в день, сотрудники этим раньше не занимались. Страница повторяет вид
раздела платежей Synergy и даёт кнопку «Привязать» прямо в строке: выбрать сделку, при необходимости разбить сумму
на несколько сделок, подтвердить.

Как устроено.
  * Список платежей читается из локальной копии (таблица `pay_cache`), которая обновляется из Synergy по `updated-at`:
    живой запрос на каждое открытие был бы медленным и съедал общий лимит Synergy.
  * Разноска: если платёж делится на несколько сделок, первая часть остаётся в исходном платеже, остальные создаются как
    новые платежи (так делал владелец вручную с CКБП-003393). Сначала создаются новые части, и только потом правится исходный
    платёж — при сбое исходный остаётся нетронутым, созданное откатывается. Создание — `post_once`, без автоповтора;
    неопределённый ответ (500, таймаут) проверяется по уникальной метке разноски в описании.
  * После разноски сразу пересчитывается «Сумма платежей» (custom-30778) затронутых сделок; таймер делает то же раз в 10 минут.
  * Каждое действие пишется в `pay_allocations` (кто, когда, куда, сколько): оттуда же работает «Отменить разноску».
Только оплаты клиентов: возвраты, займы и внутренние переводы сюда не попадают (их не грузим в Synergy).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.collector import SynergyClient
from app.config import Settings
from app.db import connect

logger = logging.getLogger(__name__)
router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DATA = Path(__file__).resolve().parents[1] / "data" / "payments"
FIELD_PAID = "custom-30778"      # «Сумма платежей» сделки
FIELD_DEAL_SUM = "custom-11983"  # «Сумма З» — сумма сделки для заказчика
FIELD_DEAL_INN = "custom-16356"  # «ИНН З»
FIELD_RATE = "custom-83"         # «Ставка З»
FIELD_ADDRESS = "custom-77"      # «Адрес объекта»
FIELD_START = "custom-11912"     # «Начало работ»
FIELD_END = "custom-11913"       # «Окончание работ»
RECENT_FROM = "2026-11-01"       # платежи без документа 1С показываем в «Ждут разноски» только с этой даты
STALE_SEC = 45
_lock = threading.Lock()

# Копия ответов Synergy для карточки платежа/счёта (сделки и счета клиента, компании по ИНН). Раньше каждое открытие карточки
# ходило в Synergy заново (1–1,5 с); теперь отдаём готовое, а фоновый поток держит копию свежей (_warm).
_RAW: dict[tuple, tuple[float, Any]] = {}
RAW_TTL = 900     # сек: столько отдаём копию без обращения в Synergy
WARM_AGE = 240    # сек: фон обновляет копию старше этого возраста
_raw_lock = threading.Lock()


def _cached(key: tuple, fn, ttl: int = RAW_TTL, force: bool = False):
    hit = _RAW.get(key)
    if hit and not force and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    with _raw_lock:
        _RAW[key] = (time.time(), val)
    return val


def _raw_forget() -> None:
    """После записи в Synergy (разноска, смена сделки) копии сделок и счетов устарели — сбрасываем."""
    with _raw_lock:
        for k in [k for k in _RAW if k[0] in ("cdeals", "cinv")]:
            _RAW.pop(k, None)


def _companies_by_inn(cl: SynergyClient, inn: str, force: bool = False) -> list[dict]:
    return _cached(("inn", inn), lambda: cl.get("companies", per_page=20, **{"filter[inn]": inn}).get("data") or [], 3600, force)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pay_cache (
    id TEXT PRIMARY KEY, number INTEGER, op_at TEXT, created_at TEXT, updated_at TEXT, amount REAL, direction TEXT, status TEXT,
    purpose TEXT, description TEXT, doc1c TEXT, deal_id TEXT, deal_number INTEGER, deal_name TEXT, deal_stage TEXT, deal_amount REAL,
    payer_id TEXT, payer_name TEXT, payer_inn TEXT, receiver_name TEXT, receiver_bank TEXT);
CREATE TABLE IF NOT EXISTS pay_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS pay_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, grp TEXT NOT NULL, ts TEXT NOT NULL, user TEXT, action TEXT NOT NULL, payment_id TEXT NOT NULL,
    deal_id TEXT, deal_number INTEGER, amount REAL, orig_amount REAL, new_payment_id TEXT, note TEXT, undone INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pay_flags (payment_id TEXT PRIMARY KEY, note TEXT, user TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS inv_cache (
    id TEXT PRIMARY KEY, number TEXT, issued TEXT, amount REAL, balance REAL, status TEXT, payer_id TEXT, payer_name TEXT, payer_inn TEXT,
    deal_id TEXT, deal_number INTEGER, deal_name TEXT, manager TEXT, purpose TEXT, reason TEXT, org TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS inv_manager (synergy_id TEXT PRIMARY KEY, doc_number TEXT, fio TEXT NOT NULL, how TEXT, comment TEXT);
CREATE TABLE IF NOT EXISTS pay_deals (id TEXT PRIMARY KEY, data TEXT NOT NULL, fetched REAL NOT NULL);
"""


PAY_COLS = ("id, number, op_at, created_at, updated_at, amount, direction, status, purpose, description, doc1c, deal_id, deal_number, deal_name, deal_stage, "
            "deal_amount, payer_id, payer_name, payer_inn, receiver_name, receiver_bank, invoice_id, invoice_number")
INSERT_PAY = f"INSERT OR REPLACE INTO pay_cache ({PAY_COLS}) VALUES (" + ",".join("?" * 23) + ")"


def _client(settings: Settings) -> SynergyClient:
    return SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token, timeout_sec=60.0,
                         min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries)


def _db(settings: Settings):
    conn = connect(settings.db_path)
    conn.executescript(SCHEMA)
    for tbl, col in (("pay_cache", "invoice_id TEXT"), ("pay_cache", "invoice_number TEXT"), ("pay_allocations", "invoice_id TEXT"), ("pay_allocations", "inv_deal_set INTEGER DEFAULT 0")):
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
        except Exception:   # noqa: BLE001 — колонка уже есть
            pass
    conn.commit()
    # LIKE в SQLite не различает регистр только у латиницы: «барьер» не находил «БАРЬЕР». Поиск идёт через ulower().
    conn.create_function("ulower", 1, lambda v: v.lower() if isinstance(v, str) else v)
    return conn


def _num(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ───────────────────────── копия платежей ─────────────────────────

def _stages(cl: SynergyClient, conn) -> dict[str, str]:
    row = conn.execute("SELECT v FROM pay_meta WHERE k='stages'").fetchone()
    st = json.loads(row[0]) if row else {}
    if not st:
        for page in (1, 2, 3):
            rows = cl.get("deal-stages", per_page=100, **{"page[number]": page}).get("data") or []
            st.update({x["id"]: x["attributes"].get("name") or "" for x in rows})
            if len(rows) < 100:
                break
        conn.execute("INSERT OR REPLACE INTO pay_meta VALUES ('stages', ?)", (json.dumps(st, ensure_ascii=False),))
        conn.commit()
    return st


def _row_from(x: dict, inc: dict, stages: dict) -> tuple:
    a = x["attributes"]
    rel = x.get("relationships") or {}
    g = lambda k: (rel.get(k) or {}).get("data")
    d, p, r, rb, iv = g("deal"), g("payer"), g("receiver"), g("receiver-bank-detail"), g("invoice")
    iva = inc.get((iv["type"], iv["id"]), {}) if iv else {}
    da = inc.get((d["type"], d["id"]), {}) if d else {}
    dcu = da.get("customs") or {}
    ds = ((da.get("_rel") or {}).get("stage") or {}).get("data") if da else None
    pa = inc.get((p["type"], p["id"]), {}) if p else {}
    ra = inc.get((r["type"], r["id"]), {}) if r else {}
    rba = inc.get((rb["type"], rb["id"]), {}) if rb else {}
    m = re.search(r"\[1С:([^\]]+)\]", str(a.get("description") or ""))
    return (x["id"], a.get("number"), a.get("operation-created-at") or a.get("created-at"), a.get("created-at"), a.get("updated-at"),
            _num(a.get("amount")), a.get("direction"), a.get("status"), a.get("purpose") or "", a.get("description") or "", m.group(1) if m else None,
            d["id"] if d else None, da.get("number"), da.get("name"), stages.get(ds["id"], "") if ds else None,
            (_num(dcu.get(FIELD_DEAL_SUM)) or _num(da.get("amount"))) if da else None,
            p["id"] if p else None, pa.get("name"), pa.get("inn"), ra.get("short-name") or ra.get("full-name"), rba.get("name"),
            iv["id"] if iv else None, iva.get("custom-number") if iv else None)


def sync_payments(cl: SynergyClient, conn, full: bool = False) -> int:
    """Обновить копию платежей. Обычно — только изменившиеся с прошлого раза (сортировка по updated-at); полная — раз в 6 часов."""
    stages = _stages(cl, conn)
    mark = conn.execute("SELECT v FROM pay_meta WHERE k='watermark'").fetchone()
    last_full = conn.execute("SELECT v FROM pay_meta WHERE k='last_full'").fetchone()
    if not mark or not last_full or time.time() - float(last_full[0]) > 6 * 3600:
        full = True
    water = "" if full else mark[0]
    seen, newest, page, n = set(), "", 1, 0
    while True:
        d = cl.get("invoice-payments", per_page=50, sort="-updated-at", include="deal,payer,receiver,receiver-bank-detail,invoice", **{"page[number]": page})
        inc = {}
        for i in d.get("included") or []:
            at = dict(i["attributes"])
            at["_rel"] = i.get("relationships") or {}
            inc[(i["type"], i["id"])] = at
        rows = d.get("data") or []
        for x in rows:
            upd = x["attributes"].get("updated-at") or ""
            newest = max(newest, upd)
            seen.add(x["id"])
            if upd >= water:
                conn.execute(INSERT_PAY, _row_from(x, inc, stages))
                n += 1
        conn.commit()
        if len(rows) < 50 or (not full and all((x["attributes"].get("updated-at") or "") < water for x in rows)):
            break
        page += 1
    if full and seen:
        conn.execute("DELETE FROM pay_cache WHERE id NOT IN (%s)" % ",".join("?" * len(seen)), tuple(seen))   # удалённые в Synergy платежи
        conn.execute("INSERT OR REPLACE INTO pay_meta VALUES ('last_full', ?)", (str(time.time()),))
    if newest:
        # запас в минуту: часы Synergy и сохранение в базе не совпадают до секунды
        conn.execute("INSERT OR REPLACE INTO pay_meta VALUES ('watermark', ?)", (newest[:16] + ":00",))
    conn.execute("INSERT OR REPLACE INTO pay_meta VALUES ('synced', ?)", (str(time.time()),))
    conn.commit()
    return n


def _refresh_rows(cl: SynergyClient, conn, ids: list[str]) -> None:
    stages = _stages(cl, conn)
    for pid in ids:
        try:
            d = cl.get(f"invoice-payments/{pid}", include="deal,payer,receiver,receiver-bank-detail,invoice")
        except httpx.HTTPStatusError:
            conn.execute("DELETE FROM pay_cache WHERE id=?", (pid,))
            continue
        inc = {(i["type"], i["id"]): {**i["attributes"], "_rel": i.get("relationships") or {}} for i in d.get("included") or []}
        conn.execute(INSERT_PAY, _row_from(d["data"], inc, stages))
    conn.commit()


def maybe_sync(cl: SynergyClient, conn, force: bool = False) -> None:
    s = conn.execute("SELECT v FROM pay_meta WHERE k='synced'").fetchone()
    if not force and s and time.time() - float(s[0]) < STALE_SEC:
        return
    if _lock.acquire(blocking=not s):   # первую копию ждём, остальные обновления не задерживают открытие страницы
        try:
            sync_payments(cl, conn)
        finally:
            _lock.release()


# ───────────────────────── фоновое обновление ─────────────────────────
# Раньше синхронизацию с Synergy и дочитывание подробностей сделок делал сам запрос страницы: раз в минуту-другую
# переключение вкладки «зависало» на 4–11 секунд. Теперь это делает фоновый поток, а запросы только читают готовое.

_bg = {"thread": None, "wake": threading.Event()}
BG_EVERY = 40   # сек между обновлениями


def _bg_loop(settings: Settings) -> None:
    conn = _db(settings)
    cl = _client(settings)
    while True:
        try:
            with _lock:
                sync_payments(cl, conn)
                sync_invoices(cl, conn)
            ids = [r[0] for r in conn.execute("SELECT DISTINCT deal_id FROM pay_cache WHERE deal_id IS NOT NULL")]
            deal_info(cl, conn, ids)
        except Exception:   # noqa: BLE001 — Synergy недоступна или лимит: следующий круг попробует снова
            logger.exception("фоновое обновление платежей")
        try:
            _warm(cl, conn)
        except Exception:   # noqa: BLE001 — прогрев не критичен, карточка просто откроется по-старому
            logger.exception("прогрев карточек")
        _bg["wake"].wait(BG_EVERY)
        _bg["wake"].clear()


def _warm(cl: SynergyClient, conn, budget: int = 12) -> None:
    """Прогрев копии для карточек: компании неразнесённых платежей и неоплаченных счетов, самые «старые» копии первыми."""
    pairs: dict[str, str] = {}
    for r in conn.execute("SELECT payer_id, payer_inn FROM pay_cache WHERE deal_id IS NULL AND payer_id IS NOT NULL AND op_at >= ? ORDER BY op_at DESC", (RECENT_FROM,)):
        pairs[r[0]] = r[1] or ""
    for r in conn.execute("SELECT payer_id, payer_inn FROM inv_cache WHERE status != 'paid' AND payer_id IS NOT NULL ORDER BY issued DESC"):
        pairs.setdefault(r[0], r[1] or "")
    now = time.time()

    def age(cid: str) -> float:
        h = [_RAW.get(("cdeals", cid, 1)), _RAW.get(("cinv", cid, 1))]
        return min((now - x[0] for x in h if x), default=1e9) if all(h) else 1e9

    todo = sorted((c for c in pairs if age(c) > WARM_AGE), key=age, reverse=True)[:budget]
    for cid in todo:
        ids = [cid]
        if pairs[cid]:
            ids += [x["id"] for x in _companies_by_inn(cl, pairs[cid], force=age(cid) > 3000) if x["id"] != cid and x["attributes"].get("inn") == pairs[cid]]
        for c in ids[:6]:
            _company_deals(cl, c, set(), frozenset(), force=True)
            _client_invoices(cl, [c], set(), force=True)


def ensure_background(settings: Settings) -> None:
    if _bg["thread"] is None or not _bg["thread"].is_alive():
        t = threading.Thread(target=_bg_loop, args=(settings,), name="payments-bg", daemon=True)
        _bg["thread"] = t
        t.start()


# ───────────────────────── копия счетов ─────────────────────────

INV_COLS = "id, number, issued, amount, balance, status, payer_id, payer_name, payer_inn, deal_id, deal_number, deal_name, manager, purpose, reason, org, updated_at"
INSERT_INV = f"INSERT OR REPLACE INTO inv_cache ({INV_COLS}) VALUES (" + ",".join("?" * 17) + ")"


def _inv_row(x: dict, inc: dict) -> tuple:
    a = x["attributes"]
    rel = x.get("relationships") or {}
    g = lambda k: (rel.get(k) or {}).get("data") or {}
    pa, da, ra = inc.get(("companies", g("payer").get("id"))) or {}, inc.get(("deals", g("deal").get("id"))) or {}, inc.get(("users", g("responsible").get("id"))) or {}
    reason = a.get("reason") or ""
    m = re.match(r"Выставлен от:\s*([^·]+)", reason)
    return (x["id"], str(a.get("custom-number") or a.get("number")), (a.get("issued-at") or "")[:10], _num(a.get("amount")) or 0.0, _num(a.get("balance")) or 0.0,
            a.get("status"), g("payer").get("id"), pa.get("name"), pa.get("inn"), g("deal").get("id"), da.get("number"), (da.get("name") or "").strip() or None,
            "", a.get("purpose") or "", reason, m.group(1).strip() if m else None, a.get("updated-at"))   # manager заполняет _apply_managers из inv_manager


def _apply_managers(conn) -> None:
    """ФИО менеджера счёта — из комментария счёта в 1С (таблица inv_manager, scripts/invoice_managers.py), а не «ответственный» 1С/Synergy:
    решение владельца 18.11.2026. Нет в комментарии — пусто."""
    conn.execute("UPDATE inv_cache SET manager = COALESCE((SELECT fio FROM inv_manager m WHERE m.synergy_id = inv_cache.id), '')")
    conn.commit()


def sync_invoices(cl: SynergyClient, conn, full: bool = False) -> int:
    mark = conn.execute("SELECT v FROM pay_meta WHERE k='inv_watermark'").fetchone()
    last_full = conn.execute("SELECT v FROM pay_meta WHERE k='inv_last_full'").fetchone()
    if not mark or not last_full or time.time() - float(last_full[0]) > 6 * 3600:
        full = True
    water = "" if full else mark[0]
    seen, newest, page, n = set(), "", 1, 0
    while True:
        d = cl.get("invoices", per_page=50, sort="-updated-at", include="payer,deal,responsible", **{"page[number]": page})
        inc = {(i["type"], i["id"]): i["attributes"] for i in d.get("included") or []}
        rows = d.get("data") or []
        for x in rows:
            upd = x["attributes"].get("updated-at") or ""
            newest = max(newest, upd)
            seen.add(x["id"])
            if upd >= water:
                conn.execute(INSERT_INV, _inv_row(x, inc))
                n += 1
        conn.commit()
        if len(rows) < 50 or (not full and all((x["attributes"].get("updated-at") or "") < water for x in rows)):
            break
        page += 1
    if full and seen:
        conn.execute("DELETE FROM inv_cache WHERE id NOT IN (%s)" % ",".join("?" * len(seen)), tuple(seen))
        conn.execute("INSERT OR REPLACE INTO pay_meta VALUES ('inv_last_full', ?)", (str(time.time()),))
    if newest:
        conn.execute("INSERT OR REPLACE INTO pay_meta VALUES ('inv_watermark', ?)", (newest[:16] + ":00",))
    conn.commit()
    _apply_managers(conn)
    return n


def _refresh_invoice_rows(cl: SynergyClient, conn, ids: list[str]) -> None:
    for iid in ids:
        try:
            d = cl.get(f"invoices/{iid}", include="payer,deal,responsible")
        except httpx.HTTPStatusError:
            conn.execute("DELETE FROM inv_cache WHERE id=?", (iid,))
            continue
        inc = {(i["type"], i["id"]): i["attributes"] for i in d.get("included") or []}
        conn.execute(INSERT_INV, _inv_row(d["data"], inc))
    conn.commit()
    _apply_managers(conn)


# ───────────────────────── 1С: суммы документов для подсказок ─────────────────────────

_items_cache: dict = {"mt": None, "map": {}}


def _items() -> dict[str, dict]:
    files = sorted(DATA.glob("incoming_*.json"))
    mt = tuple(f.stat().st_mtime for f in files)
    if _items_cache["mt"] != mt:
        m: dict[str, dict] = {}
        for f in files:
            try:
                for i in json.loads(f.read_text(encoding="utf-8")):
                    if i.get("lines") or i["number"] not in m:
                        m[i["number"]] = i
            except (OSError, ValueError):
                pass
        _items_cache.update(mt=mt, map=m)
    return _items_cache["map"]


# ───────────────────────── страница и список ─────────────────────────

@router.get("/payments", response_class=HTMLResponse)
def payments_page(request: Request):
    base = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    return TEMPLATES.TemplateResponse("payments.html", {"request": request, "base": base})


# Колонки с фильтром «как в Excel»: выражение SQL, по которому берутся значения. Пустое значение — маркер EMPTY.
EMPTY = "__empty__"
COLS = {
    "op_at": "substr(op_at, 1, 10)",
    "amount": "amount",
    "status": "status",
    "deal": "CASE WHEN deal_id IS NULL THEN NULL ELSE COALESCE(deal_name, '') || ' №' || COALESCE(deal_number, '') END",
    "type": "direction",
    "payer": "payer_name",
    "receiver": "receiver_name",
}


def _flt_sql(flt: dict, skip: str | None = None) -> tuple[list[str], list]:
    w, p = [], []
    for col, val in (flt or {}).items():
        if col == skip:
            continue
        if col == "purpose":   # текстовая колонка: «содержит»
            if str(val).strip():
                w.append("ulower(purpose) LIKE ?"); p.append(f"%{str(val).strip().lower()}%")
            continue
        expr = COLS.get(col)
        if not expr or not isinstance(val, list) or not val:
            continue
        vals = [v for v in val if v != EMPTY]
        parts = []
        if vals:
            parts.append(f"CAST({expr} AS TEXT) IN ({','.join('?' * len(vals))})"); p += [str(v) for v in vals]
        if EMPTY in val:
            parts.append(f"({expr} IS NULL OR CAST({expr} AS TEXT) = '')")
        w.append("(" + " OR ".join(parts) + ")")
    return w, p


def _where(tab: str, q: str, receiver: str, status: str, since: str, until: str, flt: dict | None = None, skip: str | None = None) -> tuple[str, list]:
    w, p = ["1=1"], []
    fw, fp = _flt_sql(flt or {}, skip)
    w += fw; p += fp
    if tab == "wait":
        w.append("direction='income' AND deal_id IS NULL AND (doc1c IS NOT NULL OR op_at >= ?) AND id NOT IN (SELECT payment_id FROM pay_flags)")
        p.append(RECENT_FROM)
    elif tab == "clarify":
        w.append("id IN (SELECT payment_id FROM pay_flags)")
    elif tab == "done":
        w.append("direction='income' AND deal_id IS NOT NULL")
    elif tab == "other":   # всё, что не вошло в три вкладки выше: расходы и старые приходы без сделки (без документа 1С, до ноября 2026)
        w.append("id NOT IN (SELECT payment_id FROM pay_flags) AND NOT (direction='income' AND deal_id IS NOT NULL) "
                 "AND NOT (direction='income' AND deal_id IS NULL AND (doc1c IS NOT NULL OR op_at >= ?))")
        p.append(RECENT_FROM)
    if q:
        like = f"%{q.strip().lower()}%"
        w.append("(ulower(payer_name) LIKE ? OR ulower(purpose) LIKE ? OR ulower(doc1c) LIKE ? OR CAST(number AS TEXT)=? OR CAST(CAST(amount AS INTEGER) AS TEXT)=? OR ulower(deal_name) LIKE ? OR CAST(deal_number AS TEXT)=? OR payer_inn LIKE ?)")
        digits = re.sub(r"\D", "", q)
        p += [like, like, like, q.strip(), digits, like, q.strip(), like]
    if receiver:
        w.append("receiver_name = ?"); p.append(receiver)
    if status in ("executed", "not_executed"):
        w.append("status = ?"); p.append(status)
    if since:
        w.append("op_at >= ?"); p.append(since)
    if until:
        w.append("op_at < ?"); p.append(until + "T99")
    return " AND ".join(w), p


DEAL_TTL = 300   # сек: подробности сделок (клиент, транспорт, ставка, адрес, оплачено) в списке разнесённых


def deal_info(cl: SynergyClient, conn, ids: list[str], max_fetch: int = 40, force: bool = False, only_cached: bool = False) -> dict[str, dict]:
    """Подробности сделок для списка платежей. Берём из копии `pay_deals`, устаревшие/отсутствующие дочитываем из Synergy."""
    out, now, fetched = {}, time.time(), 0
    for did in ids:
        row = conn.execute("SELECT data, fetched FROM pay_deals WHERE id=?", (did,)).fetchone()
        if row and (only_cached or (not force and now - row["fetched"] < DEAL_TTL)):
            out[did] = json.loads(row["data"])
            continue
        if only_cached:
            continue
        if fetched >= max_fetch:
            if row:
                out[did] = json.loads(row["data"])
            continue
        try:
            d = cl.get(f"deals/{did}", include="stage,responsible,transport-type,company,contact")
        except httpx.HTTPStatusError:
            if row:
                out[did] = json.loads(row["data"])
            continue
        fetched += 1
        inc = {(i["type"], i["id"]): i["attributes"] for i in d.get("included") or []}
        view = _deal_view(d["data"], inc, set(), frozenset())
        out[did] = view
        conn.execute("INSERT OR REPLACE INTO pay_deals VALUES (?,?,?)", (did, json.dumps(view, ensure_ascii=False), now))
    conn.commit()
    return out


@router.get("/payments/api/list")
def api_list(request: Request, tab: str = "wait", q: str = "", receiver: str = "", status: str = "", since: str = "", until: str = "",
             sort: str = "op_at", desc: int = 1, offset: int = 0, limit: int = 100, refresh: int = 0, filters: str = ""):
    settings: Settings = request.app.state.settings
    conn = _db(settings)
    ensure_background(settings)
    if refresh:
        _bg["wake"].set()   # «обновить сейчас» — фоновый поток проснётся, запрос не ждёт
    if not conn.execute("SELECT 1 FROM pay_meta WHERE k='synced'").fetchone():
        with _lock:   # самый первый запуск: копии ещё нет, ждём её один раз
            if not conn.execute("SELECT 1 FROM pay_meta WHERE k='synced'").fetchone():
                sync_payments(_client(settings), conn)
    col = {"op_at": "op_at", "amount": "amount", "status": "status", "deal": "deal_number", "payer": "payer_name", "number": "number", "type": "direction", "receiver": "receiver_name"}.get(sort, "op_at")
    flt = _parse_filters(filters)
    w, p = _where(tab, q, receiver, status, since, until, flt)
    rows = conn.execute(f"SELECT * FROM pay_cache WHERE {w} ORDER BY {col} {'DESC' if desc else 'ASC'}, id DESC LIMIT ? OFFSET ?", (*p, min(limit, 200), offset)).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM pay_cache WHERE {w}", p).fetchone()[0]
    counts = {}
    for t in ("wait", "clarify", "done", "other", "all"):
        tw, tp = _where(t, "", "", "", "", "")
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM pay_cache WHERE {tw}", tp).fetchone()[0]
    flags = {r["payment_id"]: dict(r) for r in conn.execute("SELECT * FROM pay_flags")}
    last = {r["payment_id"]: r["grp"] for r in conn.execute("SELECT payment_id, grp FROM pay_allocations WHERE action='allocate' AND undone=0 ORDER BY id")}
    recv = [r[0] for r in conn.execute("SELECT DISTINCT receiver_name FROM pay_cache WHERE receiver_name IS NOT NULL ORDER BY 1")]
    out = []
    for r in rows:
        d = dict(r)
        d["flag"] = flags.get(d["id"])
        d["can_undo"] = d["id"] in last
        hint = re.search(r"ПОДСКАЗКА ПО СДЕЛКЕ[^:]*:\s*(.*)$", d["description"] or "")
        d["hint"] = hint.group(1) if hint else ""
        d.pop("description", None)
        out.append(d)
    try:
        info = deal_info(None, conn, sorted({d["deal_id"] for d in out if d.get("deal_id")}), only_cached=True)
        for d in out:
            d["deal"] = info.get(d["deal_id"]) if d.get("deal_id") else None
    except Exception as exc:   # noqa: BLE001 — без подробностей список всё равно показываем
        logger.warning("подробности сделок не получены: %s", exc)
    synced = conn.execute("SELECT v FROM pay_meta WHERE k='synced'").fetchone()
    return {"rows": out, "total": total, "counts": counts, "receivers": recv, "synced_ago": int(time.time() - float(synced[0])) if synced else None}


def _parse_filters(raw: str) -> dict:
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


@router.get("/payments/api/values")
def api_values(request: Request, col: str, tab: str = "wait", q: str = "", receiver: str = "", status: str = "", since: str = "", until: str = "",
               filters: str = "", search: str = ""):
    """Значения колонки для окна фильтра «как в Excel»: с числом платежей, с учётом остальных фильтров (но не этой колонки)."""
    conn = _db(request.app.state.settings)
    expr = COLS.get(col)
    if not expr:
        return JSONResponse({"error": "колонка без списка значений"}, status_code=400)
    w, p = _where(tab, q, receiver, status, since, until, _parse_filters(filters), skip=col)
    rows = conn.execute(f"SELECT {expr} AS v, COUNT(*) AS n FROM pay_cache WHERE {w} GROUP BY v ORDER BY (v IS NULL OR v = ''), v LIMIT 600", p).fetchall()
    out = []
    for r in rows:
        v = r["v"]
        key = EMPTY if v is None or str(v) == "" else str(v)
        if search and key != EMPTY and search.lower() not in str(v).lower():
            continue
        out.append({"v": key, "n": r["n"]})
    return {"values": out}


# ───────────────────────── подбор сделок ─────────────────────────

def _deal_view(x: dict, inc: dict, sums: set[float], keys: set[str] = frozenset()) -> dict:
    a = x["attributes"]
    cu = a.get("customs") or {}
    rel = x.get("relationships") or {}
    st = ((rel.get("stage") or {}).get("data") or {})
    rs = ((rel.get("responsible") or {}).get("data") or {})
    stage = (inc.get(("deal-stages", st.get("id"))) or {}).get("name", "")
    resp = inc.get(("users", rs.get("id"))) or {}
    total = _num(cu.get(FIELD_DEAL_SUM)) or _num(a.get("amount")) or 0.0
    paid = _num(cu.get(FIELD_PAID)) or 0.0
    cands = {v for v in (_num(cu.get(FIELD_DEAL_SUM)), _num(a.get("amount"))) if v}
    tt = ((rel.get("transport-type") or {}).get("data") or {})
    co = ((rel.get("company") or {}).get("data") or {})
    ct = ((rel.get("contact") or {}).get("data") or {})
    contact = inc.get(("contacts", ct.get("id"))) or {}
    cname = (inc.get(("companies", co.get("id"))) or {}).get("name")
    pname = contact.get("as-string") or contact.get("name") or ""
    client = cname or (f"{pname} (контакт, компания в сделке не указана)" if pname else "")
    return {"id": x["id"], "number": a.get("number"), "client": client, "client_id": co.get("id") or "", "contact": contact.get("as-string") or contact.get("name") or "",
            "transport": (inc.get(("transport-types", tt.get("id"))) or {}).get("name", ""), "rate": _num(cu.get(FIELD_RATE)),
            "address": (cu.get(FIELD_ADDRESS) or "").strip(),
            "start": str(cu.get(FIELD_START) or "")[:10], "end": str(cu.get(FIELD_END) or "")[:10], "name": (a.get("name") or "").strip(), "created": (a.get("created-at") or "")[:10], "stage": stage,
            "manager": " ".join(filter(None, [resp.get("last-name"), (resp.get("first-name") or "")[:1] + "." if resp.get("first-name") else ""])),
            "total": total, "paid": paid, "left": max(total - paid, 0.0) if total else None, "sum_match": bool(cands & sums),
            "key_match": any(k and k in (a.get("name") or "") for k in keys)}


def _company_deals(cl: SynergyClient, cid: str, sums: set[float], keys: set[str] = frozenset(), limit: int = 50, force: bool = False) -> list[dict]:
    out = []
    for page in (1, 2):
        d = _cached(("cdeals", cid, page), lambda: cl.get(f"companies/{cid}/deals", per_page=50, sort="-created-at", include="stage,responsible,transport-type,company,contact", **{"page[number]": page}), force=force)
        inc = {(i["type"], i["id"]): i["attributes"] for i in d.get("included") or []}
        out += [_deal_view(x, inc, sums, keys) for x in d.get("data") or []]
        if len(d.get("data") or []) < 50 or len(out) >= limit:
            break
    return out


@router.get("/payments/api/{pid}/candidates")
def api_candidates(request: Request, pid: str, company: str = ""):
    settings: Settings = request.app.state.settings
    conn = _db(settings)
    cl = _client(settings)
    row = conn.execute("SELECT * FROM pay_cache WHERE id=?", (pid,)).fetchone()
    if not row:
        return JSONResponse({"error": "платёж не найден"}, status_code=404)
    item = _items().get(row["doc1c"] or "") or {}
    sums = {row["amount"]}
    keys: set[str] = set()   # номера счёта и реализации 1С: у части сделок они в названии («1С CКБП-001920 стр.491»)
    for l in item.get("lines") or []:
        sums |= {float(l[k]) for k in ("sum", "inv_sum", "rel_sum") if l.get(k)}
        keys |= {l[k] for k in ("inv", "rel") if l.get(k)}
    comp_ids: list[str] = []
    if company:
        comp_ids = [company]
    else:
        if row["payer_id"]:
            comp_ids.append(row["payer_id"])
        if row["payer_inn"]:   # у одного клиента в Synergy бывает несколько записей-компаний
            for x in _companies_by_inn(cl, row["payer_inn"]):
                if x["id"] not in comp_ids and x["attributes"].get("inn") == row["payer_inn"]:
                    comp_ids.append(x["id"])
    seen, deals = set(), []
    for cid in comp_ids[:6]:
        for d in _company_deals(cl, cid, sums, keys):
            if d["id"] not in seen:
                seen.add(d["id"])
                d["company_id"] = cid
                deals.append(d)
    deals.sort(key=lambda d: d["created"], reverse=True)
    deals.sort(key=lambda d: (not d["key_match"], not d["sum_match"]))   # сначала номер 1С в названии, потом совпавшая сумма, дальше новые
    inv = [{"inv": l.get("inv"), "inv_date": l.get("inv_date"), "inv_sum": l.get("inv_sum"), "inv_comment": l.get("inv_comment"),
            "rel": l.get("rel"), "rel_comment": l.get("rel_comment")} for l in (item.get("lines") or [])]
    invoices = _client_invoices(cl, comp_ids, keys, company_only=bool(company))
    mg = {r[0]: r[1] for r in conn.execute("SELECT synergy_id, fio FROM inv_manager")}
    for i in invoices:
        i["manager"] = mg.get(i["id"], "")
    return {"payment": {k: row[k] for k in row.keys() if k != "description"}, "purpose": row["purpose"], "onec": inv, "deals": deals[:60],
            "companies": comp_ids, "invoices": invoices}


def _client_invoices(cl: SynergyClient, comp_ids: list[str], keys: set[str], company_only: bool = False, force: bool = False) -> list[dict]:
    """Счета клиента из Synergy (загружены из 1С): номер 1С, дата, сумма, остаток, статус, сделка, менеджер. Неоплаченные и частично оплаченные — первыми."""
    out, seen = [], set()
    for cid in comp_ids[:6]:
        for page in (1, 2):
            try:
                d = _cached(("cinv", cid, page), lambda: cl.get(f"companies/{cid}/invoices", per_page=50, sort="-created-at", include="deal,responsible", **{"page[number]": page}), force=force)
            except httpx.HTTPStatusError:
                break
            inc = {(i["type"], i["id"]): i["attributes"] for i in d.get("included") or []}
            for x in d.get("data") or []:
                if x["id"] in seen:
                    continue
                seen.add(x["id"])
                a = x["attributes"]
                dl = ((x.get("relationships") or {}).get("deal") or {}).get("data") or {}
                da = inc.get(("deals", dl.get("id"))) or {}
                rs = ((x.get("relationships") or {}).get("responsible") or {}).get("data") or {}
                ra = inc.get(("users", rs.get("id"))) or {}
                number = str(a.get("custom-number") or a.get("number"))
                out.append({"id": x["id"], "number": number, "date": (a.get("issued-at") or "")[:10], "amount": _num(a.get("amount")) or 0.0,
                            "balance": _num(a.get("balance")) or 0.0, "status": a.get("status"), "deal_id": dl.get("id") or "", "deal_number": da.get("number"),
                            "deal_name": (da.get("name") or "").strip(), "manager": " ".join(filter(None, [ra.get("last-name"), (ra.get("first-name") or "")[:1] + "." if ra.get("first-name") else ""])),
                            "reason": a.get("reason") or "", "purpose": a.get("purpose") or "", "match": number in keys,
                            "org": (re.match(r"Выставлен от:\s*([^·]+)", a.get("reason") or "") or [None, ""])[1].strip()})
            if len(d.get("data") or []) < 50:
                break
    out.sort(key=lambda i: (not i["match"], i["status"] == "paid", i["date"]), reverse=False)
    out.sort(key=lambda i: i["date"], reverse=True)
    out.sort(key=lambda i: (not i["match"], i["status"] == "paid"))
    return out[:60]


# ───────────────────────── раздел «Счета» ─────────────────────────

INV_COLS_FILTER = {
    "issued": "issued", "number": "number", "status": "status", "amount": "amount", "balance": "balance",
    "payer": "payer_name", "deal": "CASE WHEN deal_id IS NULL THEN NULL ELSE COALESCE(deal_name, '') || ' №' || COALESCE(deal_number, '') END",
    "manager": "manager", "org": "org",
}


def _inv_where(tab: str, q: str, flt: dict, skip: str | None = None) -> tuple[str, list]:
    w, p = ["1=1"], []
    if tab == "unpaid":
        w.append("status != 'paid'")
    elif tab == "paid":
        w.append("status = 'paid'")
    elif tab == "nodeal":
        w.append("deal_id IS NULL")
    if q:
        like = f"%{q.strip().lower()}%"
        w.append("(ulower(payer_name) LIKE ? OR ulower(number) LIKE ? OR ulower(purpose) LIKE ? OR ulower(reason) LIKE ? OR ulower(manager) LIKE ? OR ulower(deal_name) LIKE ? OR payer_inn LIKE ? OR CAST(CAST(amount AS INTEGER) AS TEXT)=? OR CAST(deal_number AS TEXT)=?)")
        p += [like, like, like, like, like, like, f"%{q.strip()}%", re.sub(r"\D", "", q), q.strip()]
    for col, val in (flt or {}).items():
        if col == skip:
            continue
        if col == "purpose":
            if str(val).strip():
                w.append("ulower(purpose) LIKE ?"); p.append(f"%{str(val).strip().lower()}%")
            continue
        expr = INV_COLS_FILTER.get(col)
        if not expr or not isinstance(val, list) or not val:
            continue
        vals = [v for v in val if v != EMPTY]
        parts = []
        if vals:
            parts.append(f"CAST({expr} AS TEXT) IN ({','.join('?' * len(vals))})"); p += [str(v) for v in vals]
        if EMPTY in val:
            parts.append(f"({expr} IS NULL OR CAST({expr} AS TEXT) = '')")
        w.append("(" + " OR ".join(parts) + ")")
    return " AND ".join(w), p


@router.get("/payments/invoices", response_class=HTMLResponse)
def invoices_page(request: Request):
    base = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    return TEMPLATES.TemplateResponse("invoices.html", {"request": request, "base": base})


@router.get("/payments/api/inv/list")
def api_inv_list(request: Request, tab: str = "unpaid", q: str = "", sort: str = "issued", desc: int = 1, offset: int = 0, limit: int = 100, filters: str = ""):
    settings: Settings = request.app.state.settings
    conn = _db(settings)
    ensure_background(settings)
    if not conn.execute("SELECT 1 FROM pay_meta WHERE k='inv_watermark'").fetchone():
        with _lock:   # самый первый запуск: копии счетов ещё нет
            if not conn.execute("SELECT 1 FROM pay_meta WHERE k='inv_watermark'").fetchone():
                sync_invoices(_client(settings), conn)
    flt = _parse_filters(filters)
    col = {"issued": "issued", "number": "number", "status": "status", "amount": "amount", "balance": "balance", "payer": "payer_name", "deal": "deal_number", "manager": "manager", "org": "org"}.get(sort, "issued")
    w, p = _inv_where(tab, q, flt)
    rows = conn.execute(f"SELECT * FROM inv_cache WHERE {w} ORDER BY {col} {'DESC' if desc else 'ASC'}, id DESC LIMIT ? OFFSET ?", (*p, min(limit, 200), offset)).fetchall()
    total = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(amount),0), COALESCE(SUM(balance),0) FROM inv_cache WHERE {w}", p).fetchone()
    counts = {}
    for t in ("unpaid", "paid", "nodeal", "all"):
        tw, tp = _inv_where(t, "", {})
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM inv_cache WHERE {tw}", tp).fetchone()[0]
    return {"rows": [dict(r) for r in rows], "total": total[0], "sum_amount": total[1], "sum_balance": total[2], "counts": counts}


@router.get("/payments/api/inv/values")
def api_inv_values(request: Request, col: str, tab: str = "unpaid", q: str = "", filters: str = ""):
    conn = _db(request.app.state.settings)
    expr = INV_COLS_FILTER.get(col)
    if not expr:
        return JSONResponse({"error": "колонка без списка значений"}, status_code=400)
    w, p = _inv_where(tab, q, _parse_filters(filters), skip=col)
    rows = conn.execute(f"SELECT {expr} AS v, COUNT(*) AS n FROM inv_cache WHERE {w} GROUP BY v ORDER BY (v IS NULL OR v = ''), v LIMIT 600", p).fetchall()
    return {"values": [{"v": (EMPTY if r["v"] is None or str(r["v"]) == "" else str(r["v"])), "n": r["n"]} for r in rows]}


@router.get("/payments/api/inv/{iid}/deals")
def api_inv_deals(request: Request, iid: str, company: str = ""):
    """Сделки клиента для привязки счёта."""
    settings: Settings = request.app.state.settings
    conn = _db(settings)
    cl = _client(settings)
    row = conn.execute("SELECT * FROM inv_cache WHERE id=?", (iid,)).fetchone()
    if not row:
        return JSONResponse({"error": "счёт не найден"}, status_code=404)
    comp_ids = [company] if company else ([row["payer_id"]] if row["payer_id"] else [])
    if not company and row["payer_inn"]:
        for x in _companies_by_inn(cl, row["payer_inn"]):
            if x["id"] not in comp_ids and x["attributes"].get("inn") == row["payer_inn"]:
                comp_ids.append(x["id"])
    sums, keys = {row["amount"]}, {row["number"]}
    seen, deals = set(), []
    for cid in comp_ids[:6]:
        for d in _company_deals(cl, cid, sums, keys):
            if d["id"] not in seen:
                seen.add(d["id"]); deals.append(d)
    deals.sort(key=lambda d: d["created"], reverse=True)
    deals.sort(key=lambda d: (not d["key_match"], not d["sum_match"]))
    return {"invoice": dict(row), "deals": deals[:60]}


@router.post("/payments/api/inv/{iid}/deal")
async def api_inv_set_deal(request: Request, iid: str):
    body = await request.json()
    return await _run(request, lambda: _inv_set_deal(request.app.state.settings, iid, body))


def _inv_set_deal(settings: Settings, iid: str, body: dict) -> dict:
    conn = _db(settings)
    cl = _client(settings)
    deal_id = body.get("deal_id")
    user = str(body.get("user") or "").strip()[:60] or "не указано"
    cur = cl.get(f"invoices/{iid}", include="deal")["data"]
    old = ((cur["relationships"].get("deal") or {}).get("data") or {}).get("id")
    try:
        cl.patch(f"invoices/{iid}", {"data": {"type": "invoices", "id": iid, "relationships": {"deal": {"data": ({"type": "deals", "id": str(deal_id)} if deal_id else None)}}}})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 422:   # у счёта уже есть платежи по другой сделке
            raise _UserError("Synergy не даёт сменить сделку: к счёту уже привязаны платежи по другой сделке. Сначала отмените разноску этих платежей.")
        raise
    conn.execute("INSERT INTO pay_allocations (grp,ts,user,action,payment_id,deal_id,invoice_id,note) VALUES (?,?,?,?,?,?,?,?)",
                 (uuid.uuid4().hex[:12], _now(), user, "inv_deal", "", str(deal_id or ""), iid, f"было: {old or '—'}"))
    conn.commit()
    _touch_invoice(cl, iid)
    _refresh_invoice_rows(cl, conn, [iid])
    return {"ok": True}


@router.get("/payments/api/companies")
def api_companies(request: Request, q: str = ""):
    settings: Settings = request.app.state.settings
    cl = _client(settings)
    q = q.strip()
    if len(q) < 3:
        return {"companies": []}
    key = "filter[inn]" if re.fullmatch(r"\d{10,12}", q) else "filter[name]"
    rows = cl.get("companies", per_page=15, **{key: q}).get("data") or []
    return {"companies": [{"id": x["id"], "name": x["attributes"].get("name"), "inn": x["attributes"].get("inn")} for x in rows]}


# ───────────────────────── разноска ─────────────────────────

def _recount_deal(cl: SynergyClient, deal_id: str) -> float:
    """Сумма проведённых приходов сделки -> custom-30778 (то же правило, что у таймера dashboard-deal-payments)."""
    total = 0.0
    for p in cl.get(f"deals/{deal_id}/invoice-payments", per_page=50).get("data") or []:
        a = p["attributes"]
        if a.get("direction") == "income" and a.get("status") == "executed":
            total += float(a.get("amount") or 0)
    cl.patch(f"deals/{deal_id}", {"data": {"type": "deals", "id": str(deal_id), "attributes": {"customs": {FIELD_PAID: total}}}})
    return total


def _delete(cl: SynergyClient, path: str) -> int:
    cl._wait()
    return httpx.delete(f"{cl._base}/{path}", headers=cl._headers, timeout=60).status_code


def _create_payment(cl: SynergyClient, attrs: dict, rel: dict, marker: str) -> str:
    """Создание нового платежа без слепого повтора: при неопределённом ответе ищем платёж по метке разноски."""
    body = {"data": {"type": "invoice-payments", "attributes": attrs, "relationships": rel}}
    try:
        return str(cl.post_once("invoice-payments", body)["data"]["id"])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500 and exc.response.status_code != 429:
            raise
    except httpx.TransportError:
        pass
    time.sleep(3)   # неизвестно, создалось ли: смотрим свежие платежи
    for x in cl.get("invoice-payments", per_page=20, sort="-created-at").get("data") or []:
        if marker in str(x["attributes"].get("description") or ""):
            return str(x["id"])
    raise RuntimeError("Synergy не подтвердила создание части платежа; проверьте раздел «Платежи» и повторите")


def _rel(row: dict, deal_id: str | None, invoice_id: str | None = None) -> dict:
    rel: dict = {}
    if invoice_id:
        rel["invoice"] = {"data": {"type": "invoices", "id": str(invoice_id)}}
    if row.get("receiver_id"):
        rel["receiver"] = {"data": {"type": "org-details", "id": row["receiver_id"]}}
    if row.get("receiver_bank_id"):
        rel["receiver-bank-detail"] = {"data": {"type": "account-bank-details", "id": row["receiver_bank_id"]}}
    if row.get("payer_id"):
        rel["payer"] = {"data": {"type": "companies", "id": row["payer_id"]}}
    if row.get("payer_bank_id"):
        rel["payer-bank-detail"] = {"data": {"type": "company-bank-details", "id": row["payer_bank_id"]}}
    if deal_id:
        rel["deal"] = {"data": {"type": "deals", "id": str(deal_id)}}
    return rel


@router.post("/payments/api/{pid}/allocate")
async def api_allocate(request: Request, pid: str):
    body = await request.json()
    return await _run(request, lambda: _allocate(request.app.state.settings, pid, body))


@router.post("/payments/api/{pid}/undo")
async def api_undo(request: Request, pid: str):
    body = await request.json()
    return await _run(request, lambda: _undo(request.app.state.settings, pid, body))


@router.post("/payments/api/{pid}/flag")
async def api_flag(request: Request, pid: str):
    body = await request.json()
    conn = _db(request.app.state.settings)
    if body.get("clear"):
        conn.execute("DELETE FROM pay_flags WHERE payment_id=?", (pid,))
    else:
        conn.execute("INSERT OR REPLACE INTO pay_flags VALUES (?,?,?,?)", (pid, str(body.get("note") or "")[:500], str(body.get("user") or "")[:60], _now()))
    conn.commit()
    return {"ok": True}


async def _run(request: Request, fn):
    from starlette.concurrency import run_in_threadpool
    try:
        res = await run_in_threadpool(fn)
        _raw_forget()
        return res
    except _UserError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:   # noqa: BLE001
        logger.exception("разноска платежа: ошибка")
        return JSONResponse({"error": f"Ошибка: {exc}"}, status_code=500)


class _UserError(Exception):
    pass


def _full_payment(cl: SynergyClient, pid: str) -> tuple[dict, dict]:
    d = cl.get(f"invoice-payments/{pid}", include="deal,payer,payer-bank-detail,receiver,receiver-bank-detail")
    a = d["data"]["attributes"]
    rel = d["data"]["relationships"]
    g = lambda k: ((rel.get(k) or {}).get("data") or {}).get("id")
    return a, {"deal_id": g("deal"), "payer_id": g("payer"), "payer_bank_id": g("payer-bank-detail"), "receiver_id": g("receiver"), "receiver_bank_id": g("receiver-bank-detail")}


def _touch_invoice(cl: SynergyClient, iid: str) -> None:
    """Любой PATCH счёта заставляет Synergy пересчитать остаток и статус (при отвязке платежа сам он этого не делает — проверено 18.11.2026)."""
    try:
        cur = cl.get(f"invoices/{iid}")["data"]["attributes"]
        cl.patch(f"invoices/{iid}", {"data": {"type": "invoices", "id": iid, "attributes": {"purpose": cur.get("purpose") or ""}}})
    except Exception:   # noqa: BLE001
        logger.exception("пересчёт счёта %s", iid)


def _allocate(settings: Settings, pid: str, body: dict) -> dict:
    conn = _db(settings)
    cl = _client(settings)
    user = str(body.get("user") or "").strip()[:60] or "не указано"
    parts = [{"deal_id": str(p["deal_id"]), "amount": round(float(p["amount"]), 2), "number": p.get("number"), "invoice_id": str(p.get("invoice_id") or ""),
              "invoice_number": p.get("invoice_number") or ""} for p in body.get("parts") or [] if float(p.get("amount") or 0) > 0]
    if not parts:
        raise _UserError("Не выбрана ни одна сделка или не указана сумма.")
    if len({p["deal_id"] for p in parts}) != len(parts):
        raise _UserError("Одна сделка выбрана дважды.")
    a, ids = _full_payment(cl, pid)   # перечитываем: за время работы страницы платёж могли разнести
    if ids["deal_id"]:
        raise _UserError("Этот платёж уже привязан к сделке — обновите страницу.")
    if a.get("direction") != "income":
        raise _UserError("Разносятся только входящие платежи.")
    amount = round(float(a["amount"]), 2)
    total = round(sum(p["amount"] for p in parts), 2)
    if total > amount + 0.005:
        raise _UserError(f"Сумма по сделкам ({total:,.2f} ₽) больше суммы платежа ({amount:,.2f} ₽).".replace(",", " "))
    rest = round(amount - total, 2)
    grp = uuid.uuid4().hex[:12]
    marker = f"[разнос {grp}]"
    base_attrs = {"direction": "income", "payment-type": a.get("payment-type") or "non_cash", "purpose": a.get("purpose") or "",
                  "operation-created-at": a.get("operation-created-at"), "operation-accounted-at": a.get("operation-accounted-at")}
    if a.get("customs"):
        base_attrs["customs"] = {k: v for k, v in a["customs"].items() if v not in (None, "")}
    created: list[str] = []
    orig_touched = False
    inv_set: list[str] = []   # счета, которым мы поставили сделку (Synergy требует, чтобы сделка платежа совпадала со сделкой счёта)
    try:
        for p in parts:
            if not p.get("invoice_id"):
                continue
            cur = cl.get(f"invoices/{p['invoice_id']}", include="deal")["data"]
            cur_deal = ((cur["relationships"].get("deal") or {}).get("data") or {}).get("id")
            if cur_deal and cur_deal != p["deal_id"]:
                raise _UserError(f"Счёт {p.get('invoice_number') or p['invoice_id']} уже привязан к другой сделке, чем выбрано для этой части. "
                                 "Выберите сделку этого счёта или другой счёт.")
            if not cur_deal:
                cl.patch(f"invoices/{p['invoice_id']}", {"data": {"type": "invoices", "id": p["invoice_id"], "relationships": {"deal": {"data": {"type": "deals", "id": p["deal_id"]}}}}})
                inv_set.append(p["invoice_id"])
        for p in parts[1:]:   # первая часть останется в исходном платеже
            attrs = {**base_attrs, "amount": p["amount"], "status": "executed",
                     "description": f"{marker} Часть платежа №{a.get('number')} на {amount:,.2f} ₽, разнесена по сделке №{p.get('number')}.".replace(",", " ")}
            new = _create_payment(cl, attrs, _rel(ids, p["deal_id"]), marker)
            created.append(new)
            if p.get("invoice_id"):   # сделка и счёт одним запросом Synergy не принимает (422) — счёт отдельным
                cl.patch(f"invoice-payments/{new}", {"data": {"type": "invoice-payments", "id": new, "relationships": {"invoice": {"data": {"type": "invoices", "id": p["invoice_id"]}}}}})
            conn.execute("INSERT INTO pay_allocations (grp,ts,user,action,payment_id,deal_id,deal_number,amount,new_payment_id,invoice_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (grp, _now(), user, "part", pid, p["deal_id"], p.get("number"), p["amount"], new, p.get("invoice_id") or None))
            conn.commit()
        if rest > 0.005:   # нераспределённый остаток остаётся отдельным платежом без сделки
            attrs = {**base_attrs, "amount": rest, "status": "not_executed",
                     "description": f"{marker} Остаток платежа №{a.get('number')} на {amount:,.2f} ₽ после разноски: не разнесён, нужна сделка.".replace(",", " ")}
            new = _create_payment(cl, attrs, _rel(ids, None), marker)
            created.append(new)
            conn.execute("INSERT INTO pay_allocations (grp,ts,user,action,payment_id,amount,new_payment_id) VALUES (?,?,?,?,?,?,?)", (grp, _now(), user, "rest", pid, rest, new))
            conn.commit()
        first = parts[0]
        patch_attrs = {"status": "executed"}
        if len(parts) > 1 or rest > 0.005:
            patch_attrs["amount"] = first["amount"]
        marker_desc = (a.get("description") or "") + f" {marker} Разнесено: сделка №{first.get('number')}, {first['amount']:,.2f} ₽, {user}.".replace(",", " ")
        patch_attrs["description"] = marker_desc
        orig_touched = True
        cl.patch(f"invoice-payments/{pid}", {"data": {"type": "invoice-payments", "id": pid, "attributes": patch_attrs,
                                                      "relationships": {"deal": {"data": {"type": "deals", "id": first["deal_id"]}}}}})
        if first.get("invoice_id"):
            cl.patch(f"invoice-payments/{pid}", {"data": {"type": "invoice-payments", "id": pid, "relationships": {"invoice": {"data": {"type": "invoices", "id": first["invoice_id"]}}}}})
    except Exception:
        if orig_touched:   # откат исходного платежа: сумма, статус, без сделки и счёта
            try:
                cl.patch(f"invoice-payments/{pid}", {"data": {"type": "invoice-payments", "id": pid,
                         "attributes": {"amount": amount, "status": a.get("status") or "not_executed", "description": a.get("description") or ""},
                         "relationships": {"deal": {"data": None}, "invoice": {"data": None}}}})
            except Exception:   # noqa: BLE001
                logger.exception("откат исходного платежа %s", pid)
        for iid in inv_set:   # откат: счетам возвращаем «без сделки»
            try:
                cl.patch(f"invoices/{iid}", {"data": {"type": "invoices", "id": iid, "relationships": {"deal": {"data": None}}}})
            except Exception:   # noqa: BLE001
                logger.exception("откат: не удалось снять сделку со счёта %s", iid)
        for c in created:   # откат: исходный платёж не тронут, созданные части убираем
            try:
                _delete(cl, f"invoice-payments/{c}")
            except Exception:   # noqa: BLE001
                logger.exception("откат: не удалось удалить часть %s", c)
        conn.execute("DELETE FROM pay_allocations WHERE grp=?", (grp,))
        conn.commit()
        raise
    conn.execute("INSERT INTO pay_allocations (grp,ts,user,action,payment_id,deal_id,deal_number,amount,orig_amount,note,invoice_id,inv_deal_set) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (grp, _now(), user, "allocate", pid, first["deal_id"], first.get("number"), first["amount"], amount, a.get("description") or "",
                  first.get("invoice_id") or None, 1 if first.get("invoice_id") in inv_set else 0))
    for p in parts[1:]:   # у частей, созданных нами, отметка «сделку счёту поставили мы» для отмены
        if p.get("invoice_id") in inv_set:
            conn.execute("UPDATE pay_allocations SET inv_deal_set=1 WHERE grp=? AND invoice_id=? AND action='part'", (grp, p["invoice_id"]))
    conn.execute("DELETE FROM pay_flags WHERE payment_id=?", (pid,))
    conn.commit()
    sums = {}
    for p in parts:
        try:
            sums[p["deal_id"]] = _recount_deal(cl, p["deal_id"])
        except Exception:   # noqa: BLE001 — таймер досчитает через 10 минут
            logger.exception("пересчёт суммы платежей сделки %s", p["deal_id"])
    deal_info(cl, conn, [p["deal_id"] for p in parts], force=True)
    for iid in {p["invoice_id"] for p in parts if p.get("invoice_id")}:
        _touch_invoice(cl, iid)
    _refresh_invoice_rows(cl, conn, [p["invoice_id"] for p in parts if p.get("invoice_id")])
    _refresh_rows(cl, conn, [pid, *created])
    return {"ok": True, "group": grp, "created": created, "rest": rest, "paid": sums}


def _undo(settings: Settings, pid: str, body: dict) -> dict:
    conn = _db(settings)
    cl = _client(settings)
    user = str(body.get("user") or "").strip()[:60] or "не указано"
    head = conn.execute("SELECT * FROM pay_allocations WHERE payment_id=? AND action='allocate' AND undone=0 ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    if not head:
        raise _UserError("Для этого платежа нет разноски, сделанной через эту страницу.")
    grp = head["grp"]
    others = conn.execute("SELECT * FROM pay_allocations WHERE grp=? AND action IN ('part','rest')", (grp,)).fetchall()
    for o in others:   # части уже могли изменить вручную — тогда отмена опасна
        try:
            cur = cl.get(f"invoice-payments/{o['new_payment_id']}", include="deal")
        except httpx.HTTPStatusError:
            continue
        if abs(float(cur["data"]["attributes"]["amount"]) - float(o["amount"])) > 0.005:
            raise _UserError(f"Часть платежа №{cur['data']['attributes'].get('number')} уже изменена вручную. Отмените вручную в Synergy.")
    deals = {head["deal_id"], *[o["deal_id"] for o in others if o["deal_id"]]}
    for o in others:
        _delete(cl, f"invoice-payments/{o['new_payment_id']}")
    desc = (head["note"] or "")
    inv_ids = {r["invoice_id"] for r in [head, *others] if r["invoice_id"]}
    prel = {"deal": {"data": None}}
    if head["invoice_id"]:
        prel["invoice"] = {"data": None}
    cl.patch(f"invoice-payments/{pid}", {"data": {"type": "invoice-payments", "id": pid,
             "attributes": {"amount": head["orig_amount"], "status": "not_executed", "description": desc}, "relationships": prel}})
    for r in [head, *others]:   # сделку, которую мы поставили счёту при разноске, снимаем
        if r["invoice_id"] and r["inv_deal_set"]:
            try:
                cl.patch(f"invoices/{r['invoice_id']}", {"data": {"type": "invoices", "id": r["invoice_id"], "relationships": {"deal": {"data": None}}}})
            except Exception:   # noqa: BLE001
                logger.exception("снятие сделки со счёта %s", r["invoice_id"])
    for iid in inv_ids:
        _touch_invoice(cl, iid)
    _refresh_invoice_rows(cl, conn, list(inv_ids))
    back, _ = _full_payment(cl, pid)
    if back.get("status") != "not_executed":   # Synergy не всегда принимает смену статуса вместе со связью — повторяем отдельно
        cl.patch(f"invoice-payments/{pid}", {"data": {"type": "invoice-payments", "id": pid, "attributes": {"status": "not_executed"}}})
    conn.execute("UPDATE pay_allocations SET undone=1 WHERE grp=?", (grp,))
    conn.execute("INSERT INTO pay_allocations (grp,ts,user,action,payment_id,amount) VALUES (?,?,?,?,?,?)", (grp, _now(), user, "undo", pid, head["orig_amount"]))
    conn.commit()
    for d in deals:
        try:
            _recount_deal(cl, d)
        except Exception:   # noqa: BLE001
            logger.exception("пересчёт после отмены %s", d)
    conn.execute("DELETE FROM pay_cache WHERE id IN (%s)" % ",".join("?" * len(others)), tuple(o["new_payment_id"] for o in others)) if others else None
    if deals:
        deal_info(cl, conn, [d for d in deals if d], force=True)
    _refresh_rows(cl, conn, [pid])
    return {"ok": True}
