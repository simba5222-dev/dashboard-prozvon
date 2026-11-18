"""Поиск техники менеджерами продаж: звонок -> заявка, цена, карточки транспорта.

Менеджер отдела продаж получает заявку на технику и обзванивает исполнителей.
Разбор самого разговора (что у исполнителя есть, цена, занято ли) — общий с
отделом поиска (`search_calls`). Здесь то, чего у отдела поиска нет:

* **Привязка звонка к заявке.** В Synergy она есть — поле звонка `custom-11360`
  «Заявки»; сейчас его вручную проставляет помощник. Через API списка связей
  нет, поэтому копию держим у себя (`call_order_links`). Связи помощника —
  эталон, по которому первую неделю проверяем нашу привязку.
* **Карточки транспорта.** Что исполнитель назвал своим, а в базе «Транспорт»
  этого нет — предлагаем (`propose`) или, когда владелец включит, создаём со
  статусом «Добавлено ИИ» (`create`).

Жёсткое правило потока: цену исполнителя не писать в поля заявки заказчика —
это чужая ставка. Цены лежат только в нашей базе и на экране дашборда.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app import knowledge
from app.collector import SynergyClient, order_detail, orders_by_sort
from app.config import Settings
from app.crm_write import CrmWriter, STATUS_AI_ADDED
from app.db import (
    get_state, mark_ai_transport_created, phone10 as to_phone10, save_ai_transport_add,
    save_call_order_link, save_order_index, save_transport_card, set_search_check_order,
    set_state,
)

logger = logging.getLogger(__name__)

FIELD_EQUIPMENT = "custom-18621"
INDEX_MARK = "order_index_mark"
HELPER_LINK_FIELD = "custom-11360"

HIGH = "высокая"
MEDIUM = "средняя"


def _parse(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def equipment_of(customs: dict[str, Any]) -> str:
    """Тип техники заявки строкой (в Synergy это список)."""
    value = (customs or {}).get(FIELD_EQUIPMENT)
    if isinstance(value, list):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def refresh_order_index(conn: sqlite3.Connection, client: SynergyClient,
                        stages: dict[str, tuple[str, str]], *, days: int = 14,
                        max_pages: int = 40) -> int:
    """Пополнить индекс заявок: только изменившиеся и только свежие и открытые.

    Листать все заявки каждый раз нельзя: лимит запросов к Synergy общий на
    аккаунт. Идём по `-updated-at` до водяного знака прошлого прогона и читаем
    заявку целиком только если у неё поменялось время изменения. Закрытые и
    старше окна не нужны: под них звонки уже не привязываем.
    """
    now = datetime.now(timezone.utc)
    mark = _parse(get_state(conn, INDEX_MARK))
    boundary = (mark - timedelta(hours=1)) if mark else (now - timedelta(days=days))
    rows = orders_by_sort(client, sort="-updated-at", boundary=boundary, max_pages=max_pages)
    known = {r["order_id"]: r["updated_at"]
             for r in conn.execute("SELECT order_id, updated_at FROM order_index")}
    oldest = now - timedelta(days=days)
    fetched = 0
    for row in rows:
        created = _parse(row["created_at"])
        if created is None or created < oldest:
            continue
        _name, kind = stages.get(row["stage_id"], ("", ""))
        if kind in ("won", "lost") and row["id"] not in known:
            continue
        if known.get(row["id"]) == row["updated_at"]:
            continue
        detail = order_detail(client, row["id"], stages)
        if not detail:
            continue
        save_order_index(
            conn, order_id=detail["order_id"], number=str(detail.get("number") or ""),
            responsible_id=detail.get("responsible_id") or "",
            equipment=equipment_of(detail["customs"]),
            stage_kind=detail.get("stage_kind") or "",
            created_at=detail["created_at"], updated_at=detail["updated_at"],
            fetched_at=now.isoformat(),
        )
        fetched += 1
    conn.commit()
    set_state(conn, INDEX_MARK, now.isoformat())
    return fetched


def pull_helper_links(conn: sqlite3.Connection, client: SynergyClient, *,
                      days: int = 3, max_pages: int = 150) -> int:
    """Связи «звонок — заявка», которые помощник проставил в Synergy вручную.

    Это эталон для сверки нашей привязки. Списка связей через API нет, поэтому
    листаем свежие звонки и смотрим `custom-11360` у каждого.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    found = 0
    for page in range(1, max_pages + 1):
        data = client.get("telephony-calls", sort="-created-at",
                          **{"page[number]": page}).get("data") or []
        if not data:
            break
        reached_old = False
        for call in data:
            attrs = call.get("attributes") or {}
            created = _parse(attrs.get("created-at"))
            if created is not None and created < since:
                reached_old = True
                continue
            value = (attrs.get("customs") or {}).get(HELPER_LINK_FIELD)
            if value in (None, "", []):
                continue
            order_id = str(value[0] if isinstance(value, list) else value)
            # В нашей таблице звонок лежит либо под кодом ВАТС (`call-id`), либо под числовым
            # id Synergy (так хранит большинство строк): искать нужно по обоим, иначе связи
            # помощника молча не находятся (нашли 16.11.2026: 31 связь, ни одна не подхватилась).
            uid = next((u for u in (attrs.get("call-id"), str(call.get("id") or ""))
                        if u and conn.execute("SELECT 1 FROM calls WHERE uid = ?", (u,)).fetchone()), None)
            if not uid:
                continue
            save_call_order_link(conn, uid, order_id, "helper",
                                 synergy_call_id=str(call["id"]), confidence="помощник",
                                 at=now.isoformat())
            found += 1
        if reached_old:
            break
    conn.commit()
    return found


