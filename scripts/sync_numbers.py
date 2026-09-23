#!/usr/bin/env python
"""Справочник номеров: кем нам приходится каждый, кто звонит.

Зачем. Проверка одного входящего стоила до десяти обращений в CRM: поиск
контакта по четырём телефонным полям в двух написаниях номера, затем его
заявки, затем компания. При полутысяче звонков в день это тысячи запросов
в чужую систему ради данных, которые почти не меняются.

Номеров в истории звонков всего около трёх с половиной тысяч. Поэтому
каждый разбирается **один раз** и кладётся в таблицу `numbers`, а дальше
все решения принимаются из неё — мгновенно и без сети. Новых номеров в
день появляется несколько десятков, их добирает этот же скрипт по таймеру.

    ./scripts/sync_numbers.py                 разобрать новые номера
    ./scripts/sync_numbers.py --refresh 30    заодно обновить старше 30 дней
    ./scripts/sync_numbers.py --limit 200     ограничить проход
    ./scripts/sync_numbers.py --apply-crm     проставить тип в карточке контакта

Категории:

    исполнитель   есть карточки техники, заявок нет
    заказчик      есть заявки, карточек техники нет
    оба           есть и то, и другое — в нашем деле это обычное дело
    неизвестный   в CRM не нашёлся

**Категория не решает, слушать звонок или нет.** Это правило: 23.09.2026
на цифрах вышло, что 16 из 67 пойманных заявок пришли с номеров, у которых
есть карточки техники — исполнители звонят и за техникой тоже. Категория
идёт в разбор уликой, а решает содержание разговора.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient, find_contact  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, phone10, save_number  # noqa: E402
from app.search_calls import supplier_cards  # noqa: E402

logger = logging.getLogger("sync_numbers")

PERFORMER = "исполнитель"
CUSTOMER = "заказчик"
BOTH = "оба"
UNKNOWN = "неизвестный"

# Названия вариантов в поле «Тип контакта_API» — слово в слово из CRM,
# вместе с опечаткой в «Исполнтиель». Своё написание недопустимо: поле
# списочное, и чужое значение станет в отчётах отдельной категорией.
CRM_FIELD = "custom-30616"
CRM_VALUE = {PERFORMER: "Исполнтиель", CUSTOMER: "Заказчик", BOTH: "Заказчик/Исп"}


def classify(cards: int, orders: int, found: bool) -> str:
    if cards and orders:
        return BOTH
    if cards:
        return PERFORMER
    if orders:
        return CUSTOMER
    return CUSTOMER if found else UNKNOWN


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", type=int, default=0,
                        help="Перепроверить номера старше стольких дней.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply-crm", action="store_true",
                        help="Проставить тип в карточке контакта в Synergy.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(message)s")
    settings = Settings()
    if not settings.synergy_configured:
        print("нет ключа Synergy")
        return 1
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries,
    )
    conn = connect(settings.db_path)
    init_schema(conn)

    stale = ""
    if args.refresh:
        stale = (datetime.now(timezone.utc) - timedelta(days=args.refresh)).isoformat()
    rows = conn.execute(
        """
        SELECT DISTINCT substr(replace(replace(replace(k.client_phone, '+', ''),
                               ' ', ''), '-', ''), -10) AS phone10
          FROM calls k
         WHERE k.client_phone IS NOT NULL AND k.client_phone <> ''
        """).fetchall()
    known = {r["phone10"]: r for r in conn.execute("SELECT * FROM numbers")}
    todo = []
    for row in rows:
        number = row["phone10"]
        if len(number) != 10:
            continue
        seen = known.get(number)
        if seen and not (stale and (seen["resolved_at"] or "") < stale):
            continue
        todo.append(number)
    if args.limit:
        todo = todo[: args.limit]
    print(f"номеров в звонках: {len(rows)}, разобрано раньше: {len(known)}, "
          f"к разбору: {len(todo)}")

    now = datetime.now(timezone.utc).isoformat()
    counts: dict[str, int] = {}
    written = 0
    for index, number in enumerate(todo, 1):
        # Сначала своё: карточки транспорта уже лежат у нас копией, и для
        # половины номеров этого хватает без единого обращения в CRM.
        cards = supplier_cards(conn, number)
        contact_id = str(cards[0]["contact_id"]) if cards else ""
        name = (cards[0]["contact_name"] if cards else "") or ""
        types = sorted({c["type_name"] for c in cards if c["type_name"]})

        found = bool(contact_id)
        if not contact_id:
            contact = find_contact(client, number)
            if contact is not None:
                found = True
                contact_id = str(contact["id"])
                attrs = contact.get("attributes") or {}
                name = str(attrs.get("as-string") or "").strip() or name

        orders = 0
        if contact_id:
            orders = client.count(f"contacts/{contact_id}/orders") or 0

        category = classify(len(cards), orders, found)
        counts[category] = counts.get(category, 0) + 1
        save_number(conn, phone10=number, name=name, contact_id=contact_id,
                    category=category, cards=len(cards), types=", ".join(types),
                    orders=orders, resolved_at=now)
        conn.commit()

        if args.apply_crm and contact_id and category in CRM_VALUE:
            try:
                client.patch(f"contacts/{contact_id}", {"data": {
                    "id": contact_id, "type": "contacts",
                    "attributes": {"customs": {CRM_FIELD: [CRM_VALUE[category]]}},
                }})
                written += 1
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("контакт %s: тип не записан — %s", contact_id, exc)

        if index % 50 == 0:
            logger.info("  разобрано %s из %s", index, len(todo))

    print("разложились так: " + ", ".join(f"{k} — {v}" for k, v in sorted(counts.items())))
    if args.apply_crm:
        print(f"типов записано в CRM: {written}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
