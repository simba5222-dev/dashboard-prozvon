#!/usr/bin/env python
"""Синхронизация звонков Авито (CallTracking) в нашу базу. Только чтение из Авито.

    sudo -u claude .venv/bin/python scripts/avito_sync.py --days 3          свежее (для таймера)
    sudo -u claude .venv/bin/python scripts/avito_sync.py --since 2026-01-01 вся история с начала года

Идемпотентно: повторная загрузка того же звонка (callId) обновляет строку, дублей не будет.
Пишет в базу дашборда — запускать от claude (владелец базы).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.avito import AvitoClient, AvitoError, to_msk_date  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, save_avito_call  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3, help="за сколько последних дней")
    ap.add_argument("--since", help="с даты ГГГГ-ММ-ДД (вместо --days)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    settings = Settings()
    client = AvitoClient(settings.avito_client_id or "", settings.avito_client_secret or "", settings.avito_user_id)
    now = datetime.now(timezone.utc)
    start = (datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else now - timedelta(days=args.days))
    conn = connect(settings.db_path)
    init_schema(conn)
    stamp = now.isoformat(timespec="seconds")
    n = 0
    try:
        for c in client.calls(start, now):
            save_avito_call(
                conn, call_id=int(c["callId"]), call_time=str(c["callTime"]), local_date=to_msk_date(str(c["callTime"])),
                buyer_phone=str(c.get("buyerPhone") or ""), seller_phone=str(c.get("sellerPhone") or ""),
                virtual_phone=str(c.get("virtualPhone") or ""), item_id=c.get("itemId"),
                talk_sec=int(c.get("talkDuration") or 0), wait_sec=float(c.get("waitingDuration") or 0), fetched_at=stamp)
            n += 1
            if n % 100 == 0:
                conn.commit()
                print(f"  загружено {n}…", flush=True)
    except AvitoError as exc:
        conn.commit()
        print(f"остановились на ошибке Авито (уже сохранено {n}): {exc}", flush=True)
        return 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM avito_calls").fetchone()[0]
    print(f"{start.date()} — {now.date()}: получено {n}, всего в таблице {total}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
