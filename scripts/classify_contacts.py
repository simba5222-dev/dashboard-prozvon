#!/usr/bin/env python
"""Тип контакта в Synergy по тому, что у человека есть на самом деле.

Правило владельца (28.09.2026), целиком:

    есть заявка                               →  «Заказчик»
    есть только транспорт                     →  «Исполнитель»
    есть и транспорт, и заявки                →  «Заказчик/Исп»

Третий тип в решениях ведёт себя как заказчик: звонки таких контактов из
прослушки не исключаются.

**Заказчиков на два порядка больше.** С транспортом 6 245 контактов, с одними
заявками — 42 475. При скорости CRM в 1,25 секунды на карточку это 15 часов
записи подряд, и каждая правка будит сценарии Synergy. Поэтому заказчики идут
не одним махом, а ночными порциями: `--customers --limit N`.

Зачем. В нашем деле одна и та же контора сегодня сдаёт нам погрузчик, а
завтра ищет у нас экскаватор себе на объект. Разделение «исполнитель против
заказчика» этого не описывает, поэтому и заведён третий тип. Важное
следствие: звонки контактов с типом «Заказчик/исп.» из прослушки **не**
исключаются — они звонят и за техникой тоже.

    ./scripts/classify_contacts.py --scan     собрать, у кого есть заявки
    ./scripts/classify_contacts.py            показать, что получится
    ./scripts/classify_contacts.py --apply    проставить типы владельцам техники
    ./scripts/classify_contacts.py --customers --limit 3000 --apply   порция заказчиков

`--scan` проходит все заявки и запоминает их контакты. Так дешевле: заявок
75 тысяч по 50 на страницу — полторы тысячи запросов, а спрашивать каждый из
шести тысяч контактов отдельно — шесть тысяч.

Тип пишется в поле «Тип контакта_API» (`custom-30616`). Штатная связь
`contact-type` через API не меняется: запрос проходит с ответом 200, а
значение остаётся прежним — проверено 23.09.2026. Поле-обходчик завёл
владелец, и это единственный способ проставить тип извне.

Тип не трогается, если он уже верный, и не перебивается у «Кадров» и
«Диспетчеров»: это другая ось. Там человек помечен по роли в компании, а не
по тому, чем торгует, и решение по таким контактам — за человеком.
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

# Тип пишется в поле «Тип контакта_API» (`custom-30616`), а не в штатную
# связь `contact-type`: ту API принимает и молча не применяет — проверено
# 23.09.2026 на контакте 3115806, ответ 200, значение не меняется. Поле
# заведено владельцем ровно затем, чтобы тип можно было проставить извне.
FIELD_TYPE = "custom-30616"

# Значения берутся ИЗ СПРАВОЧНИКА ПОЛЯ, слово в слово. Своё написание
# недопустимо: поле-список, и значение не из списка окажется в отчётах и
# фильтрах отдельной категорией. Сверка со справочником перед записью —
# не формальность: 23.09.2026 владелец поправил в CRM опечатку
# («Исполнтиель» → «Исполнитель»), и проверка остановила запись прежде,
# чем в базу ушли тысячи значений мимо списка.
BOTH = "Заказчик/Исп"
PERFORMER = "Исполнитель"
CUSTOMER = "Заказчик"
# Эти значения не перебиваем: они про роль человека в компании, а не про
# то, что у него есть.
KEEP = {"Кадры", "Диспетчер"}


def scan_orders(client: SynergyClient, target: Path) -> int:
    """Собрать id контактов, у которых есть хотя бы одна заявка."""
    found: set[str] = set()
    page = 1
    while True:
        try:
            # `include` обязателен: без него JSON:API отдаёт у связи только
            # ссылки, без `data`, и список контактов выходит пустым. На этом
            # 23.09.2026 сгорел получасовой проход — вернулось 0 контактов.
            data = client.get("orders", per_page=50, include="contacts",
                              **{"page[number]": page})
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


def field_options(client: SynergyClient) -> list[str]:
    """Допустимые значения поля «Тип контакта_API» — из самой CRM."""
    data = client.get(f"custom-fields/{FIELD_TYPE.split('-')[1]}")
    attrs = (data.get("data") or {}).get("attributes") or {}
    return [str(v) for v in (attrs.get("select-options") or [])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true",
                        help="Пройти заявки и запомнить, у кого они есть.")
    parser.add_argument("--apply", action="store_true", help="Записать типы в CRM.")
    parser.add_argument("--limit", type=int, default=0, help="Не больше стольких контактов.")
    parser.add_argument("--customers", action="store_true",
                        help="Те, у кого есть заявки и нет техники, — «Заказчик». "
                             "Их 42 тысячи, поэтому идти порциями и ночью.")
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

    options = field_options(client)
    missing = [name for name in (BOTH, PERFORMER, CUSTOMER) if name not in options]
    if missing:
        print(f"в поле «Тип контакта_API» нет вариантов: {', '.join(missing)}")
        print(f"есть: {', '.join(options)}")
        print("добавьте их в CRM или поправьте BOTH/PERFORMER в этом скрипте")
        return 1

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
    if args.customers:
        # Заказчики: заявки есть, техники нет. Владельцев техники здесь быть
        # не должно — они разбираются обычным проходом и получают «Исполнитель»
        # или «Заказчик/Исп».
        свои = {o["contact_id"] for o in owners}
        for cid in sorted(with_orders - свои, key=lambda x: int(x) if str(x).isdigit() else 0):
            plan.append(({"contact_id": str(cid), "name": "", "cards": 0}, CUSTOMER))
        print(f"контактов с заявками и без техники → «{CUSTOMER}»: {len(plan)}")
    else:
        for owner in owners:
            want = BOTH if owner["contact_id"] in with_orders else PERFORMER
            plan.append((owner, want))
        both = sum(1 for _, want in plan if want == BOTH)
        print(f"контактов с транспортом: {len(plan)}")
        print(f"  из них с заявками → «{BOTH}»: {both}")
        print(f"  только транспорт  → «{PERFORMER}»: {len(plan) - both}")
    if not args.apply:
        print("\nэто предварительный расчёт, в CRM ничего не записано."
              " Для записи: --apply")
        for owner, want in plan[:5]:
            print(f"  пример: контакт {owner['contact_id']} "
                  f"({owner['name'] or 'без имени'}, карточек {owner['cards']}) → {want}")
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
        customs = ((data.get("data") or {}).get("attributes") or {}).get("customs") or {}
        value = customs.get(FIELD_TYPE)
        current = str(value[0]) if isinstance(value, list) and value else str(value or "")
        if current in KEEP:
            kept += 1
            continue
        if current == want:
            same += 1
            continue
        try:
            # Только своё поле: `customs` при записи не перетирает остальные,
            # проверено — соседние значения в карточке остаются на месте.
            client.patch(f"contacts/{cid}", {"data": {
                "id": str(cid), "type": "contacts",
                "attributes": {"customs": {FIELD_TYPE: [want]}},
            }})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("контакт %s не записан: %s", cid, exc)
            failed += 1
            continue
        changed += 1
        if changed % 100 == 0:
            logger.info("  проставлено %s из %s", changed, len(plan))

    print(f"проставлено: {changed}, уже стояло: {same}, "
          f"не тронуто (Кадры/Диспетчер): {kept}, не вышло: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
