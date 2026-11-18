#!/usr/bin/env python
"""Найти заявки потока 1 (реклама) и завести их в своё зеркало `call_orders`.

    sudo -u claude .venv/bin/python scripts/discover_ad_orders.py            # сухой прогон
    sudo -u claude .venv/bin/python scripts/discover_ad_orders.py --apply    # записать
    ... --days 3      за сколько последних дней искать (по умолчанию 3)

Зачем. `scripts/analyze_orders.py` умеет складывать все звонки клиента по
заявке (входящие и исходящие) в одну картину — для живой заявки: где она
сейчас и надо ли звать РОПа, для проваленной: на каком этапе сорвалось и что
менеджер не сделал (`manager_actions`/`gaps`). Но спрашивает он только то,
что уже есть в нашей таблице `call_orders` — а туда заявки потока 1
(реклама) никогда не попадали: их создаёт и дописывает отдельный код
(`app/synergy.py` в asr-vats-megafon, сейчас на сервере «офис»), который
пишет прямо в Synergy и знать о локальной базе дашборда не может. Тот же
класс ошибки уже был у потока 2 до 29.09.2026 (см. `_mirror_order` в
`app/inbound.py`) — там заявки занижали отчёты в семь раз. Здесь то же самое,
просто для другого потока.

Признак настоящей заявки — задокументирован в skill «синержи»: свежая
заявка в Synergy не всегда настоящий лид (CRM сама заводит карточку в
первую секунду звонка), надёжный признак — заполненный тип техники,
`custom-18621`.

`call_orders.call_uid` ссылается на `calls.uid` (включён
`PRAGMA foreign_keys`), а звонка потока 1 в нашей базе нет и не будет — его
обработал другой сервер. Поэтому заводим звонок-заглушку через `save_call()`
с полями, которые естественно гасят её во всех остальных отчётах:
`duration_sec=0`, `record_url=None` (эти два фильтрует почти всё, что
считает «звонки за день» и `fetch_records.py`), `is_demo=2` (не 0 и не 1 —
самодокументируется как служебная строка, не настоящий звонок, и не путается
с демо-режимом приложения), `uid="synthetic-<order_id>"` (нечисловой, с
настоящими uid из ВАТС, это просто число, не совпадёт).

После этого `analyze_orders.py` подхватывает такую заявку сам, без единой
правки в своём коде — обычный `JOIN` на `calls` находит дату как обычно.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient, load_stages  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_schema, save_call, save_call_order  # noqa: E402
from app.stats import local_parts  # noqa: E402

logger = logging.getLogger("discover_ad_orders")

FIELD_TRANSPORT = "custom-18621"
FIELD_REASON = "custom-27969"  # «Причина статистика» — список, не заявка, если из этих:
NOT_A_LEAD = {"Спам", "СПАМ", "Дубль", "Кадры", "Не целевой звонок"}
# «Недозвон» намеренно НЕ исключаем 22.10.2026, по просьбе владельца: сколько
# раз и с какой периодичностью менеджер пытался дозвониться — это он тоже
# хочет видеть в разборе, не прятать.


def find_candidates(client: SynergyClient, since: datetime, known: set[str]) -> list[dict]:
    """Свежие заявки с заполненным типом техники, которых у нас ещё нет."""
    out = []
    page = 1
    while page <= 10:  # с запасом; обычно хватает первой-второй страницы
        payload = client.get("orders", sort="-created-at", include="stage",
                             **{"page[number]": page})
        rows = payload.get("data") or []
        if not rows:
            break
        stop = False
        for row in rows:
            attrs = row.get("attributes") or {}
            created_raw = str(attrs.get("created-at") or "")
            try:
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if created < since:
                stop = True
                break
            order_id = str(row["id"])
            if order_id in known:
                continue
            customs = attrs.get("customs") or {}
            if not customs.get(FIELD_TRANSPORT):
                continue
            reason = customs.get(FIELD_REASON) or []
            reason = reason if isinstance(reason, list) else [reason]
            if NOT_A_LEAD & set(reason):
                continue
            ref = (((row.get("relationships") or {}).get("stage") or {}).get("data") or {})
            out.append({
                "id": order_id,
                "name": attrs.get("name") or "",
                "created_at": created_raw,
                "stage_id": str(ref.get("id") or ""),
            })
        if stop:
            break
        page += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3, help="за сколько последних дней искать")
    ap.add_argument("--order", help="только эта заявка (для ручной проверки)")
    ap.add_argument("--apply", action="store_true", help="записать в call_orders")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        timeout_sec=30.0, min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries)
    stages = load_stages(client)

    known = {str(r[0]) for r in conn.execute("SELECT order_id FROM call_orders")}
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    candidates = find_candidates(client, since, known)
    if args.order:
        candidates = [c for c in candidates if c["id"] == str(args.order)]

    print(f"новых заявок потока 1 за {args.days} дн.: {len(candidates)}")
    for order in candidates:
        stage_name, stage_kind = stages.get(order["stage_id"], ("", ""))
        local_date, local_hour = local_parts(order["created_at"], settings.timezone_offset_hours)
        uid = f"synthetic-{order['id']}"
        print(f"  заявка {order['id']} «{order['name']}», этап «{stage_name}», создана {local_date}")
        if not args.apply:
            continue
        save_call(
            conn, uid=uid, vats_login="", client_phone="", direction="in", status="",
            started_at=order["created_at"], local_date=local_date, local_hour=local_hour,
            wait_sec=0, duration_sec=0, record_url=None, in_group=0, diversion=None,
            is_demo=2, fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        save_call_order(
            conn, order_id=order["id"], call_uid=uid, name=order["name"],
            created_at=order["created_at"], responsible="", stage_name=stage_name,
            stage_kind=stage_kind, amount=None, is_demo=2,
        )
        conn.commit()

    return 0


if __name__ == "__main__":
    sys.exit(main())
