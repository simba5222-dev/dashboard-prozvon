#!/usr/bin/env python
"""Подтянуть ссылки на запись из истории ВАТС напрямую — для звонков,
которые до нас дошли только через опрос Synergy.

    sudo -u claude .venv/bin/python scripts/backfill_missing_records.py --days 10
    ... --apply     действительно записать (без него — сухой прогон)

08.10.2026: из состоявшихся входящих на прямые номера продаж за неделю
196 остались без ссылки на запись. Разбор показал: это не настройка ВАТС
у конкретных менеджеров, а источник данных — звонок, найденный только через
опрос Synergy (не через наш вебхук), в самой Synergy почти всегда приходит
с пустым полем «запись». У ВАТС она есть, просто не долетела до нас вебхуком.

Достаём через `/lookup-records` на боевом сервере (`91-220-109-49.nip.io`) —
он ходит в ВАТС напрямую командой `history`. Сшивка — по телефону и минуте
начала звонка, тот же приём, что и везде в этом проекте: у двух источников
разные идентификаторы звонка, общего ключа между ними нет.

После бэкфилла запись video в screen_inbound.py подхватит её на обычном
прогоне, если звонок не старше двух дней; для более старых — звать
screen_inbound.py отдельно с бОльшим --days.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx                                     # noqa: E402

from app.config import get_settings               # noqa: E402
from app.db import connect, init_schema, phone10   # noqa: E402

logger = logging.getLogger("backfill_records")

ДОПУСК_СЕК = 120  # расхождение времени начала между ВАТС и Synergy


def fetch_day(base_url: str, token: str, start: datetime, end: datetime) -> list[dict]:
    r = httpx.get(
        f"{base_url.rstrip('/')}/lookup-records",
        params={"start": start.isoformat(), "end": end.isoformat()},
        headers={"X-Lookup-Token": token}, timeout=60,
    )
    r.raise_for_status()
    return r.json()["rows"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="за сколько последних дней")
    ap.add_argument("--apply", action="store_true", help="действительно записать")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    if not (settings.prod_lookup_url and settings.prod_lookup_token):
        logger.error("не настроен доступ к боевому серверу — .env читается только под claude")
        return 1

    conn = connect(settings.db_path)
    init_schema(conn)

    since_date = (datetime.now(timezone.utc).date() - timedelta(days=args.days)).isoformat()
    rows = conn.execute(
        """SELECT uid, client_phone, started_at FROM calls
           WHERE direction='in' AND is_demo=0 AND duration_sec>=10
           AND (record_url IS NULL OR record_url='') AND local_date >= ?""",
        (since_date,)).fetchall()
    if not rows:
        print("нечего подтягивать — у всех звонков уже есть ссылка")
        return 0
    print(f"звонков без ссылки за {args.days} дней: {len(rows)}")

    missing: dict[str, list[tuple[str, str]]] = {}
    for uid, phone, started_at in rows:
        missing.setdefault(phone10(phone), []).append((uid, started_at))

    today = datetime.now(timezone.utc).date()
    found = updated = 0
    for d in range(args.days + 1):
        day = today - timedelta(days=d)
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1) - timedelta(seconds=1)
        try:
            vats_rows = fetch_day(settings.prod_lookup_url, settings.prod_lookup_token,
                                  day_start, day_end)
        except httpx.HTTPError as exc:
            logger.warning("день %s не прочитался: %s", day, exc)
            continue
        print(f"{day}: в истории ВАТС {len(vats_rows)} звонков с записью")
        for r in vats_rows:
            phone = phone10(r.get("client") or "")
            candidates = missing.get(phone)
            if not candidates:
                continue
            try:
                vats_time = datetime.fromisoformat(r["start"].replace("Z", "+00:00"))
            except (ValueError, KeyError):
                continue
            for i, (uid, started_at) in enumerate(candidates):
                try:
                    call_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if abs((vats_time - call_time).total_seconds()) <= ДОПУСК_СЕК:
                    found += 1
                    print(f"  {uid}: найдена запись")
                    if args.apply:
                        conn.execute("UPDATE calls SET record_url=? WHERE uid=?",
                                    (r["record"], uid))
                        updated += 1
                    del candidates[i]
                    break

    if args.apply:
        conn.commit()
    print(f"\nнайдено совпадений: {found}, записано: {updated}" +
          ("" if args.apply else "   (сухой прогон, ничего не записано)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
