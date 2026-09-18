#!/usr/bin/env python
"""Наполнить подсказки в карточках контактов.

    sudo -u claude .venv/bin/python scripts/fill_hints.py            показать
    sudo -u claude .venv/bin/python scripts/fill_hints.py --apply    записать
    ... --days 30       по каким звонкам собирать клиентов

Всплывающую карточку клиента CRM показывает сама. Но в ней только то, что
менеджеры завели руками, а у нас лежит то, чего там нет: о чём клиент говорил
в прошлые разы, что ему называли по цене, чем кончилось и какие вопросы по его
технике задать обязательно. Новичок без этого начинает разговор с нуля.

Поле задаётся настройкой `DASH_FIELD_CONTACT_HINT`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import hints  # noqa: E402
from app.collector import SynergyClient, find_contact, load_stages  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crm_write import CrmWriter  # noqa: E402
from app.db import connect  # noqa: E402

logger = logging.getLogger("hints")


def main() -> int:
    ap = argparse.ArgumentParser(description="Подсказки в карточках контактов.")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    field = settings.field_contact_hint
    if not field:
        print("поле подсказки не задано: заполните DASH_FIELD_CONTACT_HINT", file=sys.stderr)
        return 1

    conn = connect(settings.db_path)
    since = (date.today() - timedelta(days=args.days)).isoformat()
    phones = [r["client_phone"] for r in conn.execute(
        "SELECT DISTINCT client_phone FROM calls WHERE local_date >= ? AND direction = 'in' "
        "AND duration_sec >= 20 ORDER BY started_at DESC LIMIT ?", (since, args.limit))]

    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
    writer = CrmWriter(client, apply=args.apply)
    stages = load_stages(client)

    written = empty = no_contact = 0
    for phone in phones:
        contact = find_contact(client, phone)
        if contact is None:
            no_contact += 1
            continue
        orders = hints.orders_of(client, contact["id"], stages)
        text = hints.build(conn, phone, orders)
        if not text:
            empty += 1
            continue
        print(f"— {phone} ({(contact.get('attributes') or {}).get('as-string')}):")
        print("   " + text.replace("\n", "\n   "))
        if args.apply and writer.set_contact_hint(contact["id"], field, text):
            written += 1
        elif not args.apply:
            written += 1

    conn.close()
    print(f"\n{'записано' if args.apply else 'собрано (сухой прогон)'}: {written}, "
          f"сказать нечего: {empty}, контакта в CRM нет: {no_contact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
