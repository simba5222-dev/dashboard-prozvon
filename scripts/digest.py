#!/usr/bin/env python
"""Сводка по разобранным разговорам: что упущено и где потеряны деньги.

    sudo -u claude .venv/bin/python scripts/digest.py --days 14
    ... --field потребность     только пункты про потребность
    ... --limit 30              сколько разговоров показать в списках

Отчёт `/report` отвечает на вопрос «что было в этом разговоре». Сводка
отвечает на другой: «что из всего массива стоит разобрать руками». Поэтому
на первом месте — разговоры, где клиент назвал потребность, а заявки нет:
это единственная строка, за которой стоят деньги, а не дисциплина.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402


def rows_of(conn, since: str, until: str) -> list[dict]:
    out = []
    for row in conn.execute(
        """
        SELECT k.uid, k.local_date, k.started_at, k.client_phone, k.duration_sec,
               c.contact_name, c.company_name, c.need_value, c.task_created,
               t.analysis_json,
               (SELECT COUNT(*) FROM call_orders o WHERE o.call_uid = k.uid) AS orders_made
        FROM calls k
        JOIN transcripts t ON t.call_uid = k.uid
        LEFT JOIN card_checks c ON c.call_uid = k.uid
        WHERE k.local_date BETWEEN ? AND ? AND t.analysis_json IS NOT NULL
        ORDER BY k.started_at DESC
        """,
        (since, until),
    ):
        item = dict(row)
        try:
            item["analysis"] = json.loads(item["analysis_json"])
        except ValueError:
            continue
        out.append(item)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Сводка по разобранным разговорам.")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--day", help="по какую дату, ГГГГ-ММ-ДД")
    ap.add_argument("--field", help="показать пункты только по этому полю")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    settings = get_settings()
    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()

    conn = connect(settings.db_path)
    rows = rows_of(conn, since, until)
    if not rows:
        print(f"{since}…{until}: разобранных разговоров нет")
        return 0

    live = [r for r in rows if not r["analysis"].get("unusable")]
    quality = [r["analysis"]["call_quality"] for r in live if r["analysis"].get("call_quality")]
    missed_all = [(r, m) for r in rows for m in r["analysis"].get("missed") or []]

    print(f"{since}…{until}: разобрано {len(rows)} разговоров, "
          f"содержательных {len(live)}, пустых {len(rows) - len(live)}")
    if quality:
        print(f"средняя оценка разговора: {sum(quality) / len(quality):.1f} из 5")
    print(f"разговоров с упущенным в CRM: "
          f"{len({r['uid'] for r, _ in missed_all})}, пунктов {len(missed_all)}")

    equipment = Counter(
        r["analysis"].get("equipment", "").strip().lower()
        for r in live if r["analysis"].get("equipment")
    )
    if equipment:
        print("\nО какой технике говорили:")
        for name, count in equipment.most_common(10):
            print(f"  {count:>3}  {name}")

    questions = Counter(
        q for r in live for q in r["analysis"].get("questions_missed") or []
    )
    if questions:
        print("\nОбязательные вопросы, которые чаще всего не задают:")
        for question, count in questions.most_common(10):
            print(f"  {count:>3}  {question}")

    upsell = Counter(u for r in live for u in r["analysis"].get("upsell_missed") or [])
    if upsell:
        print("\nЧто стоило предложить в дополнение и не предложили:")
        for item, count in upsell.most_common(8):
            print(f"  {count:>3}  {item}")

    by_field = Counter(m["field"] for _, m in missed_all)
    if by_field:
        print("\nЧего не хватает в карточках:")
        for field, count in by_field.most_common():
            print(f"  {count:>3}  {field}")

    # Потребность прозвучала, а заявки нет — то, ради чего всё это затевалось.
    lost = [
        r for r in live
        if r["analysis"].get("client_need") and not r["orders_made"]
    ]
    print(f"\nПотребность прозвучала, заявки нет — {len(lost)} разговоров:")
    for r in lost[: args.limit]:
        who = r["contact_name"] or "без имени"
        firm = f", {r['company_name']}" if r["company_name"] else ""
        mark = "" if r["task_created"] else ", задачи тоже нет"
        print(f"  {r['local_date']} {r['started_at'][11:16]} {who}{firm} "
              f"({r['client_phone']}, {r['duration_sec']} с){mark}")
        print(f"      нужно: {r['analysis']['client_need']}")
        if r["need_value"]:
            print(f"      в карточке: {r['need_value']}")

    if args.field:
        picked = [(r, m) for r, m in missed_all if m["field"].lower() == args.field.lower()]
        print(f"\nПункты «{args.field}» — {len(picked)}:")
        for r, m in picked[: args.limit]:
            print(f"  {r['local_date']} {r['started_at'][11:16]} "
                  f"{r['contact_name'] or r['client_phone']}: {m['value']}")
            print(f"      «{m['quote'][:110]}»")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
