#!/usr/bin/env python
"""Дозаполнить «выжимку» в заявках, где разбор есть, а поле пустое.

    sudo -u claude .venv/bin/python scripts/backfill_summary.py            сухой прогон
    sudo -u claude .venv/bin/python scripts/backfill_summary.py --apply    записать
    ... --days 30                                                         глубже по времени

Поле «выжимка» завели 17.09.2026. Заявки «Пойманная с прослушки» его получают
с первого дня, а у заявок, которые CRM заводит по звонку с рекламы, поле
оставалось пустым: боевой сервер о нём не знал. Он уже исправлен, но прошлые
заявки сами не наполнятся.

Сам разбор заново не считается — всё уже лежит в заявке: расшифровка,
рекомендации, оценка, тип техники. Не хватало только краткого «о чём запрос»:
он есть в комментарии, который оставил разбор. Оттуда и берём.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crm_write import CrmWriter  # noqa: E402

logger = logging.getLogger("summary")

SUMMARY = "custom-30609"
TRANSCRIPT = "custom-30599"
MANAGER_RECS = "custom-30601"
SCORE = "custom-30602"
TRANSPORT = "custom-18621"
ADDRESS = "custom-255"
OUR_PRICE = "custom-257"
CLIENT_PRICE = "custom-14924"

# «Автоматический анализ звонка (312 сек., 2026-09-18T06:52:01+00:00):\n\n<текст>»
ANALYSIS_HEAD = re.compile(r"^Автоматический анализ звонка\s*\([^)]*\):\s*", re.S)


def summary_from(attrs: dict) -> str:
    """Собрать выжимку из того, что уже записано в заявке."""
    customs = attrs.get("customs") or {}
    lines: list[str] = []

    comment = str(attrs.get("comment") or "").replace("<br>", "\n").strip()
    request = ANALYSIS_HEAD.sub("", comment).strip() if ANALYSIS_HEAD.match(comment) else ""
    if request:
        lines.append(f"Запрос: {request}")

    transport = customs.get(TRANSPORT)
    if isinstance(transport, list):
        transport = ", ".join(str(t) for t in transport)
    if transport:
        lines.append(f"Техника: {transport}")
    if customs.get(ADDRESS):
        lines.append(f"Объект: {customs[ADDRESS]}")
    if customs.get(CLIENT_PRICE) is not None:
        lines.append(f"Цена клиента: {customs[CLIENT_PRICE]}")
    if customs.get(OUR_PRICE) is not None:
        lines.append(f"Назвали клиенту: {customs[OUR_PRICE]}")
    if customs.get(SCORE) is not None:
        lines.append(f"Оценка разговора: {customs[SCORE]} из 10")
    lines.append("Источник: звонок на общий номер компании")
    if customs.get(MANAGER_RECS):
        lines.append(f"Что сделать: {customs[MANAGER_RECS]}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Дозаполнить выжимку в заявках.")
    ap.add_argument("--days", type=int, default=14, help="как глубоко смотреть назад")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--apply", action="store_true", help="записывать, а не показывать")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
    writer = CrmWriter(client, apply=args.apply)

    filled = skipped = nothing = 0
    for page in range(1, 11):
        try:
            payload = client.get("orders", sort="-created-at", per_page=50, page=page)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("страница %s не прочиталась: %s", page, exc)
            break
        rows = payload.get("data") or []
        if not rows:
            break
        for row in rows:
            if filled + skipped >= args.limit:
                break
            attrs = row.get("attributes") or {}
            customs = attrs.get("customs") or {}
            if str(customs.get(SUMMARY) or "").strip():
                skipped += 1
                continue
            if not str(customs.get(TRANSCRIPT) or "").strip():
                # Разбора по этой заявке нет вовсе — выжимку взять неоткуда.
                nothing += 1
                continue
            text = summary_from(attrs)
            if len(text.splitlines()) < 2:
                nothing += 1
                continue
            print(f"заявка {row['id']} ({attrs.get('name') or '—'}): "
                  f"{text.splitlines()[0][:90]}")
            try:
                writer.set_summary(row["id"], text)
                filled += 1
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("заявка %s: не записалось — %s", row["id"], exc)
        if filled + skipped >= args.limit:
            break

    print(f"\n{'записано' if args.apply else 'нашлось (сухой прогон)'}: {filled}, "
          f"уже заполнено: {skipped}, нечем заполнить: {nothing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
