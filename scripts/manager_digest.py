#!/usr/bin/env python
"""Сводный отчёт владельцу по менеджерам — ежедневно/еженедельно.

    sudo -u claude .venv/bin/python scripts/manager_digest.py --period daily            # сухой прогон
    sudo -u claude .venv/bin/python scripts/manager_digest.py --period daily --apply     # отправить
    sudo -u claude .venv/bin/python scripts/manager_digest.py --period weekly --apply

Зачем. Вовлечённость по заявке и разбор прозвона уже считаются (таймеры
`dashboard-engagement`/`dashboard-prozvon`), но ни то, ни другое владелец
не видит само — надо заходить и смотреть руками. Этот скрипт сводит уже
посчитанное (не разбирает звонки заново) в одну картину по каждому
менеджеру и отправляет её в тот же Telegram-канал «Сторож звонков», где
владелец уже реально читает алерты (`watchdog_bot_token`/`watchdog_chat_id`,
см. `scripts/stream_watchdog.py`).

«daily» — вчера, коротко. «weekly» — последние 7 дней, с заявками и
вовлечённостью (там разброс за сутки слишком мал, чтобы о чём-то говорить).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.collector import SynergyClient, load_stages  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_schema  # noqa: E402
from app.manager_digest import build_manager_stats, render_digest  # noqa: E402

logger = logging.getLogger(__name__)


def send_telegram(token: str, chat_id: int, text: str) -> None:
    r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   data={"chat_id": chat_id, "text": text}, timeout=15)
    r.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", choices=["daily", "weekly"], required=True)
    ap.add_argument("--apply", action="store_true", help="действительно отправить в Telegram")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        timeout_sec=30.0, min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries)
    stages = load_stages(client)

    now = datetime.now(timezone.utc)
    if args.period == "daily":
        until_dt = now
        since_dt = now - timedelta(days=1)
        period_label = "вчера"
    else:
        until_dt = now
        since_dt = now - timedelta(days=7)
        period_label = "последние 7 дней"
    since = since_dt.strftime("%Y-%m-%d")
    until = until_dt.strftime("%Y-%m-%d")

    logger.info("собираю сводку за %s (%s — %s)", period_label, since, until)
    stats = build_manager_stats(conn, client, stages, since, until)
    logger.info("менеджеров в своде: %d, заявок без ответственного: %d",
                len(stats["managers"]), stats["unmatched_orders"])

    engine = settings.manager_digest_engine
    api_key = {"claude": settings.anthropic_api_key,
              "yandex": settings.yandex_api_key,
              "openai": settings.openai_api_key}.get(engine)
    model = {"claude": settings.anthropic_model,
             "yandex": "yandexgpt", "openai": "gpt-4o"}.get(engine)
    if not api_key:
        logger.error("движок %s: ключ не настроен, свод не на чём писать", engine)
        conn.close()
        return 1

    text = render_digest(stats, period_label, engine=engine, api_key=api_key,
                         model=model, folder=settings.yandex_folder or "")
    header = f"📈 Сводка по менеджерам — {period_label}\n\n"
    text = header + text

    print(text)
    if args.apply:
        if not settings.watchdog_bot_token or not settings.watchdog_chat_id:
            logger.error("watchdog_bot_token/watchdog_chat_id не настроены — некуда слать")
            conn.close()
            return 1
        send_telegram(settings.watchdog_bot_token, settings.watchdog_chat_id, text)
        logger.info("отправлено в Telegram")
    else:
        logger.info("сухой прогон — не отправлено, см. текст выше (--apply чтобы отправить)")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