def _families(equipment: str) -> set[str]:
    return {f for f in (knowledge.тип_техники(x.strip()) for x in equipment.split(";")) if f}


def choose_order(conn: sqlite3.Connection, *, synergy_user: str, started_at: str,
                 asked_type: str, max_age_days: int = 14,
                 only_open: bool = True) -> dict[str, Any]:
    """Под какую заявку менеджер звонил исполнителю. Приближённо, как у потока 4.

    Так делает помощник (по шести его примерам): звонок менеджера ложится на
    заявку, где он ответственный и которая создана раньше звонка. Тип техники из
    разговора против типа заявки: совпал — это та заявка; не совпал — не она;
    не опознали какой-то из двух — не знаем. Пишем сами только когда подходит
    ровно одна заявка с совпавшей техникой (`высокая`); остальное — предложение.
    """
    when = _parse(started_at)
    if when is None or not synergy_user:
        return {"order_id": None, "confidence": "", "note": "нет времени звонка или менеджера",
                "candidates": []}
    since = when - timedelta(days=max_age_days)
    asked = knowledge.тип_техники(asked_type)
    positive: list[sqlite3.Row] = []
    unknown: list[sqlite3.Row] = []
    for row in conn.execute("SELECT * FROM order_index WHERE responsible_id = ?", (synergy_user,)):
        created = _parse(row["created_at"])
        if created is None or created > when or created < since:
            continue
        if only_open and row["stage_kind"] in ("won", "lost"):
            continue
        families = _families(row["equipment"] or "")
        if asked and families:
            if asked in families:
                positive.append(row)
            continue  # техника заявки названа и это другая техника — не она
        unknown.append(row)

    newest = lambda rows: sorted(rows, key=lambda r: r["created_at"])[-1]  # noqa: E731
    if len(positive) == 1:
        return {"order_id": positive[0]["order_id"], "confidence": HIGH,
                "note": "ответственный и техника совпали",
                "candidates": [positive[0]["order_id"]]}
    if len(positive) > 1:
        pick = newest(positive)
        return {"order_id": pick["order_id"], "confidence": MEDIUM,
                "note": f"несколько заявок с этой техникой ({len(positive)}), взята самая свежая",
                "candidates": [r["order_id"] for r in positive]}
    if len(unknown) == 1:
        return {"order_id": unknown[0]["order_id"], "confidence": MEDIUM,
                "note": "у заявки не указана техника или тип не опознан",
                "candidates": [unknown[0]["order_id"]]}
    return {"order_id": None, "confidence": "",
            "note": "нет подходящей открытой заявки у менеджера" if not unknown
            else f"неоднозначно: {len(unknown)} заявок без опознанной техники",
            "candidates": [r["order_id"] for r in unknown]}


def number_category(conn: sqlite3.Connection, phone10: str) -> str:
    row = conn.execute("SELECT category FROM numbers WHERE phone10 = ?", (phone10,)).fetchone()
    return (row["category"] if row else "") or ""


def type_id_of(conn: sqlite3.Connection, type_name: str) -> str | None:
    row = conn.execute(
        "SELECT type_id FROM transport_cards WHERE type_name = ? AND type_id <> '' LIMIT 1",
        (type_name,)).fetchone()
    return str(row["type_id"]) if row else None


