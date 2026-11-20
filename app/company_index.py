"""Надёжный поиск компании Synergy по ИНН — чтобы не плодить дубли.

Почему не `filter[inn]`: он не находит часть компаний (загруженные пачкой 15.10.2026, ИНН на 99… и др.) — наши скрипты платежей
и счетов тогда заводили вторую карточку (выяснено 19.11.2026: 8 из 35 созданных нами компаний — дубли). Поэтому держим свою копию
справочника компаний в таблице `company_index` (id, ИНН, КПП, название) и ищем в ней; `filter[inn]` — лишь дополнительный источник.

Копия обновляется двумя способами: `refresh_new` — быстро, идём по свежим компаниям (`sort=-created-at`) до первой знакомой;
`upsert_page`/`refresh_full` — полный проход (раз в неделю его делает sync_balances; правка ИНН у старой карточки ловится только им).
Созданную нами компанию сразу запоминаем (`remember`), не дожидаясь обновления.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

TABLE = """CREATE TABLE IF NOT EXISTS company_index(
    id TEXT PRIMARY KEY, inn TEXT, kpp TEXT, name TEXT, created TEXT, archived INT DEFAULT 0, seen_at REAL)"""
INDEX = "CREATE INDEX IF NOT EXISTS ix_company_index_inn ON company_index(inn)"
_last_refresh = 0.0


def digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v or ""))


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE)
    conn.execute(INDEX)
    conn.commit()


def upsert(conn: sqlite3.Connection, c: dict) -> None:
    a = c.get("attributes") or {}
    conn.execute("INSERT OR REPLACE INTO company_index VALUES (?,?,?,?,?,?,?)",
                 (str(c["id"]), digits(a.get("inn")), digits(a.get("kpp")), a.get("name") or a.get("full-name") or "",
                  a.get("created-at") or "", 1 if a.get("archived-at") else 0, time.time()))


def remember(conn: sqlite3.Connection, comp_id: str, inn: str, name: str = "", kpp: str = "") -> None:
    ensure(conn)
    conn.execute("INSERT OR REPLACE INTO company_index VALUES (?,?,?,?,?,?,?)",
                 (str(comp_id), digits(inn), digits(kpp), name, time.strftime("%Y-%m-%dT%H:%M:%S"), 0, time.time()))
    conn.commit()


def refresh_new(cl, conn: sqlite3.Connection, max_pages: int = 20, min_gap_sec: float = 60.0) -> int:
    """Подтянуть свежесозданные компании: страницы по `-created-at`, пока не встретим страницу, где всё уже известно."""
    global _last_refresh
    ensure(conn)
    if time.time() - _last_refresh < min_gap_sec:
        return 0
    known = {r[0] for r in conn.execute("SELECT id FROM company_index")}
    added = 0
    for page in range(1, max_pages + 1):
        rows = cl.get("companies", **{"sort": "-created-at", "page[number]": page}).get("data") or []
        if not rows:
            break
        new = [c for c in rows if str(c["id"]) not in known]
        for c in new:
            upsert(conn, c)
        added += len(new)
        if len(new) < len(rows):   # дошли до уже известных
            break
    conn.commit()
    _last_refresh = time.time()
    return added


def companies_by_inn(cl, conn: sqlite3.Connection, inn: str, *, refresh: bool = True, use_api: bool = True) -> list[dict]:
    """Все карточки с этим ИНН в виде словарей как у Synergy ({id, attributes:{name, inn, kpp, created-at}}). Архивные включены."""
    ensure(conn)
    k = digits(inn)
    if not k:
        return []
    if refresh:
        try:
            refresh_new(cl, conn)
        except Exception:  # noqa: BLE001 — копия не обязана быть свежей, поиск не должен падать
            pass
    found: dict[str, dict] = {}
    for cid, i, kpp, name, created in conn.execute("SELECT id, inn, kpp, name, created FROM company_index WHERE inn=?", (k,)):
        found[cid] = {"type": "companies", "id": cid, "attributes": {"inn": i, "kpp": kpp, "name": name, "created-at": created}}
    if use_api:
        try:
            for c in cl.get("companies", per_page=20, **{"filter[inn]": k}).get("data") or []:
                if digits((c.get("attributes") or {}).get("inn")) == k:
                    found[str(c["id"])] = c
        except Exception:  # noqa: BLE001
            pass
    return list(found.values())
