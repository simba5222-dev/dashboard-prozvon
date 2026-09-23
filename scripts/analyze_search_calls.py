#!/usr/bin/env python
"""Разбор разговоров отдела поиска техники: что поставщик назвал своим.

    ./scripts/analyze_search_calls.py --days 1           за сегодня
    ./scripts/analyze_search_calls.py --days 7 --limit 20
    ./scripts/analyze_search_calls.py --recheck --days 14

Записи должны быть скачаны — этим занимается `fetch_records.py --search`
под пользователем `agent`. Сам разбор ходит к распознаванию и к модели,
поэтому запускается под `claude`.

**Про `--recheck`.** Отметка «не занесено» живёт ровно до того момента, пока
менеджер не занесёт технику в карточку. Пересчёт берёт уже разобранные
разговоры и сверяет их с карточками заново, не тратя ни распознавание, ни
модель. Без него отчёт показывал бы вчерашние упрёки по исправленным
карточкам — а это верный способ отучить людей ему верить.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import search_calls  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, phone10, save_search_check  # noqa: E402

logger = logging.getLogger("analyze_search_calls")


def recheck(conn, settings) -> int:
    """Пересчитать вердикты по текущим карточкам, без модели и распознавания."""
    rows = conn.execute("SELECT * FROM search_checks ORDER BY local_date DESC").fetchall()
    changed = 0
    for row in rows:
        try:
            has = json.loads(row["offered"] or "[]")
        except ValueError:
            continue
        cards = search_calls.supplier_cards(conn, row["phone10"])
        result = search_calls.compare(
            {"has": has, "more_unnamed": row["more_unnamed"] or ""}, cards)
        if result["verdict"] == row["verdict"] and \
                json.dumps(result["missing"], ensure_ascii=False) == (row["missing"] or "[]"):
            continue
        save_search_check(
            conn, call_uid=row["call_uid"], local_date=row["local_date"],
            vats_login=row["vats_login"], phone10=row["phone10"],
            contact_name=(cards[0]["contact_name"] if cards else row["contact_name"]),
            asked_type=row["asked_type"], offered=row["offered"],
            known=json.dumps(result["known"], ensure_ascii=False),
            missing=json.dumps(result["missing"], ensure_ascii=False),
            verdict=result["verdict"],
            checked_at=datetime.now(timezone.utc).isoformat(), is_demo=0,
            more_unnamed=row["more_unnamed"] or "",
        )
        changed += 1
    conn.commit()
    print(f"пересчитано вердиктов: {changed} из {len(rows)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--day", help="Конкретный день, YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-sec", type=int, default=0,
                        help="Короче этого не разбираем.")
    parser.add_argument("--uid", help="Один звонок или несколько через запятую.")
    parser.add_argument("--redo", action="store_true", help="Разобрать заново.")
    parser.add_argument("--recheck", action="store_true",
                        help="Только сверить старые разборы с карточками сейчас.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    conn = connect(settings.db_path)
    init_schema(conn)

    if args.recheck:
        return recheck(conn, settings)

    records = Path(settings.records_dir)
    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()
    min_sec = args.min_sec or settings.search_min_duration_sec

    if args.uid:
        uids = [u.strip() for u in args.uid.split(",") if u.strip()]
        rows = conn.execute(
            f"""SELECT k.uid, k.duration_sec, t.text AS transcript_text,
                       c.call_uid AS checked
                  FROM calls k
                  LEFT JOIN transcripts t ON t.call_uid = k.uid
                  LEFT JOIN search_checks c ON c.call_uid = k.uid
                 WHERE k.uid IN ({",".join("?" * len(uids))})""", uids).fetchall()
    else:
        rows = conn.execute(
            """SELECT k.uid, k.duration_sec, t.text AS transcript_text,
                      c.call_uid AS checked
                 FROM calls k
                 LEFT JOIN transcripts t ON t.call_uid = k.uid
                 LEFT JOIN search_checks c ON c.call_uid = k.uid
                WHERE k.local_date BETWEEN ? AND ?
                  AND k.direction = 'out'
                  AND k.duration_sec >= ?
                  AND k.vats_login IN (SELECT vats_login FROM managers
                                        WHERE dept = ? AND active = 1)
                ORDER BY k.started_at DESC""",
            (since, until, min_sec, settings.search_dept)).fetchall()

    todo = []
    for row in rows:
        if row["checked"] and not args.redo:
            continue
        if not row["transcript_text"] and not (records / f"{row['uid']}.mp3").exists():
            continue
        todo.append(row)
    if args.limit:
        todo = todo[: args.limit]
    print(f"{since}…{until}: к разбору {len(todo)} разговоров")

    done = failed = 0
    started = time.monotonic()
    for row in todo:
        uid = row["uid"]
        try:
            outcome = search_calls.check_call(
                conn, settings, uid, records / f"{uid}.mp3",
                text=row["transcript_text"] or None,
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("%s: не разобран — %s: %s", uid, type(exc).__name__, exc)
            failed += 1
            continue
        done += int(bool(outcome.get("checked")))
        if outcome.get("missing"):
            logger.info("  %s: не занесено — %s", uid, ", ".join(outcome["missing"]))

    spent = time.monotonic() - started
    print(f"разобрано {done}, не вышло {failed}, за {spent:.0f} с")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
