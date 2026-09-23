#!/usr/bin/env python
"""Пересчитать, чей звонок, по добавочному номеру из ВАТС.

    sudo -u claude .venv/bin/python scripts/fix_call_owner.py --days 7
    sudo -u claude .venv/bin/python scripts/fix_call_owner.py --days 7 --apply

Зачем. Synergy пишет в звонок добавочный номер, а чей он — отдельным полем,
и это поле устаревает при переиспользовании добавочного. 23.09.2026
добавочный 766 числился за Ратенковым, хотя там работал уже Никитин: 58 его
звонков за день ушли в чужой счёт, а сам он в отчётах не появился вовсе.

Сборщик починен и новые звонки раскладывает по добавочному. Старые строки он
не перепишет: сохранение звонка идемпотентно и уже записанные не трогает.
Поэтому историю правим отдельно и осознанно — как и признак прозвона.

Считаем заново, а не правим разницу, поэтому запускать можно сколько угодно
раз. Соответствие добавочных берётся из таблицы сотрудников, его приносит
`scripts/sync_vats_users.py`.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector import (  # noqa: E402
    CALL_AUTHOR_FIELD, SynergyClient, iter_calls_for_day, logins_by_ext,
    parse_author, surname_of,
)
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3, help="за сколько последних дней")
    ap.add_argument("--apply", action="store_true", help="записать изменения")
    args = ap.parse_args()

    settings = get_settings()
    conn: sqlite3.Connection = connect(settings.db_path)
    by_ext = logins_by_ext(conn)
    if not by_ext:
        print("добавочные не проставлены — сначала scripts/sync_vats_users.py")
        return 2
    print(f"известно добавочных: {len(by_ext)}")

    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries,
    )
    today = date.today()
    changed = 0
    for back in range(args.days):
        day = (today - timedelta(days=back)).isoformat()
        seen = 0
        for item in iter_calls_for_day(client, day):
            attrs = item["attributes"]
            outgoing = attrs.get("direction") == "outgoing"
            side = attrs.get("src-phone-number") if outgoing else attrs.get("dst-phone-number")
            ext = re.sub(r"\D", "", str(side or ""))
            owner = by_ext.get(ext) if 2 <= len(ext) <= 5 else None
            if not owner:
                continue
            seen += 1
            row = conn.execute("SELECT vats_login FROM calls WHERE uid = ?",
                               (str(item["id"]),)).fetchone()
            if row is None or row["vats_login"] == owner:
                continue
            author, _ = parse_author((attrs.get("customs") or {}).get(CALL_AUTHOR_FIELD))
            changed += 1
            print(f"  {day} звонок {item['id']}: «{row['vats_login']}» → «{owner}» "
                  f"(добавочный {ext}, Synergy считала «{surname_of(author) or '—'}»)")
            if args.apply:
                conn.execute("UPDATE calls SET vats_login = ? WHERE uid = ?",
                             (owner, str(item["id"])))
        if args.apply:
            conn.commit()
        print(f"{day}: с известным добавочным {seen}")

    tail = "" if args.apply else " (сухой прогон, ничего не записано)"
    print(f"\nперевешено звонков: {changed}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
