#!/usr/bin/env python
"""Ежедневный отчёт: кто из менеджеров сколько добавил карточек техники
в раздел «Транспорт» CRM Synergy — в группу Telegram «Транспорт СРМ».

    ./scripts/transport_daily_report.py                 за вчера, в группу
    ./scripts/transport_daily_report.py --date 2026-11-02
    ./scripts/transport_daily_report.py --no-send        только посчитать и напечатать

Запускается таймером `dashboard-transport-report` в 08:00 по Москве — уже
после ночной пересинхронизации справочника техники (`dashboard-transport-sync`,
каждые 15 минут) и после смены, за которую накопились карточки. Считает
полные сутки, а не «с полуночи до сейчас» — иначе отчёт в 08:00 отражал бы
только первые полчаса дня.

Группа заведена 03.11.2026 (владелец попросил), бот — тот же сторож
(@technores_storozh_bot), токен и общая механика отправки — из
stream_watchdog.py, chat_id — свой, DASH_TRANSPORT_REPORT_CHAT_ID.

Источник данных — локальная копия transport_cards (sync_transports.py),
поле responsible_name, подтянутое оттуда же 03.11.2026. У карточек старше
конца октября поле часто пустое (в CRM тогда его не писали) — это не
ошибка синхронизации, у свежих карточек покрытие 100%.

Выгрузка за произвольный период — не здесь, а командой `/период ДД.ММ ДД.ММ`
прямо в группе «Транспорт СРМ» (слушает report_bot.py, тот же процесс, что
отвечает на кнопку «Отчёт» в канале сторожа). Этот скрипт — только для
автоматической ежедневной сводки за вчера.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx                                      # noqa: E402

from app.config import get_settings                # noqa: E402
from app.db import connect, init_schema             # noqa: E402
from app.transport_report import build_report       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="Дата в формате ГГГГ-ММ-ДД, по умолчанию вчера.")
    ap.add_argument("--no-send", action="store_true", help="Только посчитать, не слать в Telegram.")
    args = ap.parse_args()

    settings = get_settings()
    for_date = args.date or (date.today() - timedelta(days=1)).isoformat()

    conn = connect(settings.db_path)
    init_schema(conn)
    text = build_report(conn, for_date, for_date)
    conn.close()

    print(text)

    if args.no_send:
        return 0

    if not settings.watchdog_bot_token or not settings.transport_report_chat_id:
        print("DASH_WATCHDOG_BOT_TOKEN или DASH_TRANSPORT_REPORT_CHAT_ID не заданы — не отправлено", file=sys.stderr)
        return 1

    r = httpx.post(
        f"https://api.telegram.org/bot{settings.watchdog_bot_token}/sendMessage",
        data={"chat_id": settings.transport_report_chat_id, "text": text},
        timeout=20,
    )
    r.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
