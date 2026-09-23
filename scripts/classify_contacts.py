#!/usr/bin/env python
"""Тип контакта в Synergy по тому, что у человека есть на самом деле.

Правило владельца (23.09.2026):

    есть привязанный транспорт и есть заявки  →  «Заказчик/исп.»
    есть только транспорт                     →  «Исполнитель»

Зачем. В нашем деле одна и та же контора сегодня сдаёт нам погрузчик, а
завтра ищет у нас экскаватор себе на объект. Разделение «исполнитель против
заказчика» этого не описывает, поэтому и заведён третий тип. Важное
следствие: звонки контактов с типом «Заказчик/исп.» из прослушки **не**
исключаются — они звонят и за техникой тоже.

    ./scripts/classify_contacts.py --scan     собрать, у кого есть заявки
    ./scripts/classify_contacts.py            показать, что получится
    ./scripts/classify_contacts.py --apply    проставить типы в CRM

`--scan` проходит все заявки и запоминает их контакты. Так дешевле: заявок
75 тысяч по 50 на страницу — полторы тысячи запросов, а спрашивать каждый из
шести тысяч контактов отдельно — шесть тысяч.

Тип не трогается, если он уже верный, и не перебивается у «Диспетчеров» и
«Кадров»: это другая ось. Там человек помечен по роли в компании, а не по
тому, чем торгует, и решение по таким контактам — за человеком.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import Settings  # noqa: E402

logger = logging.getLogger("classify_contacts")

TYPE_BOTH = "4265"       # «Заказчик/исп.»
TYPE_PERFORMER = "101"   # «Исполнитель»
TYPE_CUSTOMER = "97"     # «Заказчик»
# Эти типы не перебиваем: они про роль человека, а не про то, что у него есть.
KEEP = {"1138", "2055"}  # «Диспетчер», «Кадры»

NAMES = {TYPE_BOTH: "Заказчик/исп.", TYPE_PERFORMER: "Исполнитель",
         TYPE_CUSTOMER: "Заказчик", "1138": "Диспетчер", "2055": "Кадры"}


def scan_orders(client: SynergyClient, target: Path) -> int:
    """Собрать id контактов, у которых есть хотя бы одна заявка."""
    found: set[str] = set()
    page = 1
    while True:
        try:
            data = client.get("orders", per_page=50, **{"page[number]": page})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("страница заявок %s не прочитана: %s", page, exc)
            break
        rows = data.get("data") or []
        if not rows:
            break
        for row in rows:
            for ref in (((row.get("relationships") or {}).get("contacts") or {}).get("data") or []):
                found.add(str(ref["id"]))
        total = (data.get("meta") or {}).get("page-count") or 1
        if page % 100 == 0:
            logger.info("  страница %s из %s, контактов с заявками %s",
                        page, total, len(found))
        if page >= total:
            break
        page += 1
    target.write_text(json.dumps(sorted(found)), encoding="utf-8")
    print(f"контактов с заявками: {len(found)} → {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true",
                        help="Пройти заявки и запомнить, у кого они есть.")
    parser.add_argument("--apply", action="store_true", help="Записать типы в CRM.")
    parser.add_argument("--limit", type=int, default=0, help="Не больше стольких контактов.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries,
    )
    # Рядом с базой: у дашборда всё рабочее лежит в data/.
    cache = Path(settings.db_path).parent / "contacts-with-orders.json"

    if args.scan:
        return scan_orders(client, cache)

    if not cache.exists():
        print(f"нет {cache} — сначала запустите с --scan")
        return 1
    with_orders = set(json.loads(cache.read_text(encoding="utf-8")))

    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    owners = [dict(row) for row in conn.execute(
        """SELECT contact_id, MIN(contact_name) AS name, COUNT(*) AS cards
             FROM transport_cards WHERE contact_id <> ''
            GROUP BY contact_id ORDER BY cards DESC""")]
    conn.close()

    plan: list[tuple[dict, str]] = []
    for owner in owners:
        want = TYPE_BOTH if owner["contact_id"] in with_orders else TYPE_PERFORMER
        plan.append((owner, want))
    both = sum(1 for _, want in plan if want == TYPE_BOTH)
    print(f"контактов с транспортом: {len(plan)}")
    print(f"  из них с заявками → «Заказчик/исп.»: {both}")
    print(f"  только транспорт  → «Исполнитель»:   {len(plan) - both}")
    if not args.apply:
        print("\nэто предварительный расчёт, в CRM ничего не записано."
              " Для записи: --apply")
        for owner, want in plan[:5]:
            print(f"  пример: контакт {owner['contact_id']} "
                  f"({owner['name'] or 'без имени'}, карточек {owner['cards']}) → {NAMES[want]}")
        return 0

    if args.limit:
        plan = plan[: args.limit]
    changed = same = kept = failed = 0
    for owner, want in plan:
        cid = owner["contact_id"]
        try:
            data = client.get(f"contacts/{cid}")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("контакт %s не прочитан: %s", cid, exc)
            failed += 1
            continue
        current = str((((data.get("data") or {}).get("relationships") or {})
                       .get("contact-type") or {}).get("data", {}).get("id") or "")
        if current in KEEP:
            kept += 1
            continue
        if current == want:
            same += 1
            continue
        try:
            client.patch(f"contacts/{cid}", {"data": {
                "id": str(cid), "type": "contacts",
                "relationships": {"contact-type": {
                    "data": {"type": "contact-types", "id": want}}},
            }})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("контакт %s не записан: %s", cid, exc)
            failed += 1
            continue
        changed += 1
        if changed % 100 == 0:
            logger.info("  проставлено %s из %s", changed, len(plan))

    print(f"проставлено: {changed}, уже стояло: {same}, "
          f"не тронуто (Диспетчер/Кадры): {kept}, не вышло: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
