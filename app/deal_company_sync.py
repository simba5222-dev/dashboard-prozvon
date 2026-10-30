"""Синхронизация «Компания И» и «ИНН И» с исполнителем на сделке Synergy.

Контекст (30.10.2026, по просьбе владельца): на экране сделки есть поле
«Исполнитель», и владелец хотел, чтобы при его изменении «Компания И»
(`custom-98` сделки) обновлялась тем же значением. Сначала показалось, что
«Исполнитель» — это отдельное custom-поле сделки (`custom-26700`), но
проверка на реальной сделке (3642023, «Автокран») это опровергла: оба поля
пустые у всех проверенных сделок, `custom-26700` явно не используется.

Настоящая цепочка, подтверждённая на той же сделке 30.10.2026:

    сделка → её транспорт (GET /deals/{id}/transports)
           → телефон исполнителя на транспорте (custom-29545, «Телефон»)
           → контакт по этому телефону (как ищет app.collector.find_contact)
           → компания этого контакта (GET /contacts/{id}/companies)
           → она пишется в «Компания И» (custom-98 сделки),
             а её нативный ИНН (`attributes.inn` компании) — в «ИНН И»
             (custom-16357 сделки, текстовое поле), по той же просьбе
             владельца от 30.10.2026.

Триггер в Synergy — общее «Сделка: изменение» (владелец подтвердил: его
интересует изменение поля «Начало работ», но ни оно, ни «Транспорт» не
выбираются явным условием в конструкторе сценариев — там только
кастом-поля самой сделки). Поэтому решение, нужно ли копировать, целиком
на нашей стороне, а не в условии сценария.

Форма тела вебхука Synergy нигде не документирована, поэтому, как и
`app/subor_hook.py`, тело используем только чтобы достать id сделки, а
все значения перечитываем через API — телу чужого вебхука не доверяем.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("deal_company_sync")

FIELD_TRANSPORT_PHONE = "custom-29545"    # «Телефон» — на transports
FIELD_PERFORMER_COMPANY = "custom-98"     # «Компания И» — на deals, relation на Company
FIELD_PERFORMER_INN = "custom-16357"      # «ИНН И» — на deals, текст


def extract_deal_id(payload: dict) -> str | None:
    """Форма тела вебхука Synergy не документирована — проверяем несколько
    правдоподобных мест, как и `subor_hook.extract_order_id`."""
    if not isinstance(payload, dict):
        return None
    for path in (
        ("id",), ("deal_id",), ("dealId",),
        ("deal", "id"), ("data", "id"), ("data", "attributes", "id"),
        ("object", "id"), ("record", "id"), ("entity", "id"),
    ):
        node: object = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node not in (None, ""):
            return str(node)
    return None


def pick_transport_phone(transports: list[dict]) -> str | None:
    """У сделки может быть несколько единиц транспорта — берём телефон
    первой, у которой он вообще заполнен."""
    for t in transports:
        customs = (t.get("attributes") or {}).get("customs") or {}
        phone = customs.get(FIELD_TRANSPORT_PHONE)
        if phone:
            return phone
    return None


def decide_value(current: object, candidate: object) -> str | None:
    """Возвращает значение, которое нужно записать, или `None`, если писать
    не нужно (кандидата нет/уже совпадает). Общая логика и для «Компания И»
    (id компании), и для «ИНН И» (текст)."""
    if candidate in (None, ""):
        return None
    candidate = str(candidate)
    if str(current) == candidate:
        return None
    return candidate


def decide_updates(current_customs: dict, company_id: object, company_inn: object) -> dict[str, str]:
    """Собирает, что из «Компания И»/«ИНН И» реально нужно переписать —
    пустой словарь, если менять нечего (одной сделкой пишем, что
    разошлось, не трогая совпавшее)."""
    updates: dict[str, str] = {}
    company_value = decide_value(current_customs.get(FIELD_PERFORMER_COMPANY), company_id)
    if company_value is not None:
        updates[FIELD_PERFORMER_COMPANY] = company_value
    inn_value = decide_value(current_customs.get(FIELD_PERFORMER_INN), company_inn)
    if inn_value is not None:
        updates[FIELD_PERFORMER_INN] = inn_value
    return updates


def handle_webhook_sync(payload: dict) -> None:
    """Точка входа из FastAPI BackgroundTasks (синхронная обёртка) —
    зеркалит `subor_hook.handle_webhook_sync`."""
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(here))
    from app.collector import SynergyClient, find_contact
    from app.config import get_settings

    deal_id = extract_deal_id(payload)
    if not deal_id:
        logger.warning("вебхук «Компания И»: не нашёл id сделки в теле: %s", json.dumps(payload)[:500])
        return

    settings = get_settings()
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        timeout_sec=30.0, min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries,
    )

    try:
        deal_data = client.get(f"deals/{deal_id}").get("data")
    except Exception as e:  # noqa: BLE001 — фоновая задача обязана дожить до лога
        logger.warning("сделка %s: не удалось прочитать (%s)", deal_id, e)
        return
    if not deal_data:
        logger.warning("сделка %s: не найдена", deal_id)
        return

    try:
        transports = client.get(f"deals/{deal_id}/transports").get("data") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("сделка %s: не удалось прочитать транспорт (%s)", deal_id, e)
        return

    phone = pick_transport_phone(transports)
    if not phone:
        logger.info("сделка %s: у транспорта нет телефона исполнителя — копировать нечего", deal_id)
        return

    contact = find_contact(client, phone)
    if not contact:
        logger.info("сделка %s: по телефону %s контакт не найден", deal_id, phone)
        return

    try:
        companies = client.get(f"contacts/{contact['id']}/companies").get("data") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("сделка %s: не удалось прочитать компанию контакта %s (%s)", deal_id, contact["id"], e)
        return
    if not companies:
        logger.info("сделка %s: у контакта %s (по телефону %s) нет компании", deal_id, contact["id"], phone)
        return

    company = companies[0]
    current_customs = (deal_data.get("attributes") or {}).get("customs") or {}
    updates = decide_updates(current_customs, company["id"], company.get("attributes", {}).get("inn"))
    if not updates:
        logger.info("сделка %s: «Компания И»/«ИНН И» копировать не нужно (нет кандидата или уже совпадает)", deal_id)
        return

    try:
        client.patch(f"deals/{deal_id}", {"data": {
            "type": "deals", "id": str(deal_id),
            "attributes": {"customs": updates},
        }})
        logger.info(
            "сделка %s: обновлено %s (%s)",
            deal_id, updates, company.get("attributes", {}).get("name"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("сделка %s: не удалось записать «Компания И»/«ИНН И» (%s)", deal_id, e)
