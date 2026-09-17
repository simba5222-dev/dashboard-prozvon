#!/usr/bin/env python
"""Собрать данные из Synergy за день.

    scripts/collect.py                 за сегодня
    scripts/collect.py --day 2026-09-15
    scripts/collect.py --days 7        за последнюю неделю
    scripts/collect.py --no-cards      только звонки, без проверки карточек

Запускается по расписанию и вручную. Повторный запуск безопасен: звонки
различаются по идентификатору Synergy, проверки карточек перезаписываются.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.collector import (  # noqa: E402
    SynergyClient, check_cards, check_pending_cards, collect_calls, collect_range,
    refresh_tasks, sync_managers,
)
from app.config import get_settings  # noqa: E402
from app.db import connect, init_schema  # noqa: E402
from app.stats import local_now  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Сбор данных дашборда из Synergy.")
    ap.add_argument("--day", help="дата в виде ГГГГ-ММ-ДД, по умолчанию сегодня")
    ap.add_argument("--days", type=int, default=1, help="сколько дней назад захватить")
    ap.add_argument("--no-cards", action="store_true", help="не проверять карточки")
    ap.add_argument("--cards-limit", type=int, default=0, help="ограничить число проверок")
    ap.add_argument("--catch-up", type=int, default=0,
                    help="догнать столько непроверенных карточек за прошлые дни")
    ap.add_argument("--tasks-only", action="store_true",
                    help="только пересобрать задачи по уже проверенным звонкам "
                         "за период (дёшево: один список задач вместо обхода карточек)")
    ap.add_argument("--refresh", action="store_true",
                    help="перепроверить и те карточки, что проверялись без "
                         "данных для развёрнутого отчёта (имя, компания, заявки)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.synergy_api_token:
        print("не задан DASH_SYNERGY_API_TOKEN")
        return 1

    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token, timeout_sec=30.0,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries,
    )

    sync_managers(conn, client, settings.synergy_group)

    last = date.fromisoformat(args.day) if args.day else local_now(settings.timezone_offset_hours).date()

    if args.tasks_only:
        since = (last - timedelta(days=max(args.days - 1, 0))).isoformat()
        found = refresh_tasks(conn, client, settings, since, last.isoformat())
        print(f"{since}…{last}: задач привязано к звонкам {found}")
        conn.close()
        return 0

    if args.days > 2:
        # За несколько дней выгоднее один проход: Synergy отдаёт звонки
        # только от свежих к старым, и для каждого дня пришлось бы листать заново.
        since = (last - timedelta(days=args.days - 1)).isoformat()
        new, seen = collect_range(conn, client, settings, since, last.isoformat())
        print(f"{since}…{last}: звонков менеджеров {seen}, новых {new}")
        if not args.no_cards:
            for back in range(args.days):
                day = (last - timedelta(days=back)).isoformat()
                done = check_cards(conn, client, settings, day,
                                   limit=args.cards_limit, refresh=args.refresh)
                if done:
                    print(f"{day}: проверено карточек {done}")
        conn.close()
        return 0

    for back in range(args.days):
        day = (last - timedelta(days=back)).isoformat()
        new, seen = collect_calls(conn, client, settings, day)
        print(f"{day}: звонков менеджеров {seen}, новых {new}")
        if not args.no_cards:
            done = check_cards(conn, client, settings, day,
                               limit=args.cards_limit, refresh=args.refresh)
            print(f"{day}: проверено карточек {done}")

    if args.catch_up:
        caught = check_pending_cards(conn, client, settings, args.catch_up,
                                     refresh=args.refresh)
        if caught:
            print(f"догнано карточек за прошлые дни: {caught}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
