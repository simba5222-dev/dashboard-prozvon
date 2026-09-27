#!/usr/bin/env python
"""Сторож поля «Позвонить»: новые карточки и смена телефона.

Владелец просил автоматизацию в самой CRM: добавили транспорт — поле
«Позвонить» заполнено; сменился телефон у контакта — ссылка обновилась.
Сценарий Synergy через API не создаётся: `POST /scenarios` отвечает 404, а
условия и действия существующих сценариев не отдаются вовсе. Поэтому
поведение делает сторож на нашей стороне.

Стоит он дёшево. Список транспорта и контактов умеет сортироваться
(`sort=-created-at`, `sort=-updated-at`), поэтому всё новое и всё
изменившееся лежит на первой странице: два-три запроса вместо перебора
двенадцати тысяч карточек.

    ./scripts/watch_links.py             посмотреть, что бы изменилось
    ./scripts/watch_links.py --apply     дописать ссылки
    ./scripts/watch_links.py --pages 3   заглянуть глубже первой страницы

Полная выверка всей базы — отдельно, `sync_transports.py` плюс
`fill_call_links.py --watch --apply`: раз в сутки этого достаточно.

**Откуда берётся номер.** Из телефона самой карточки (`custom-29545`), а
если он пуст — из телефона контакта. Так же, как в справочнике карточек:
расходиться этим двум местам нельзя, иначе ссылка и сверка техники будут
звать в разные стороны.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fill_call_links import FIELD_LINK, link_for  # noqa: E402

logger = logging.getLogger("watch_links")

FIELD_PHONE = "custom-29545"
FIELD_NAME = "custom-30419"
FIELD_STATUS = "custom-26967"
CONTACT_PHONES = ("general-phone", "mobile-phone", "work-phone", "other-phone")


def first(value: object) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def contact_phone(attrs: dict) -> str:
    for field in CONTACT_PHONES:
        number = phone10(attrs.get(field))
        if number:
            return number
    return ""


def refresh(conn, client: SynergyClient, sort: str, pages: int) -> list[dict]:
    """Перечитать верхушку списка карточек и обновить свою копию."""
    touched: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for page in range(1, pages + 1):
        try:
            data = client.get("transports", per_page=50, sort=sort,
                              include="transport-type,contacts",
                              **{"page[number]": page})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("список (%s, стр. %s) не прочитан: %s", sort, page, exc)
            break
        types: dict[str, str] = {}
        contacts: dict[str, tuple[str, str]] = {}
        for item in data.get("included") or []:
            attrs = item.get("attributes") or {}
            if item.get("type") == "transport-types":
                types[item["id"]] = str(attrs.get("name") or "")
            elif item.get("type") == "contacts":
                name = " ".join(str(attrs.get(p) or "").strip()
                                for p in ("last-name", "first-name", "middle-name")).strip()
                contacts[item["id"]] = (name, contact_phone(attrs))
        for row in data.get("data") or []:
            attrs = row.get("attributes") or {}
            customs = attrs.get("customs") or {}
            rels = row.get("relationships") or {}
            type_ref = ((rels.get("transport-type") or {}).get("data") or {})
            contact_refs = ((rels.get("contacts") or {}).get("data") or [])
            contact_id = str(contact_refs[0]["id"]) if contact_refs else ""
            contact_name, phone_from_contact = contacts.get(contact_id, ("", ""))
            number = phone10(customs.get(FIELD_PHONE)) or phone_from_contact
            save_transport_card(
                conn, id=str(row["id"]),
                name=first(customs.get(FIELD_NAME)) or str(attrs.get("brand") or ""),
                type_id=str(type_ref.get("id") or ""),
                type_name=types.get(str(type_ref.get("id") or ""), ""),
                phone10=number, contact_id=contact_id, contact_name=contact_name,
                status=first(customs.get(FIELD_STATUS)),
                updated_at=str(attrs.get("updated-at") or ""), synced_at=now,
                call_link=first(customs.get(FIELD_LINK)),
            )
            if number and first(customs.get(FIELD_LINK)) != link_for(number):
                touched.append({"id": str(row["id"]), "phone10": number,
                                "name": str(attrs.get("brand") or "")})
    conn.commit()
    return touched


def by_contacts(conn, client: SynergyClient, pages: int) -> list[dict]:
    """Контакты, которые недавно правили: не сменился ли у них номер.

    Карточка транспорта при смене телефона у контакта не обязана меняться
    сама, и в списке `-updated-at` по транспорту её не будет. Поэтому
    смотрим и со стороны контактов: у чьих карточек нет своего телефона,
    тем номер даёт контакт.
    """
    touched: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for page in range(1, pages + 1):
        try:
            data = client.get("contacts", per_page=50, sort="-updated-at",
                              **{"page[number]": page})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("список контактов (стр. %s) не прочитан: %s", page, exc)
            break
        for row in data.get("data") or []:
            number = contact_phone(row.get("attributes") or {})
            if not number:
                continue
            cards = conn.execute(
                "SELECT id, phone10, call_link FROM transport_cards WHERE contact_id = ?",
                (str(row["id"]),)).fetchall()
            for card in cards:
                # Свой телефон у карточки главнее: его менеджер видит и правит
                # в самой карточке. Контакт подставляется, только когда там пусто.
                want = card["phone10"] or number
                if want and str(card["call_link"] or "") != link_for(want):
                    touched.append({"id": card["id"], "phone10": want, "name": ""})
                    conn.execute(
                        "UPDATE transport_cards SET phone10 = ?, synced_at = ? WHERE id = ?",
                        (want, now, card["id"]))
    conn.commit()
    return touched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Записать ссылки в CRM.")
    parser.add_argument("--pages", type=int, default=1,
                        help="Сколько страниц верхушки смотреть.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(message)s")
    settings = Settings()
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries,
    )
    conn = connect(settings.db_path)
    init_schema(conn)

    todo: dict[str, dict] = {}
    for item in refresh(conn, client, "-created-at", args.pages):
        todo[item["id"]] = item
    for item in refresh(conn, client, "-updated-at", args.pages):
        todo[item["id"]] = item
    for item in by_contacts(conn, client, args.pages):
        todo.setdefault(item["id"], item)

    print(f"карточек без верной ссылки среди свежих: {len(todo)}")
    if not args.apply:
        for item in list(todo.values())[:5]:
            print(f"  {item['id']} → {link_for(item['phone10'])}")
        if todo:
            print("\nничего не записано. Для записи: --apply")
        conn.close()
        return 0

    written = failed = 0
    for item in todo.values():
        want = link_for(item["phone10"])
        try:
            client.patch(f"transports/{item['id']}", {"data": {
                "id": item["id"], "type": "transports",
                "attributes": {"customs": {FIELD_LINK: want}},
            }})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("карточка %s не записана: %s", item["id"], exc)
            failed += 1
            continue
        conn.execute("UPDATE transport_cards SET call_link = ? WHERE id = ?",
                     (want, item["id"]))
        written += 1
    conn.commit()
    conn.close()
    print(f"ссылок проставлено: {written}, не вышло: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
