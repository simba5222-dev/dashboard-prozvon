#!/usr/bin/env python
"""Поле «Позвонить» в карточках транспорта — ссылка на набор номера.

В карточке транспорта есть телефон поставщика (`custom-29545`), но это
просто текст: чтобы позвонить, его надо выделить и скопировать. Поле
«Позвонить» (`custom-30617`) хранит тот же номер ссылкой `tel:`, по которой
софтфон или телефон набирает сразу.

Вид ссылки — как просил владелец 24.09.2026:

    <a href="tel:+78123091309">+7-812-309-13-09</a>

В адресе номер сплошняком, дефисы только в видимой части. Образец такого
поля есть и у контактов (`custom-30342`), но там номер выводится без
разделителей — здесь читаемее.

    ./scripts/fill_call_links.py              посчитать, ничего не меняя
    ./scripts/fill_call_links.py --apply      записать
    ./scripts/fill_call_links.py --apply --limit 20

Номер берётся из местного справочника карточек (`sync_transports.py`), где
он уже приведён к десяти цифрам, и разворачивается в `+7XXXXXXXXXX`.

**Осторожно с массовым прогоном.** Правка карточки будит сценарии CRM: при
записи одной карточки 24.09.2026 у неё заодно заполнилось поле «Название».
Восемь с половиной тысяч правок разбудят их восемь с половиной тысяч раз,
поэтому идти лучше частями (`--limit`) и в нерабочее время.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import Settings  # noqa: E402

logger = logging.getLogger("fill_call_links")

FIELD_LINK = "custom-30617"   # «Позвонить» у транспорта


def pretty(number10: str) -> str:
    """Номер в читаемом виде: +7-999-999-12-12."""
    return (f"+7-{number10[:3]}-{number10[3:6]}-{number10[6:8]}-{number10[8:]}")


def link_for(number10: str) -> str:
    """Ссылка для набора. Номер в карточках российский, код страны +7.

    В адресе ссылки номер идёт сплошняком, без разделителей: их понимает не
    всякий софтфон, а в `tel:` они не несут смысла. Дефисы — только в том,
    что человек видит.
    """
    return f'<a href="tel:+7{number10}">{pretty(number10)}</a>'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Записать в CRM.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--id", help="Только эти карточки, через запятую.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries,
    )
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if args.id:
        ids = [x.strip() for x in args.id.split(",") if x.strip()]
        rows = conn.execute(
            f"""SELECT id, phone10, name FROM transport_cards
                 WHERE id IN ({",".join("?" * len(ids))}) AND phone10 <> ''""",
            ids).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, phone10, name FROM transport_cards "
            "WHERE phone10 <> '' ORDER BY CAST(id AS INTEGER)").fetchall()
    conn.close()

    total = len(rows)
    if args.limit:
        rows = rows[: args.limit]
    print(f"карточек с телефоном: {total}, к записи: {len(rows)}")
    if not args.apply:
        for row in rows[:3]:
            print(f"  пример: {row['id']} ({row['name'] or 'без названия'}) → "
                  f"{link_for(row['phone10'])}")
        print("\nничего не записано. Для записи: --apply")
        return 0

    written = same = failed = 0
    for index, row in enumerate(rows, 1):
        want = link_for(row["phone10"])
        try:
            data = client.get(f"transports/{row['id']}")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("карточка %s не прочитана: %s", row["id"], exc)
            failed += 1
            continue
        customs = ((data.get("data") or {}).get("attributes") or {}).get("customs") or {}
        if str(customs.get(FIELD_LINK) or "") == want:
            same += 1
            continue
        try:
            client.patch(f"transports/{row['id']}", {"data": {
                "id": str(row["id"]), "type": "transports",
                "attributes": {"customs": {FIELD_LINK: want}},
            }})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("карточка %s не записана: %s", row["id"], exc)
            failed += 1
            continue
        written += 1
        if index % 100 == 0:
            logger.info("  пройдено %s из %s", index, len(rows))

    print(f"записано: {written}, уже стояло: {same}, не вышло: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
