#!/usr/bin/env python
"""Слушает нажатия кнопки «📊 Отчёт» в канале сторожа — и команду «/период»
в группе «Транспорт СРМ» — и шлёт сводку в ответ.

    sudo -u claude .venv/bin/python scripts/report_bot.py

Постоянный процесс (long polling getUpdates), не таймер: отвечать нужно
сразу, а не раз в сколько-то минут. Запускается systemd-юнитом
dashboard-report-bot.service, Restart=always. Один процесс на один токен
бота (@technores_storozh_bot) — у Telegram getUpdates только один
слушатель одновременно, поэтому обе задачи в одном месте, а не в двух
сервисах.

Канал сторожа — не личный чат, в него нельзя написать текстом и получить
ответ (только админы постят), поэтому там единственный интерактивный вход —
нажатие инлайн-кнопки, которую к каждому своему сообщению прикладывает
сторож (ОТЧЁТ_КНОПКА в stream_watchdog.py). Эта кнопка на тех сообщениях
остаётся активной бессрочно — Telegram не отзывает её сам.

08.10.2026: просьба владельца — видеть по кнопке, сколько сегодня звонков
по каждому из четырёх потоков, сколько транскрибировано, разобрано, и
сколько ещё висит в обработке.

«Транспорт СРМ» (группа, не канал, бот там админ — заведена 03.11.2026) —
обычные текстовые команды доходят, поэтому выгрузка за период не через
кнопку, а командой `/период ДД.ММ ДД.ММ` (год по умолчанию — текущий,
можно и `ДД.ММ.ГГГГ`). Ежедневная сводка за вчера всё равно продолжает
уходить сама, таймером (`scripts/transport_daily_report.py`) — команда
только для «а покажи за октябрь» по запросу.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx                                      # noqa: E402

from app.collector import SynergyClient             # noqa: E402
from app.config import get_settings                # noqa: E402
from app.daily_report import build_report           # noqa: E402
from app.db import connect, init_schema             # noqa: E402
from app.narabotka_report import build_report as build_narabotka_report  # noqa: E402
from app.transport_report import build_report as build_transport_report  # noqa: E402
from app.transport_report import parse_ru_date      # noqa: E402

logger = logging.getLogger("report_bot")

PERIOD_USAGE = ("Формат: /период ДД.ММ ДД.ММ (например /период 01.10 31.10) "
                "или с годом: /период 01.10.2026 31.10.2026")
NARABOTKA_USAGE = ("Формат: /наработка [ДД.ММ] — без даты проверяет вчера, "
                    "с датой — конкретный день (например /наработка 04.11)")

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
    logger.info("слушаю нажатия кнопки и команду /период, offset=%s", offset)

    while True:
        try:
            r = httpx.get(f"{api}/getUpdates",
                          params={"offset": offset, "timeout": 50,
                                  "allowed_updates": '["callback_query","message"]'},
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
            if cq and cq.get("data") == "report":
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
                continue

            msg = upd.get("message")
            if not msg or "text" not in msg:
                continue
            chat_id = msg["chat"]["id"]
            if chat_id != settings.transport_report_chat_id:
                continue
            command = msg["text"].strip().split()
            if not command:
                continue
            cmd_name = command[0].split("@")[0].lower()
            if cmd_name == "/период":
                try:
                    if len(command) != 3:
                        reply = PERIOD_USAGE
                    else:
                        this_year = date.today().year
                        date_from = parse_ru_date(command[1], default_year=this_year)
                        date_to = parse_ru_date(command[2], default_year=this_year)
                        if not date_from or not date_to:
                            reply = f"Не разобрал даты. {PERIOD_USAGE}"
                        elif date_from > date_to:
                            reply = f"Дата начала позже даты окончания. {PERIOD_USAGE}"
                        else:
                            conn = connect(settings.db_path)
                            init_schema(conn)
                            reply = build_transport_report(conn, date_from, date_to)
                            conn.close()
                    httpx.post(f"{api}/sendMessage",
                              data={"chat_id": chat_id, "text": reply,
                                    "reply_to_message_id": msg["message_id"]}, timeout=20)
                    logger.info("/период обработан в чате %s: %s", chat_id, command[1:])
                except httpx.HTTPError as exc:
                    logger.warning("не получилось ответить на /период: %s", exc)
            elif cmd_name == "/наработка":
                try:
                    if len(command) > 2:
                        reply = NARABOTKA_USAGE
                    elif len(command) == 2:
                        parsed = parse_ru_date(command[1], default_year=date.today().year)
                        if not parsed:
                            reply = f"Не разобрал дату. {NARABOTKA_USAGE}"
                        else:
                            reply = None
                            check_date = date.fromisoformat(parsed)
                    else:
                        check_date = date.today() - timedelta(days=1)
                        reply = None
                    if reply is None:
                        synergy = SynergyClient(base_url=settings.synergy_url,
                                                token=settings.synergy_api_token)
                        reply = build_narabotka_report(synergy, check_date)
                    httpx.post(f"{api}/sendMessage",
                              data={"chat_id": chat_id, "text": reply,
                                    "reply_to_message_id": msg["message_id"]}, timeout=20)
                    logger.info("/наработка обработан в чате %s: %s", chat_id, command[1:])
                except httpx.HTTPError as exc:
                    logger.warning("не получилось ответить на /наработка: %s", exc)
                except Exception as exc:                           # noqa: BLE001
                    logger.exception("/наработка сломался на необработанной ошибке: %s", exc)
                    try:
                        httpx.post(f"{api}/sendMessage",
                                  data={"chat_id": chat_id,
                                        "text": "Не получилось собрать отчёт — техническая ошибка, уже разбираюсь.",
                                        "reply_to_message_id": msg["message_id"]}, timeout=20)
                    except httpx.HTTPError:
                        pass

        if updates:
            _save_offset(offset)


if __name__ == "__main__":
    sys.exit(main())
