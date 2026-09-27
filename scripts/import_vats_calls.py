#!/usr/bin/env python
"""Загрузить звонки из выгрузки истории ВАТС в нашу базу.

Сбор через Synergy работает с 16.09.2026 — всё, что было раньше, в нашей
базе отсутствует. Историю ВАТС отдаёт целиком одним запросом
(`GET /history/json` с `start` и `end`, только с российского сервера), и
эти звонки можно разобрать задним числом.

    ./scripts/import_vats_calls.py --file data/vats-3m.json.gz --since 2026-09-01 --until 2026-09-16
    ./scripts/import_vats_calls.py ... --apply

Отбираем то же, что ловит живой конвейер: входящие менеджерам продаж на их
**прямые** номера. Звонки на общие и рекламные номера не берём — по ним
работает другой сценарий, и заявка там заводится иначе.

**Логин в ВАТС и наш логин — разные вещи.** В выгрузке стоит латинский
логин учётки (`s.nikitin`), у нас — фамилия. Связь идёт через добавочный
номер: он есть и в справочнике учёток, и в таблице сотрудников.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, save_call  # noqa: E402

MSK = timezone(timedelta(hours=3))


def digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="data/vats-3m.json.gz")
    parser.add_argument("--users", default="data/vats-users.json")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--min-sec", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Записать в базу.")
    args = parser.parse_args()

    path = Path(args.file)
    opener = gzip.open if path.suffix == ".gz" else open
    rows = json.load(opener(path, "rt", encoding="utf-8"))
    accounts = {u["login"]: u for u in json.loads(Path(args.users).read_text(encoding="utf-8"))}

    settings = Settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    by_ext = {str(r["ext"]).strip(): r["vats_login"] for r in conn.execute(
        "SELECT ext, vats_login FROM managers WHERE dept = ? AND active = 1 "
        "AND ext IS NOT NULL AND ext <> ''", (settings.sales_dept,))}
    direct = {digits(r["phone"])[-10:] for r in conn.execute(
        "SELECT phone FROM managers WHERE phone IS NOT NULL AND phone <> ''")}

    taken = skipped = new = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if row.get("type") != "in":
            continue
        started = str(row.get("start") or "")
        day = started[:10]
        if not (args.since <= day <= args.until):
            continue
        if (row.get("duration") or 0) < args.min_sec:
            continue
        account = accounts.get(str(row.get("user") or ""))
        if not account:
            continue
        login = by_ext.get(str(account.get("ext") or "").strip())
        if not login:
            continue
        # Только прямые номера менеджеров: на общие и рекламные заявка
        # заводится по другому сценарию.
        if digits(row.get("diversion"))[-10:] not in direct:
            skipped += 1
            continue
        taken += 1
        if not args.apply:
            continue
        # Время в выгрузке в UTC, а день считаем по Москве — иначе вечерние
        # звонки уезжают во вчера, и отчёты за день не сходятся.
        try:
            moment = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(MSK)
        except ValueError:
            continue
        new += int(save_call(
            conn, uid=str(row.get("uid")), vats_login=login,
            client_phone=digits(row.get("client")), direction="in",
            status=str(row.get("status") or ""), started_at=moment.isoformat(),
            local_date=moment.date().isoformat(), local_hour=moment.hour,
            wait_sec=int(row.get("wait") or 0), duration_sec=int(row.get("duration") or 0),
            record_url=str(row.get("record") or ""), in_group=0,
            diversion=digits(row.get("diversion")), is_demo=0, fetched_at=now))
    conn.commit()
    conn.close()
    print(f"{args.since}…{args.until}: подходящих звонков {taken}, "
          f"на общие номера пропущено {skipped}")
    print(f"добавлено новых: {new}" if args.apply else "это разбор без записи, для записи: --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
