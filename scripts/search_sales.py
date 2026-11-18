#!/usr/bin/env python
"""Поиск техники менеджерами продаж: исходящие исполнителям.

    sudo -u claude .venv/bin/python scripts/search_sales.py --days 1
    ... --days 3 --limit 20 --no-sync          только разбор, без обхода Synergy
    ... --uid UID1,UID2 --redo                 один-два звонка заново (платно)

Что делает за один проход:
1. пополняет индекс заявок и подтягивает связи «звонок — заявка», которые помощник
   проставил в Synergy вручную (поле звонка custom-11360) — это эталон для сверки;
2. разбирает новые исходящие звонки менеджеров продаж исполнителям: что у
   исполнителя есть, какая цена, занята ли техника и спросил ли менеджер дату;
3. привязывает звонок к заявке и, по настройкам, предлагает/создаёт карточки в
   базе «Транспорт». Режимы — `DASH_LINK_AI_MODE` и `DASH_TRANSPORT_AI_MODE`:
   по умолчанию `propose`, в Synergy не пишется ничего.

Записи качает `fetch_records.py --search-sales` под `agent`. Сам разбор ходит к
распознаванию и модели, поэтому запускается под `claude`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import search_calls, search_sales  # noqa: E402
from app.collector import SynergyClient, load_stages  # noqa: E402
from app.config import Settings  # noqa: E402
from app.crm_write import CrmWriter  # noqa: E402
from app.db import connect, init_schema  # noqa: E402

logger = logging.getLogger("search_sales")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--day", help="Конкретный день, YYYY-MM-DD.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-sec", type=int, default=0)
    ap.add_argument("--uid", help="Один звонок или несколько через запятую.")
    ap.add_argument("--redo", action="store_true", help="Разобрать заново (платно).")
    ap.add_argument("--no-sync", action="store_true",
                    help="Не обходить Synergy: ни индекс заявок, ни связи помощника.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token, timeout_sec=30.0,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries)

    if not args.no_sync:
        stages = load_stages(client)
        indexed = search_sales.refresh_order_index(conn, client, stages,
                                                   days=settings.link_max_age_days)
        helped = search_sales.pull_helper_links(conn, client, days=3)
        print(f"индекс заявок: обновлено {indexed}; связей помощника за 3 дня: {helped}")

    records = Path(settings.records_dir)
    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()
    min_sec = args.min_sec or settings.search_min_duration_sec

    if args.uid:
        uids = [u.strip() for u in args.uid.split(",") if u.strip()]
        rows = conn.execute(
            f"""SELECT k.uid, t.text AS transcript_text, c.call_uid AS checked
                  FROM calls k LEFT JOIN transcripts t ON t.call_uid = k.uid
                  LEFT JOIN search_checks c ON c.call_uid = k.uid
                 WHERE k.uid IN ({",".join("?" * len(uids))})""", uids).fetchall()
    else:
        rows = conn.execute(
            """SELECT k.uid, t.text AS transcript_text, c.call_uid AS checked
                 FROM calls k LEFT JOIN transcripts t ON t.call_uid = k.uid
                 LEFT JOIN search_checks c ON c.call_uid = k.uid
                WHERE k.local_date BETWEEN ? AND ?
                  AND k.direction = 'out' AND k.duration_sec >= ?
                  AND k.vats_login IN (SELECT vats_login FROM managers
                                        WHERE dept = ? AND active = 1)
                  AND substr(replace(replace(replace(k.client_phone,'+',''),' ',''),'-',''), -10)
                      NOT IN (SELECT phone10 FROM numbers WHERE category = 'заказчик')
                ORDER BY k.started_at DESC""",
            (since, until, min_sec, settings.search_sales_dept)).fetchall()

    todo = [r for r in rows
            if (not r["checked"] or args.redo)
            and (r["transcript_text"] or (records / f"{r['uid']}.mp3").exists())]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{since}…{until}: к разбору {len(todo)} разговоров (всего подходящих {len(rows)})")

    writes = "create" in (settings.transport_ai_mode, settings.link_ai_mode)
    writer = CrmWriter(client, apply=writes)
    done = failed = searched = 0
    started = time.monotonic()
    for row in todo:
        uid = row["uid"]
        try:
            outcome = search_calls.check_call(conn, settings, uid, records / f"{uid}.mp3",
                                              text=row["transcript_text"] or None,
                                              require_supplier=True)
            if not outcome.get("checked"):
                continue
            extra = search_sales.after_check(conn, settings, writer, uid, outcome)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("%s: не разобран — %s: %s", uid, type(exc).__name__, exc)
            failed += 1
            continue
        done += 1
        if not extra.get("search"):
            who = (outcome.get("heard") or {}).get("counterpart") or ""
            logger.info("  %s: в поиск не входит (собеседник: %s)", uid, who or "не определён")
            continue
        searched += 1
        heard = outcome.get("heard") or {}
        link = extra.get("link") or {}
        logger.info("  %s: искал «%s»; заявка %s (%s: %s); цен %d, занято %d; в базу: %s",
                    uid, heard.get("asked") or "—", link.get("order_id") or "—",
                    link.get("confidence") or "нет", link.get("note") or "",
                    len(heard.get("prices") or []), len(heard.get("busy") or []),
                    ", ".join(f"{t['type']} ({t['mode']})" for t in extra.get("transport") or []) or "—")

    print(f"разобрано {done}, из них поиск техники {searched}, не вышло {failed}, "
          f"за {time.monotonic() - started:.0f} с")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
