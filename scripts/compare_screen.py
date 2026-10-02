#!/usr/bin/env python
"""Сравнить две головы на одних и тех же звонках просева.

    sudo -u claude .venv/bin/python scripts/compare_screen.py --limit 30

Переключать голову вслепую нельзя: у просева семь полей, и каждое решает,
заведётся заявка или нет. Берём уже просеянные разговоры, прогоняем их
второй головой и смотрим, где ответы разошлись.

Расшифровку заново не делаем — берём сохранённое начало разговора. Платим
только за вторую голову.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402

ПОЛЯ = ("решение", "equipment", "asked_by", "other_side",
        "work_described_by", "about_existing")


def решение(разбор: dict) -> bool:
    """Заведётся ли заявка. Складывается из двух полей, а не из одного.

    02.10.2026 я сравнивал голоса по сырому `is_request` и назвал разногласием
    случай, где обе головы на деле решили одинаково — не заводить: одна
    потому, что разговор про действующую заявку, другая потому, что
    ошиблась в ролях. Сравнивать надо итог.
    """
    return bool(разбор.get("is_request") and not разбор.get("about_existing"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Сравнить gpt-4o и YandexGPT на просеве.")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    s = get_settings()
    conn = connect(s.db_path)
    rows = conn.execute(
        """SELECT s.call_uid, s.head_text, s.verdict_json
           FROM screens s JOIN calls k ON k.uid = s.call_uid
           WHERE s.head_text <> '' AND s.verdict_json <> ''
             AND k.local_date >= date('now', ?)
           ORDER BY s.created_at DESC LIMIT ?""",
        (f"-{args.days} days", args.limit),
    ).fetchall()
    if not rows:
        print("сравнивать нечего")
        return 0

    print(f"сравниваю {len(rows)} разговоров: сохранённый ответ gpt-4o против YandexGPT\n")
    расхождения: Counter[str] = Counter()
    заявка_разошлась = []
    for i, r in enumerate(rows, 1):
        было = json.loads(r["verdict_json"] or "{}")
        стало = analyzer.screen_call(
            r["head_text"], "", api_key=s.yandex_api_key or "",
            model=s.yandex_model, own_company=s.own_company,
            engine="yandex", folder=s.yandex_folder or "", verify=True,
        )
        for поле in ПОЛЯ:
            if поле == "решение":
                a, b = решение(было), решение(стало)
            else:
                a, b = было.get(поле), стало.get(поле)
            if isinstance(a, str) or isinstance(b, str):
                a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
            if a != b:
                расхождения[поле] += 1
                if поле == "решение":
                    заявка_разошлась.append((r["call_uid"], a, b))
        print(f"  [{i}/{len(rows)}] {r['call_uid']}: "
              f"заявка {решение(было)} → {решение(стало)}, "
              f"техника «{было.get('equipment') or '—'}» → «{стало.get('equipment') or '—'}»")

    print("\nРАСХОЖДЕНИЯ ПО ПОЛЯМ:")
    for поле in ПОЛЯ:
        n = расхождения[поле]
        print(f"   {поле:<20} {n:>3} из {len(rows)}")
    if заявка_разошлась:
        print(f"\nглавное — «заводить ли заявку» разошлось у {len(заявка_разошлась)}:")
        for uid, a, b in заявка_разошлась[:10]:
            print(f"   {uid}: gpt-4o {a} → Яндекс {b}")
    print("\nСудить по этим цифрам нельзя: кто прав, знает только владелец. "
          "Разошедшиеся звонки — первые кандидаты на разметку.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
