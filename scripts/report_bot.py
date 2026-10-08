#!/usr/bin/env python
"""Слушает нажатия кнопки «📊 Отчёт» в канале сторожа и шлёт сводку.

    sudo -u claude .venv/bin/python scripts/report_bot.py

Постоянный процесс (long polling getUpdates), не таймер: кнопке нужно
ответить сразу, а не раз в сколько-то минут. Запускается systemd-юнитом
dashboard-report-bot.service, Restart=always.

Канал — не личный чат, в него нельзя написать текстом и получить ответ
(только админы постят), поэтому единственный интерактивный вход — нажатие
инлайн-кнопки, которую к каждому своему сообщению прикладывает сторож
(ОТЧЁТ_КНОПКА в stream_watchdog.py). Эта кнопка на тех сообщениях остаётся
активной бессрочно — Telegram не отзывает её сам.

08.10.2026: просьба владельца — видеть по кнопке, сколько сегодня звонков
по каждому из четырёх потоков, сколько транскрибировано, разобрано, и
сколько ещё висит в обработке.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx                                      # noqa: E402

from app.config import get_settings                # noqa: E402
from app.daily_report import build_report           # noqa: E402
from app.db import connect, init_schema             # noqa: E402

logger = logging.getLogger("report_bot")

OFFSET_FILE = Path(__file__).resolve().parents[1] / "data" / "report_bot_offset.txt"


def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.write_text(str(offset))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    token = settings.watchdog_bot_token
    if not token:
        logger.error("DASH_WATCHDOG_BOT_TOKEN не задан — нечего слушать")
        return 1

    api = f"https://api.telegram.org/bot{token}"
    offset = _load_offset()
    logger.info("слушаю нажатия кнопки, offset=%s", offset)

    while True:
        try:
            r = httpx.get(f"{api}/getUpdates",
                          params={"offset": offset, "timeout": 50,
                                  "allowed_updates": '["callback_query"]'},
                          timeout=60)
            r.raise_for_status()
            updates = r.json().get("result", [])
        except httpx.HTTPError as exc:
            logger.warning("getUpdates не вышел: %s — пауза 10 с", exc)
            time.sleep(10)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq or cq.get("data") != "report":
                continue
            chat_id = cq["message"]["chat"]["id"]
            try:
                httpx.post(f"{api}/answerCallbackQuery",
                          data={"callback_query_id": cq["id"]}, timeout=15)
                conn = connect(settings.db_path)
                init_schema(conn)
                text = build_report(conn, settings)
                conn.close()
                httpx.post(f"{api}/sendMessage",
                          data={"chat_id": chat_id, "text": text}, timeout=20)
                logger.info("отчёт отправлен в чат %s", chat_id)
            except httpx.HTTPError as exc:
                logger.warning("не получилось ответить на кнопку: %s", exc)

        if updates:
            _save_offset(offset)


if __name__ == "__main__":
    sys.exit(main())
