#!/usr/bin/env python
"""Обновить сведения о заявках: стадия, сумма, ответственный — и найти новые.

    sudo -u claude .venv/bin/python scripts/refresh_orders.py          показать
    sudo -u claude .venv/bin/python scripts/refresh_orders.py --apply  записать

Зачем. Проверка карточки одноразовая: `check_cards` берёт только звонки, у
которых её ещё нет. Поэтому стадия заявки записывалась один раз — через
минуты после разговора — и больше не менялась. Заявка, закрытая в сделку
через неделю, в дашборде так и оставалась «Новым»: 22.09.2026 из 27 заявок
устарели четыре, и две из них были уже «Сделка».

Делаем два прохода, оба без модели и потому дешёвые:

1. **Известные заявки** — спрашиваем каждую по идентификатору и обновляем
   стадию, сумму и ответственного. Один запрос на заявку.
2. **Новые заявки** — по звонкам **прозвона** за последние `--days` дней
   перечитываем заявки контакта. Ловит то, что менеджер завёл не сразу, а
   через час или назавтра: при одноразовой проверке такая заявка не
   появлялась никогда. Только прозвон и только три дня — потому что это
   запрос на каждый звонок, а звонков компании полтысячи в день; заявки
   входящих отслеживаются своим путём, через `inbound_checks`.

Стадии в CRM переименовывают, поэтому вид стадии (`won`/`lost`) берём из
справочника, а не угадываем по названию.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector import (  # noqa: E402
    SynergyClient, load_stages, orders_after_call,
)
from app.config import get_settings  # noqa: E402
from app.db import connect, save_call_order  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="записать изменения")
    ap.add_argument("--days", type=int, default=3,
                    help="за сколько последних дней искать новые заявки")
    args = ap.parse_args()

    settings = get_settings()
    conn: sqlite3.Connection = connect(settings.db_path)
    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
    stages = load_stages(client)

    # --- 1. Обновить то, что уже знаем.
    known = conn.execute(
        "SELECT order_id, call_uid, name, created_at, responsible, stage_name, amount "
        "FROM call_orders"
    ).fetchall()
    changed = 0
    for row in known:
        try:
            data = (client.get(f"orders/{row['order_id']}", include="stage").get("data") or {})
        except Exception:  # noqa: BLE001 — заявку могли удалить, это не повод падать
            print(f"  заявка {row['order_id']}: не прочиталась")
            continue
        attrs = data.get("attributes") or {}
        ref = (((data.get("relationships") or {}).get("stage") or {}).get("data") or {})
        stage_name, stage_kind = stages.get(str(ref.get("id") or ""), ("", ""))
        if stage_name == (row["stage_name"] or ""):
            continue
        changed += 1
        print(f"  заявка {row['order_id']}: «{row['stage_name']}» → «{stage_name}»", flush=True)
        if args.apply:
            save_call_order(
                conn, order_id=str(row["order_id"]), call_uid=row["call_uid"],
                name=row["name"], created_at=row["created_at"],
                responsible=str(attrs.get("responsible") or row["responsible"] or ""),
                stage_name=stage_name, stage_kind=stage_kind,
                amount=float(attrs.get("amount") or 0.0), is_demo=0,
            )
    if args.apply:
        conn.commit()

    # --- 2. Найти заявки, заведённые позже проверки карточки.
    calls = conn.execute(
        """SELECT k.uid, k.started_at, c.contact_id
             FROM calls k JOIN card_checks c ON c.call_uid = k.uid
            WHERE k.local_date >= date('now', ?) AND c.contact_id IS NOT NULL
              AND k.in_group = 1
            ORDER BY k.started_at DESC""",
        (f"-{args.days} day",),
    ).fetchall()
    seen = {str(r["order_id"]) for r in known}
    found = 0
    for call in calls:
        try:
            fresh = orders_after_call(client, str(call["contact_id"]), call["started_at"],
                                      settings.order_window_hours, stages)
        except Exception:  # noqa: BLE001
            continue
        for order in fresh:
            if str(order["order_id"]) in seen:
                continue
            found += 1
            seen.add(str(order["order_id"]))
            print(f"  новая заявка {order['order_id']} по звонку {call['uid']}: "
                  f"«{order['name']}», стадия «{order['stage_name']}»")
            if args.apply:
                save_call_order(conn, call_uid=call["uid"], is_demo=0, **order)
    if args.apply:
        conn.commit()

    tail = "" if args.apply else " (сухой прогон, ничего не записано)"
    print(f"\nзаявок известно {len(known)}, стадий изменилось {changed}, "
          f"новых найдено {found}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
