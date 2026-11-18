#!/usr/bin/env python
"""ABC-анализ клиентской базы — категория, частота, риск оттока (контакт в Synergy).

    sudo -u claude .venv/bin/python scripts/analyze_abc.py              # сухой прогон
    sudo -u claude .venv/bin/python scripts/analyze_abc.py --apply      # записать в CRM
    ... --contact 123456 --apply   записать только этого контакта (проверка перед боевым прогоном)

Зачем. Владелец 29.10.2026 попросил делить клиентскую базу на А/В/С по
деньгам и частоте обращений и проставлять это в CRM. Поля на контакте
(id проверены вживую через GET /custom-fields/{id} перед первой записью,
все три — "select", значение пишется списком из одного элемента, как и
другие такие поля этого аккаунта):

    custom-30764  ABC категория    — А / В / С         — пишем мы
    custom-30765  Частота заказа   — Постоянный / Эпизодический / Разовый — пишем мы
    custom-30766  Риск оттока      — Нет / Да          — пишем мы
    custom-30767  Потенциал роста  — ручное поле менеджера, не трогаем
    custom-30768  Комментарий по клиенту — ручное поле менеджера, не трогаем

Как считаем (решено с владельцем 29.10.2026, проверено на живых данных):

1. Деньги — сумма по всем выигранным сделкам контакта за всё время
   (custom-14924 заявки — цена, на которую согласен клиент; если пусто —
   запасной путь custom-257, наша цена). Клиенты ранжируются по убыванию,
   по накопленному проценту от общей выручки — Парето: А — первые 80%,
   В — следующие 15%, С — последние 5%. На живых данных это дало не
   учебные 20/80, а 833 из 2598 клиентов в А (32%) — реальное распределение
   площе учебника, владелец решил пороги оставить как есть.
2. Частота — число выигранных сделок за всё время: 1 → Разовый,
   2-4 → Эпизодический, 5+ → Постоянный.
3. Риск оттока — категория А или В, и последняя сделка больше RISK_DAYS
   (183 дня, ~6 месяцев) назад.

Заявка привязывается к клиенту связью `contact`. Около 39% выигранных
заявок (проверено 29.10.2026: 2241 из 5778, ≈18,6 млн ₽ из 59,4 млн) —
настоящие закрытые сделки (с техникой, адресом, суммой), но без
привязанного контакта в CRM. В ABC они не попадают вовсе: писать
категорию некуда, нет contact_id. Телефон заявки (custom-276) подхватывает
малую часть таких заявок в саму ранжировку (134 из 2241 на 29.10.2026),
но запись в Synergy всё равно идёт только по настоящему contact_id.

Пересчёт — раз в сутки, полный, не инкрементальный: объём (≈6 тыс.
выигранных сделок) это позволяет за пару минут. В CRM пишем только если
категория/частота/риск изменились с прошлого прогона или ещё не
записывались (`applied=0`) — не гоняем лишний раз общий лимит запросов
Synergy, который делят между собой телефония и дашборд.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient, load_stages  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crm_write import CrmWriter  # noqa: E402
from app.db import connect, get_abc_report, init_schema, save_abc_report  # noqa: E402

logger = logging.getLogger("abc")

FIELD_CATEGORY = "custom-30764"      # ABC категория: А / В / С
FIELD_FREQUENCY = "custom-30765"     # Частота заказа: Постоянный / Эпизодический / Разовый
FIELD_RISK = "custom-30766"          # Риск оттока: Нет / Да
FIELD_PRICE_CLIENT = "custom-14924"  # цена, на которую согласен клиент
FIELD_PRICE_OURS = "custom-257"      # наша цена (запасной путь, если клиентская пуста)
FIELD_PHONE = "custom-276"           # телефон клиента прямо в заявке (запасной ключ ранжирования)

RISK_DAYS = 183  # ~6 месяцев, решение владельца 29.10.2026

CAT_LABELS = {"A": "А", "B": "В", "C": "С"}
FREQ_ONE, FREQ_FEW, FREQ_MANY = "Разовый", "Эпизодический", "Постоянный"
RISK_YES, RISK_NO = "Да", "Нет"


def digits10(phone: str | None) -> str | None:
    d = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else None


def won_orders(client: SynergyClient, won_stage_id: str, max_pages: int = 200) -> list[dict]:
    """Все выигранные заявки за всё время — через relationship-эндпоинт
    стадии (`order-stages/{id}/orders`), а не весь `/orders`: тот фильтры
    тихо игнорирует (skill «синержи»), и перелистать пришлось бы все ~77
    тысяч заявок компании вместо ~5,8 тысячи выигранных (проверено живьём
    29.10.2026)."""
    out: list[dict] = []
    page = 1
    while page <= max_pages:
        payload = client.get(f"order-stages/{won_stage_id}/orders",
                              include="contact", **{"page[number]": page})
        rows = payload.get("data") or []
        if not rows:
            break
        out.extend(rows)
        page += 1
    return out


def aggregate_clients(rows: list[dict]) -> tuple[dict[str, dict], float, int]:
    """Деньги/частота/дата по клиентам. Ключ — "c:<contact_id>" у настоящих
    контактов, "p:<телефон>" — запасной путь только для ранжирования (в CRM
    такие клиенты не пишутся, см. docstring модуля)."""
    clients: dict[str, dict] = {}
    orphan_revenue = 0.0
    orphan_count = 0
    for row in rows:
        attrs = row.get("attributes") or {}
        customs = attrs.get("customs") or {}
        contact_ref = ((row.get("relationships") or {}).get("contact") or {}).get("data")
        contact_id = str(contact_ref.get("id")) if contact_ref else None
        try:
            money = float(customs.get(FIELD_PRICE_CLIENT) or customs.get(FIELD_PRICE_OURS) or 0)
        except (TypeError, ValueError):
            money = 0.0
        made = None
        try:
            made = datetime.fromisoformat(str(attrs.get("created-at") or "").replace("Z", "+00:00"))
        except ValueError:
            pass

        key = None
        if contact_id:
            key = f"c:{contact_id}"
        else:
            phone = digits10(customs.get(FIELD_PHONE))
            if phone:
                key = f"p:{phone}"
        if not key:
            orphan_revenue += money
            orphan_count += 1
            continue
        c = clients.setdefault(key, {"contact_id": contact_id, "revenue": 0.0,
                                     "deals": 0, "last": None})
        c["revenue"] += money
        c["deals"] += 1
        if made and (c["last"] is None or made > c["last"]):
            c["last"] = made
    return clients, orphan_revenue, orphan_count


def classify(clients: dict[str, dict], *, now: datetime) -> list[tuple[str, dict, str, str, str]]:
    """(key, клиент, категория A/B/C, частота, риск Да/Нет) по убыванию выручки."""
    total = sum(c["revenue"] for c in clients.values())
    ranked = sorted(clients.items(), key=lambda kv: -kv[1]["revenue"])
    risk_cutoff = now - timedelta(days=RISK_DAYS)
    out = []
    cum = 0.0
    for key, c in ranked:
        cum += c["revenue"]
        share = cum / total if total else 0.0
        cat = "A" if share <= 0.80 else "B" if share <= 0.95 else "C"
        n = c["deals"]
        freq = FREQ_ONE if n <= 1 else FREQ_FEW if n <= 4 else FREQ_MANY
        risk = RISK_YES if cat in ("A", "B") and (c["last"] is None or c["last"] < risk_cutoff) else RISK_NO
        out.append((key, c, cat, freq, risk))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contact", help="писать в CRM только этого контакта (для ручной проверки)")
    ap.add_argument("--apply", action="store_true", help="записать в Synergy")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        timeout_sec=30.0, min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries)
    crm = CrmWriter(client, apply=args.apply)

    stages = load_stages(client)
    won_ids = [sid for sid, (_name, kind) in stages.items() if kind == "won"]
    if not won_ids:
        logger.error("в справочнике стадий не нашлось kind=won — не считаю")
        return 1

    rows: list[dict] = []
    for sid in won_ids:
        rows.extend(won_orders(client, sid))
    logger.info("выигранных заявок всего: %d", len(rows))

    clients, orphan_revenue, orphan_count = aggregate_clients(rows)
    now = datetime.now(timezone.utc)
    classified = classify(clients, now=now)
    logger.info("клиентов с привязкой: %d, сиротская выручка (без клиента и телефона): "
                "%d заявок, %.0f ₽", len(clients), orphan_count, orphan_revenue)

    evaluated_at = now.isoformat()
    written = unchanged = skipped_no_contact = 0

    for key, c, cat, freq, risk in classified:
        contact_id = c["contact_id"]
        last_iso = c["last"].isoformat() if c["last"] else None
        prior = get_abc_report(conn, contact_id) if contact_id else None
        changed = (not prior or prior["category"] != cat
                   or prior["frequency"] != freq or prior["risk"] != risk)
        needs_write = changed or not (prior and prior["applied"])
        restrict_to_other = args.contact and str(contact_id) != str(args.contact)

        applied_now = False
        if contact_id and needs_write and not restrict_to_other:
            ok = crm.update_contact_customs(contact_id, {
                FIELD_CATEGORY: [CAT_LABELS[cat]],
                FIELD_FREQUENCY: [freq],
                FIELD_RISK: [risk],
            })
            applied_now = ok
            if ok:
                written += 1
        elif contact_id and not needs_write:
            applied_now = True
            unchanged += 1
        else:
            skipped_no_contact += 1

        save_abc_report(
            conn, contact_id=contact_id or key, revenue=c["revenue"], deals_count=c["deals"],
            last_deal_at=last_iso, category=cat, frequency=freq, risk=risk,
            applied=1 if applied_now else 0, evaluated_at=evaluated_at,
        )

        if args.contact and str(contact_id) == str(args.contact):
            logger.info("контакт %s: категория=%s частота=%s риск=%s выручка=%.0f сделок=%d",
                        contact_id, CAT_LABELS[cat], freq, risk, c["revenue"], c["deals"])

    conn.commit()
    logger.info("готово: записано в CRM %d, без изменений %d, без привязки к контакту %d",
                written, unchanged, skipped_no_contact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
