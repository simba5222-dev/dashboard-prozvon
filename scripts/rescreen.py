#!/usr/bin/env python
"""Пробный просев: прогнать разговоры заново, ничего не меняя в работе.

    sudo -u claude .venv/bin/python scripts/rescreen.py --day 2026-10-01
    ... --limit 20          начать с двадцати
    ... --engine openai     прогнать прежней головой, для сравнения

**Заявки не создаются, боевой просев не трогается.** Результат ложится в
отдельную таблицу и показывается на `/trial` рядом с тем, что система
решила в тот день. Нужно, чтобы владелец мог посмотреть, как новая голова
и новые правила разобрали вчерашний день, прежде чем доверить им работу.

Расшифровку заново не делаем: берём сохранённое начало разговора. Платим
только за голову — около 0,26 ₽ за звонок.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_schema, save_trial  # noqa: E402

logger = logging.getLogger("прогон")


def решение(разбор: dict) -> bool:
    """Завелась бы заявка. Складывается из двух полей, а не из одного."""
    return bool(разбор.get("is_request") and not разбор.get("about_existing"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Пробный просев без создания заявок.")
    ap.add_argument("--day", help="день, ГГГГ-ММ-ДД (по умолчанию вчера)")
    ap.add_argument("--days", type=int, default=1, help="сколько дней подряд")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--engine", default="", help="openai или yandex; по умолчанию из настроек")
    ap.add_argument("--redo", action="store_true", help="перепрогнать уже прогнанные")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = get_settings()
    conn = connect(s.db_path)
    init_schema(conn)

    until = args.day or (date.today() - timedelta(days=1)).isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()
    голова = args.engine or s.screen_engine
    яндексом = голова == "yandex"

    условие = "" if args.redo else "AND t.call_uid IS NULL"
    rows = conn.execute(
        f"""SELECT s.call_uid, s.head_text FROM screens s
            JOIN calls k ON k.uid = s.call_uid
            LEFT JOIN screen_trials t ON t.call_uid = s.call_uid
            WHERE k.local_date BETWEEN ? AND ? AND s.head_text <> '' {условие}
            ORDER BY k.started_at DESC""",
        (since, until),
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("прогонять нечего")
        return 0

    цена = 0.26 if яндексом else 0.79
    print(f"{since}…{until}: прогоняю {len(rows)} разговоров головой «{голова}», "
          f"примерно {len(rows) * цена:.0f} ₽\n")

    заявок = 0
    for i, r in enumerate(rows, 1):
        разбор = analyzer.screen_call(
            r["head_text"], "",
            api_key=(s.yandex_api_key if яндексом else s.openai_api_key) or "",
            model=s.yandex_model if яндексом else s.analysis_model,
            engine=голова, folder=s.yandex_folder or "",
            own_company=s.own_company,
        )
        save_trial(conn, call_uid=r["call_uid"], engine=голова,
                   head_text=r["head_text"],
                   verdict_json=json.dumps(разбор, ensure_ascii=False),
                   made_at=datetime.now(timezone.utc).isoformat())
        conn.commit()
        если = решение(разбор)
        заявок += если
        print(f"  [{i}/{len(rows)}] {r['call_uid']}: "
              f"{'ЗАЯВКА' if если else 'не заявка'} · "
              f"{разбор.get('equipment') or 'техника не названа'}")

    print(f"\nпрогнано {len(rows)}, заявок нашлось {заявок}")
    print("смотреть: /trial — заявки НЕ созданы, боевой просев не тронут")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
