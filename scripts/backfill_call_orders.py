#!/usr/bin/env python
"""Перенести в зеркало заявки, заведённые по входящим звонкам.

Таблица `call_orders` наполняется проверкой карточек, а та ходит **только по
исходящим** — там речь о прозвоне. Заявки «Пойманная с прослушки» рождаются
из входящих и в зеркало не попадали: 29.09.2026 в CRM их было 34 с начала
недели, а в таблице пять. Любой отчёт по `call_orders` занижал результат
всемеро, причём в нашу же невыгоду.

Впредь связь пишется в момент создания (`inbound._mirror_order`). Этот скрипт
восстанавливает прошлое — связь для него уже лежит в `screens.created_order_id`,
искать по контакту и времени не нужно.

    ./scripts/backfill_call_orders.py
    ./scripts/backfill_call_orders.py --apply

Стадию и ответственного берём из CRM: заявка живёт своей жизнью, и в отчёте
должно стоять её нынешнее состояние, а не то, каким оно было при заведении.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient, load_stages  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, save_call_order  # noqa: E402

logger = logging.getLogger("backfill_orders")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-09-01")
    parser.add_argument("--apply", action="store_true", help="Записать в зеркало.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    rows = conn.execute("""
        SELECT s.call_uid, s.created_order_id AS order_id, k.local_date
        FROM screens s JOIN calls k ON k.uid = s.call_uid
        WHERE COALESCE(s.created_order_id, '') <> ''
          AND k.local_date >= ?
          AND NOT EXISTS (SELECT 1 FROM call_orders o
                          WHERE o.order_id = s.created_order_id AND o.call_uid = s.call_uid)
        ORDER BY k.local_date
    """, (args.since,)).fetchall()
    print(f"заявок по входящим, которых нет в зеркале: {len(rows)}")
    if not rows:
        conn.close()
        return 0
    if not args.apply:
        for r in rows[:5]:
            print(f"  {r['local_date']}  звонок {r['call_uid']} → заявка {r['order_id']}")
        print("\nничего не записано. Для записи: --apply")
        conn.close()
        return 0

    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries)
    stages = load_stages(client)
    перенесено = не_нашлось = 0
    for индекс, r in enumerate(rows, 1):
        try:
            данные = client.get(f"orders/{r['order_id']}", include="responsible,stage")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("заявка %s не прочитана: %s", r["order_id"], str(exc)[:120])
            не_нашлось += 1
            continue
        item = данные.get("data") or {}
        attrs = item.get("attributes") or {}
        rels = item.get("relationships") or {}
        стадия = ((rels.get("stage") or {}).get("data") or {}).get("id") or ""
        имя_стадии, вид = stages.get(str(стадия), ("", ""))
        ответственный = ""
        for включённое in данные.get("included") or []:
            if включённое.get("type") == "users":
                ответственный = str((включённое.get("attributes") or {}).get("as-string") or "")
                break
        save_call_order(
            conn, order_id=str(r["order_id"]), call_uid=r["call_uid"],
            name=str(attrs.get("name") or ""), created_at=str(attrs.get("created-at") or ""),
            responsible=ответственный, stage_name=имя_стадии, stage_kind=вид,
            amount=attrs.get("amount"), is_demo=0)
        conn.commit()
        перенесено += 1
        if индекс % 20 == 0:
            logger.info("  перенесено %s из %s", перенесено, len(rows))
    conn.close()
    print(f"\nперенесено: {перенесено}, не прочиталось: {не_нашлось}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
