#!/usr/bin/env python
"""Справочник карточек транспорта из Synergy в местную базу.

Зачем копия. Проверка «занёс ли менеджер технику в карточку» идёт по телефону
поставщика, а телефон в CRM записан как попало: «+7951…», «8951…», со
скобками. Фильтр Synergy сравнивает строки точно, поэтому искать через него
по номеру — значит промахиваться на каждом втором. Здесь номер приводится к
десяти цифрам один раз, и дальше сверка идёт мгновенно и без сети.

    ./scripts/sync_transports.py              всё (11 тысяч карточек, ~2 минуты)
    ./scripts/sync_transports.py --pages 5    первые страницы, для проверки

Тип техники и контакт приходят тем же запросом через `include` — иначе на
каждую карточку ушло бы по два обращения, а их почти двенадцать тысяч.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, phone10, save_transport_card  # noqa: E402

logger = logging.getLogger("sync_transports")

FIELD_PHONE = "custom-29545"
FIELD_NAME = "custom-30419"
FIELD_STATUS = "custom-26967"


def first(value: object) -> str:
    """Списочное поле CRM — к строке: выбор хранится списком из одного."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=0, help="Ограничить число страниц.")
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
    now = datetime.now(timezone.utc).isoformat()

    page = 1
    saved = skipped = 0
    while True:
        try:
            data = client.get("transports", per_page=50,
                              include="transport-type,contacts",
                              **{"page[number]": page})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("страница %s не прочитана: %s", page, exc)
            break

        # Справочники из того же ответа: тип техники и контакт лежат в
        # `included`, а у карточки на них только ссылка по id.
        types: dict[str, str] = {}
        contacts: dict[str, tuple[str, str]] = {}
        for item in data.get("included") or []:
            attrs = item.get("attributes") or {}
            if item.get("type") == "transport-types":
                types[item["id"]] = str(attrs.get("name") or "")
            elif item.get("type") == "contacts":
                name = " ".join(
                    str(attrs.get(part) or "").strip()
                    for part in ("last-name", "first-name", "middle-name")
                ).strip()
                number = ""
                for field in ("general-phone", "mobile-phone", "work-phone", "other-phone"):
                    number = phone10(attrs.get(field))
                    if number:
                        break
                contacts[item["id"]] = (name, number)

        rows = data.get("data") or []
        if not rows:
            break
        for row in rows:
            attrs = row.get("attributes") or {}
            customs = attrs.get("customs") or {}
            rels = row.get("relationships") or {}
            type_ref = ((rels.get("transport-type") or {}).get("data") or {})
            contact_refs = ((rels.get("contacts") or {}).get("data") or [])
            contact_id = str(contact_refs[0]["id"]) if contact_refs else ""
            # Телефон берём из карточки, а если его там нет — из контакта.
            # Пустой он у каждой третьей карточки, и без запасного варианта
            # треть базы просто не нашлась бы по номеру.
            contact_name, contact_phone = contacts.get(contact_id, ("", ""))
            number = phone10(customs.get(FIELD_PHONE)) or contact_phone
            save_transport_card(
                conn,
                id=str(row["id"]),
                name=first(customs.get(FIELD_NAME)) or str(attrs.get("brand") or ""),
                type_id=str(type_ref.get("id") or ""),
                type_name=types.get(str(type_ref.get("id") or ""), ""),
                phone10=number,
                contact_id=contact_id,
                contact_name=contact_name,
                status=first(customs.get(FIELD_STATUS)),
                updated_at=str(attrs.get("updated-at") or ""),
                synced_at=now,
            )
            saved += 1
            skipped += int(not number)
        conn.commit()

        total_pages = (data.get("meta") or {}).get("page-count") or 1
        if not args.quiet and page % 20 == 0:
            logger.info("  страница %s из %s, карточек %s", page, total_pages, saved)
        if args.pages and page >= args.pages:
            break
        if page >= total_pages:
            break
        page += 1

    conn.close()
    print(f"карточек в справочнике: {saved}, из них без телефона: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
