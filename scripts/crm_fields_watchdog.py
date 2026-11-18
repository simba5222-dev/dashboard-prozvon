#!/usr/bin/env python
"""Сторож записи в CRM: дошли ли выжимка и рекомендации до Synergy.

    sudo -u claude .venv/bin/python scripts/crm_fields_watchdog.py            # сухой прогон
    sudo -u claude .venv/bin/python scripts/crm_fields_watchdog.py --apply    # слать алерты
    ... --hours 3      окно проверки (по умолчанию 3, как у ad_lines_watchdog)

Отдельно от stream_watchdog.py и ad_lines_watchdog.py: те проверяют, что наш
собственный разбор посчитался (локально, в своей базе или в журнале
сервиса). Здесь — что результат **дошёл до Synergy**: запись в CRM — это
отдельный сетевой шаг (PATCH orders), который может отвалиться сам, даже
если разбор на нашей стороне прошёл нормально.

Один и тот же признак работает сразу для всех потоков: заявка — настоящий
лид, если у неё заполнен тип техники (`custom-18621`), это уже
задокументированное правило (skill «синержи» — свежие заявки по дате не
всегда настоящие, отличаем по transport-type). Среди таких свежих заявок
смотрим, у скольких есть выжимка (`custom-30609`) и хотя бы одна из
рекомендаций (`custom-30600` РОПу, `custom-30601` менеджеру).

Порог — не доля, а счёт: при небольшом числе заявок за окно процент шумит.
Если из двух и более настоящих свежих заявок **ни у одной** нет выжимки или
рекомендаций — это и есть сигнал «запись в CRM отвалилась», не статистика.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.collector import SynergyClient  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.stream_watchdog import (  # noqa: E402
    _ensure_state_table, _mark_alerted, _mark_ok, _should_alert, send_telegram,
)

logger = logging.getLogger("crm_fields_watchdog")

FIELD_TRANSPORT = "custom-18621"
FIELD_SUMMARY = "custom-30609"
FIELD_ROP_REC = "custom-30600"
FIELD_MANAGER_REC = "custom-30601"

MIN_SAMPLE = 2  # меньше двух заявок за окно — рано делать выводы


def _recent_real_leads(client: SynergyClient, since: datetime) -> list[dict]:
    """Свежие настоящие заявки (с типом техники) среди первой страницы
    последних по дате — окну в несколько часов хватает одной страницы."""
    payload = client.get("orders", sort="-created-at", **{"page[number]": 1})
    out = []
    for row in payload.get("data") or []:
        attrs = row.get("attributes") or {}
        created_raw = str(attrs.get("created-at") or "")
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created < since:
            continue
        customs = attrs.get("customs") or {}
        if not customs.get(FIELD_TRANSPORT):
            continue
        out.append({
            "id": row.get("id"),
            "number": attrs.get("number"),
            "summary": str(customs.get(FIELD_SUMMARY) or "").strip(),
            "rop_rec": str(customs.get(FIELD_ROP_REC) or "").strip(),
            "manager_rec": str(customs.get(FIELD_MANAGER_REC) or "").strip(),
        })
    return out


def check_crm_fields(conn: sqlite3.Connection, settings, hours: int) -> list[tuple[str, str]]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        client = SynergyClient(
            base_url=settings.synergy_url, token=settings.synergy_api_token,
            timeout_sec=30.0, min_interval_sec=settings.synergy_min_interval_sec,
            retries=settings.synergy_retries)
        leads = _recent_real_leads(client, since)
    except Exception as exc:  # noqa: BLE001 — сторож не должен падать сам
        return [("crm_fields_io", f"🔴 CRM-сторож: заявки не прочитались — {exc}")]

    out: list[tuple[str, str]] = []
    if len(leads) < MIN_SAMPLE:
        _mark_ok(conn, "crm_summary")
        _mark_ok(conn, "crm_recs")
        return out

    no_summary = [l for l in leads if not l["summary"]]
    no_rec = [l for l in leads if not l["rop_rec"] and not l["manager_rec"]]

    window_txt = f"за {hours} ч, настоящих заявок (с типом техники) {len(leads)}"
    if len(no_summary) == len(leads):
        out.append(("crm_summary",
                    f"🔴 Выжимка не доходит до Synergy: {window_txt}, ни у одной нет "
                    f"custom-30609. Разбор может идти, а запись в CRM — нет."))
    else:
        _mark_ok(conn, "crm_summary")

    if len(no_rec) == len(leads):
        out.append(("crm_recs",
                    f"🔴 Рекомендации не доходят до Synergy: {window_txt}, ни у одной нет "
                    f"ни custom-30600, ни custom-30601."))
    else:
        _mark_ok(conn, "crm_recs")

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=3, help="окно проверки, часов")
    ap.add_argument("--apply", action="store_true", help="слать алерты в Telegram")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    settings = get_settings()
    conn = sqlite3.connect(settings.db_path)
    _ensure_state_table(conn)

    problems = check_crm_fields(conn, settings, args.hours)
    if not problems:
        print("всё в порядке")
        return 0

    bot_ready = bool(settings.watchdog_bot_token and settings.watchdog_chat_id)
    for key, msg in problems:
        if not _should_alert(conn, key):
            print(f"[{key}] проблема есть, но алерт недавно уже был — молчу")
            continue
        print(msg)
        if args.apply:
            if not bot_ready:
                logger.error("нет токена/chat_id сторожа")
                continue
            try:
                send_telegram(settings.watchdog_bot_token, settings.watchdog_chat_id, msg)
                _mark_alerted(conn, key)
            except httpx.HTTPError as exc:
                logger.error("не отправилось: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