def propose_transport_adds(conn: sqlite3.Connection, settings: Settings,
                           writer: CrmWriter | None, *, uid: str, phone10: str,
                           heard: dict[str, Any], missing: list[str]) -> list[dict[str, Any]]:
    """Что исполнитель назвал своим, а в базе «Транспорт» этого нет.

    Условия: тип из справочника с цитатой (это уже сделал разбор), уверенность не
    ниже порога, номер не заказчика, у типа есть id. В режиме propose — только
    запись у себя; в create — карточка в Synergy. Пара номер+тип пишется в базу
    ДО отправки и после ошибки не снимается: создание не повторяется вслепую.
    """
    mode = settings.transport_ai_mode
    out: list[dict[str, Any]] = []
    if mode == "off" or not missing or len(phone10) < 10:
        return out
    sure = heard.get("sure")
    try:
        sure_n = int(sure)
    except (TypeError, ValueError):
        sure_n = 0
    # Самооценка «уверенности» у YandexGPT Lite не откалибрована: на пробных
    # звонках 15.11.2026 стояли 0 и 50 у вполне верных находок. Поэтому в режиме
    # propose (неделя наблюдения) показываем всё вместе с цифрой и цитатой, а порог
    # включаем только перед записью в Synergy.
    if mode == "create" and sure_n < settings.transport_ai_min_sure:
        return out
    if number_category(conn, phone10) == "заказчик":
        return out
    quotes = {item["type"]: item.get("quote", "") for item in heard.get("has") or []}
    now = datetime.now(timezone.utc).isoformat()
    for type_name in missing:
        type_id = type_id_of(conn, type_name)
        if not type_id:
            out.append({"type": type_name, "mode": "skipped", "note": "у типа нет id"})
            continue
        fresh = save_ai_transport_add(
            conn, call_uid=uid, phone10=phone10, type_name=type_name, type_id=type_id,
            quote=quotes.get(type_name, ""), sure=sure_n,
            mode="proposed" if mode == "propose" else "creating", at=now)
        if not fresh:
            continue
        conn.commit()
        item: dict[str, Any] = {"type": type_name, "mode": mode}
        if mode == "create" and writer is not None and not writer.dry_run:
            add_id = conn.execute(
                "SELECT id FROM ai_transport_adds WHERE phone10 = ? AND type_name = ?",
                (phone10, type_name)).fetchone()["id"]
            try:
                new_id = writer.create_transport(phone10, type_id)
                mark_ai_transport_created(conn, add_id, new_id,
                                          "" if new_id else "ответ без id")
                if new_id:
                    save_transport_card(
                        conn, id=new_id, name="", type_id=type_id, type_name=type_name,
                        phone10=phone10, contact_id="", contact_name="",
                        status=STATUS_AI_ADDED, updated_at=now, synced_at=now, call_link="")
                    item["transport_id"] = new_id
            except (httpx.HTTPError, ValueError) as exc:
                mark_ai_transport_created(conn, add_id, None, f"{type(exc).__name__}: {exc}"[:300])
                logger.warning("карточка %s/%s не создана: %s", phone10, type_name, exc)
            conn.commit()
        out.append(item)
    return out


def after_check(conn: sqlite3.Connection, settings: Settings, writer: CrmWriter | None,
                uid: str, result: dict[str, Any]) -> dict[str, Any]:
    """Всё, что идёт после разбора разговора менеджера продаж с исполнителем."""
    heard = result.get("heard") or {}
    asked = heard.get("asked") or ""
    if not asked and not (heard.get("has") or []):
        return {"search": False}  # менеджер технику не искал — в метрики не входит
    row = conn.execute(
        "SELECT c.started_at, c.client_phone, m.synergy_user, m.vats_login "
        "FROM calls c LEFT JOIN managers m ON m.vats_login = c.vats_login WHERE c.uid = ?",
        (uid,)).fetchone()
    if row is None:
        return {"search": True}
    out: dict[str, Any] = {"search": True}
    choice = choose_order(conn, synergy_user=str(row["synergy_user"] or ""),
                          started_at=row["started_at"], asked_type=asked,
                          max_age_days=settings.link_max_age_days)
    out["link"] = choice
    now = datetime.now(timezone.utc).isoformat()
    if choice["order_id"]:
        equipment = conn.execute("SELECT equipment FROM order_index WHERE order_id = ?",
                                 (choice["order_id"],)).fetchone()
        set_search_check_order(conn, uid, choice["order_id"],
                               (equipment["equipment"] if equipment else "") or "")
        save_call_order_link(conn, uid, choice["order_id"], "ai",
                             confidence=choice["confidence"], note=choice["note"], at=now)
        helper = conn.execute("SELECT source FROM call_order_links WHERE call_uid = ?",
                              (uid,)).fetchone()
        if (settings.link_ai_mode == "create" and choice["confidence"] == HIGH
                and writer is not None and not writer.dry_run
                and helper and helper["source"] != "helper"):
            call_id = writer.set_call_order_link(uid, choice["order_id"])
            if call_id:
                save_call_order_link(conn, uid, choice["order_id"], "ai",
                                     synergy_call_id=call_id, confidence=choice["confidence"],
                                     note=choice["note"], written_at=now, at=now)
    conn.commit()
    out["transport"] = propose_transport_adds(
        conn, settings, writer, uid=uid, phone10=to_phone10(row["client_phone"]) or "",
        heard=heard, missing=result.get("missing") or [])
    return out
