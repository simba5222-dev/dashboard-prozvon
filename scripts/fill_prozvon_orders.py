#!/usr/bin/env python
"""Дописать в заявки прозвона содержание разговора, из которого они выросли.

    sudo -u claude .venv/bin/python scripts/fill_prozvon_orders.py          показать
    sudo -u claude .venv/bin/python scripts/fill_prozvon_orders.py --apply  записать

Зачем. Заявку по итогам прозвона заводит менеджер сам, и в ней не видно, о чём
он говорил с клиентом: чтобы это понять, РОП шёл слушать запись. Теперь в
заявку дописывается выжимка, расшифровка, рекомендации и оценка разговора, а
сам звонок помечается номером заявки — чтобы в активности было видно, какой
именно разговор её породил.

Заявка чужая: стадию, название и ответственного не трогаем. Пустые значения не
пишем — затирать чужое пустотой нельзя. Запускать можно сколько угодно раз.

Разбор берётся из базы. Разговор, который ещё не разобран, пропускается: его
подберёт следующий запуск.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector import SynergyClient  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crm_write import (  # noqa: E402
    FIELD_CALL_SCORE, FIELD_MANAGER_RECOMMENDATIONS, FIELD_SUMMARY,
    FIELD_TRANSCRIPT, CrmWriter,
)
from app.db import connect  # noqa: E402


def customs_of(analysis: dict, transcript: str) -> dict:
    """Что из разбора кладём в заявку.

    Разбор прозвона отвечает на другие вопросы, чем разбор входящего, поэтому
    и поля другие: не «какая техника нужна», а «о чём договорились» и «что
    менеджеру стоило сделать иначе».
    """
    customs: dict = {}
    if (analysis.get("summary") or "").strip():
        customs[FIELD_SUMMARY] = analysis["summary"].strip()
    if transcript.strip():
        customs[FIELD_TRANSCRIPT] = transcript.strip()
    advice = analysis.get("recommendations") or []
    if isinstance(advice, list) and advice:
        customs[FIELD_MANAGER_RECOMMENDATIONS] = "\n".join(f"— {a}" for a in advice)
    score = analysis.get("call_quality")
    if isinstance(score, int):
        customs[FIELD_CALL_SCORE] = score
    return customs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="записать в CRM")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    settings = get_settings()
    conn: sqlite3.Connection = connect(settings.db_path)
    rows = conn.execute(
        """SELECT o.order_id, o.call_uid, o.name, t.text, t.analysis_json
             FROM call_orders o
             JOIN transcripts t ON t.call_uid = o.call_uid
             JOIN calls k ON k.uid = o.call_uid
            WHERE t.analysis_json IS NOT NULL AND k.in_group = 1
            GROUP BY o.order_id
            ORDER BY o.created_at"""
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
    writer = CrmWriter(client, apply=args.apply)
    filled = skipped = 0
    for row in rows:
        try:
            analysis = json.loads(row["analysis_json"])
        except (TypeError, ValueError):
            skipped += 1
            continue
        customs = customs_of(analysis, row["text"] or "")
        if not customs:
            skipped += 1
            continue
        if not args.apply:
            head = (analysis.get("summary") or "").strip()[:90]
            print(f"  заявка {row['order_id']} ← звонок {row['call_uid']}: {head}")
            continue
        writer.update_customs(str(row["order_id"]), customs)
        writer.mark_call(row["call_uid"], f"заявка {row['order_id']}")
        filled += 1
        print(f"  заявка {row['order_id']} ← звонок {row['call_uid']}: дописана")

    if args.apply:
        print(f"\nдописано {filled}, пропущено {skipped}")
    else:
        print(f"\nсухой прогон: {len(rows)} заявок с разбором, ничего не записано. "
              "Повторите с --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
