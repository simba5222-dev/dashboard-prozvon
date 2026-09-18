"""Подсказка менеджеру: что он должен знать про звонящего в первые секунды.

Всплывающую карточку клиента CRM показывает сама — но в ней только то, что
менеджеры завели руками. А у нас лежит то, чего в карточке нет: о чём клиент
говорил в прошлые разы, что ему называли по цене, чем кончилось и какие
вопросы по его технике задать обязательно.

Подсказка нужна прежде всего новичку: он не помнит клиента и не знает, что
спрашивать про ямобур. Поэтому пишем коротко и по делу — карточка маленькая,
длинный текст в ней читать некогда.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Any

from app import knowledge

logger = logging.getLogger(__name__)

# Карточка узкая: больше тысячи знаков в ней не прочитают, а промотать вниз
# посреди разговора некогда.
MAX_LENGTH = 1100


def short_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%d.%m")
    except ValueError:
        return str(iso)[:10]


def digits(phone: str) -> str:
    return re.sub(r"\D", "", str(phone or ""))[-10:]


def build(conn: sqlite3.Connection, phone: str, orders: list[dict[str, Any]] | None = None) -> str:
    """Собрать подсказку по клиенту. Пустая строка — сказать нечего."""
    number = digits(phone)
    if not number:
        return ""

    calls = conn.execute(
        """
        SELECT k.uid, k.started_at, k.duration_sec, m.display_name,
               t.analysis_json, s.verdict_json
        FROM calls k
        LEFT JOIN managers m ON m.vats_login = k.vats_login
        LEFT JOIN transcripts t ON t.call_uid = k.uid
        LEFT JOIN screens s ON s.call_uid = k.uid
        WHERE REPLACE(REPLACE(REPLACE(k.client_phone, '+', ''), ' ', ''), '-', '') LIKE ?
          AND k.duration_sec >= 20
        ORDER BY k.started_at DESC
        LIMIT 12
        """,
        (f"%{number}",),
    ).fetchall()

    lines: list[str] = []
    equipment: list[str] = []

    # 1. О чём говорили в последний раз — самое ценное для новичка.
    for row in calls:
        analysis = _loads(row["analysis_json"])
        screen = _loads(row["verdict_json"])
        need = (screen.get("request") or analysis.get("client_need")
                or analysis.get("summary") or "")
        if not need:
            continue
        who = row["display_name"] or "менеджер"
        lines.append(f"Прошлый раз {short_date(row['started_at'])} ({who}): {need[:180]}")
        if screen.get("equipment"):
            equipment.append(str(screen["equipment"]))
        break

    # 2. Что брал и чем кончилось — по заявкам из CRM.
    if orders:
        done = [o for o in orders if o.get("kind") == "won"]
        lost = [o for o in orders if o.get("kind") == "lost"]
        opened = [o for o in orders if o.get("kind") not in {"won", "lost"}]
        parts = [f"заявок {len(orders)}"]
        if done:
            parts.append(f"сделок {len(done)}")
        if lost:
            parts.append(f"проиграно {len(lost)}")
        if opened:
            parts.append(f"в работе {len(opened)}: {'; '.join(o['name'] for o in opened[:2])[:90]}")
        lines.append("История: " + ", ".join(parts))
        for order in orders[:3]:
            if order.get("transport"):
                equipment.append(str(order["transport"]))
        prices = [o for o in orders if o.get("price")]
        if prices:
            lines.append("Называли раньше: " + ", ".join(
                f"{o['transport'] or 'техника'} — {o['price']}" for o in prices[:2]))

    # 3. Чего не спросили в прошлый раз — прямо из разбора.
    for row in calls:
        missed = _loads(row["analysis_json"]).get("missed") or []
        fields = [str(m.get("field")) for m in missed if isinstance(m, dict) and m.get("field")]
        if fields:
            lines.append("В прошлый раз не записали: " + ", ".join(dict.fromkeys(fields))[:120])
            break

    # 4. Чек-лист по его технике из учебника компании.
    questions = knowledge.questions_for(" ".join(equipment))
    if questions:
        lines.append("Спросить: " + "; ".join(q.rstrip("?") for q in questions[:5]))

    if not lines:
        return ""
    text = "\n".join(lines)
    return text[:MAX_LENGTH]


def orders_of(client: Any, contact_id: str, stages: dict[str, tuple[str, str]]) -> list[dict[str, Any]]:
    """Заявки контакта в виде, удобном для подсказки: что, когда, чем кончилось.

    Цены берём те, что уже записаны в заявке разбором: «называли клиенту» —
    самый частый вопрос новичка, а лезть за ним в прошлые заявки он не успеет.
    """
    try:
        payload = client.get(f"contacts/{contact_id}/orders",
                             include="stage", sort="-created-at", per_page=20)
    except Exception:  # noqa: BLE001 — без истории подсказка всё равно полезна
        return []
    out: list[dict[str, Any]] = []
    for row in payload.get("data") or []:
        attrs = row.get("attributes") or {}
        customs = attrs.get("customs") or {}
        stage_id = str((((row.get("relationships") or {}).get("stage") or {}).get("data") or {}).get("id") or "")
        stage_name, stage_kind = stages.get(stage_id, ("", ""))
        transport = customs.get("custom-18621")
        if isinstance(transport, list):
            transport = ", ".join(str(t) for t in transport)
        price = customs.get("custom-257") or customs.get("custom-14924")
        out.append({
            "name": str(attrs.get("name") or "заявка")[:40],
            "kind": stage_kind, "stage": stage_name,
            "transport": transport or "",
            "price": f"{int(float(price)):,}".replace(",", " ") + " ₽" if price else "",
        })
    return out


def _loads(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}
