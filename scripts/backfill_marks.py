#!/usr/bin/env python
"""Проставить метки на заявки, заведённые разбором, и на их звонки.

    sudo -u claude .venv/bin/python scripts/backfill_marks.py          показать
    sudo -u claude .venv/bin/python scripts/backfill_marks.py --apply  записать

Метки завели 22.09.2026 по просьбе РОПов: открывая заявку, они видят в
активности несколько звонков и не понимают, какой из них её породил. Ставим
две, с обеих сторон:

- у заявки «Пойманная с прослушки» (custom-30614) — когда был звонок, сколько
  длился, с какого номера;
- у самого звонка «Заявка по этому звонку» (custom-30615) — номер заявки.

Пишется только эти два поля. Стадия, название, ответственный и всё остальное
не трогаются: заявку ведёт человек.

Запускать можно сколько угодно раз — значения перезаписываются теми же.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector import SynergyClient  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crm_write import CrmWriter, FIELD_CAUGHT  # noqa: E402
from app.db import connect  # noqa: E402
from app.inbound import caught_mark  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="записать в CRM")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    settings = get_settings()
    conn: sqlite3.Connection = connect(settings.db_path)
    rows = conn.execute(
        """SELECT s.call_uid, s.created_order_id, k.started_at, k.duration_sec,
                  k.client_phone
             FROM screens s JOIN calls k ON k.uid = s.call_uid
            WHERE s.created_order_id IS NOT NULL
            ORDER BY k.started_at"""
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
    writer = CrmWriter(client, apply=args.apply)
    done = missed = 0
    for row in rows:
        uid, order_id = row["call_uid"], str(row["created_order_id"])
        mark = caught_mark(uid, {"start": row["started_at"],
                                 "duration": row["duration_sec"],
                                 "client": row["client_phone"]},
                           settings.timezone_offset_hours)
        if not args.apply:
            print(f"  заявка {order_id}: {mark}")
            continue
        writer.update_customs(order_id, {FIELD_CAUGHT: mark})
        call_id = writer.mark_call(uid, f"заявка {order_id}")
        if call_id:
            done += 1
            print(f"  заявка {order_id} ← звонок {call_id}: помечены обе стороны")
        else:
            missed += 1
            print(f"  заявка {order_id}: звонок {uid} в Synergy не нашёлся")
    if args.apply:
        print(f"\nпомечено {done}, звонок не нашёлся у {missed}")
    else:
        print(f"\nсухой прогон: {len(rows)} заявок, ничего не записано. "
              "Повторите с --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
