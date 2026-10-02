#!/usr/bin/env python
"""Счёт по размеченным владельцем звонкам: кто сколько угадал.

    sudo -u claude .venv/bin/python scripts/score_trial.py             счёт как есть
    sudo -u claude .venv/bin/python scripts/score_trial.py --rerun     прогнать заново

Истина берётся из пометки владельца на `/trial`. Пометка «справа верно»
означает, что прав пробный прогон; «справа неверно» — что прав был боевой.
Пояснение владельца — главное, но машинно читается именно отметка.

Без этого счёта любая правка промпта — гадание: ответы изменятся, а лучше
стало или хуже, сказать нечем.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, save_trial  # noqa: E402


def решение(разбор: dict) -> bool:
    return bool(разбор.get("is_request") and not разбор.get("about_existing"))


ЭТАЛОН = Path(__file__).resolve().parents[1] / "checkset" / "truth-screen.json"


def эталон() -> dict[str, dict]:
    """Истина по каждому звонку — снята со слов владельца.

    Кнопку он ставил «спорно», а ответ писал пояснением: «Это заявка!»,
    «не заявка, звонит исполнитель». Машинно читается только файл, поэтому
    истина собрана из его слов один раз и лежит в `checkset/truth-screen.json`.
    Передумал — правим файл, а не код.
    """
    if not ЭТАЛОН.exists():
        return {}
    import json as _json

    d = _json.loads(ЭТАЛОН.read_text(encoding="utf-8"))
    return {x["uid"]: x for x in d.get("звонки", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description="Счёт по размеченным звонкам.")
    ap.add_argument("--rerun", action="store_true", help="прогнать размеченные заново")
    ap.add_argument("--engine", default="", help="какой головой прогонять")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    s = get_settings()
    conn = connect(s.db_path)
    rows = conn.execute(
        """SELECT t.call_uid, t.head_text, t.verdict, t.verdict_note,
                  t.verdict_json AS trial, s.verdict_json AS live, s.is_request
           FROM screen_trials t LEFT JOIN screens s ON s.call_uid = t.call_uid
           WHERE COALESCE(t.verdict,'') <> '' ORDER BY t.verdict_at"""
    ).fetchall()
    if not rows:
        print("размеченных звонков нет")
        return 0

    истины = эталон()
    голова = args.engine or s.screen_engine
    яндексом = голова == "yandex"
    совпало = разошлось = без_истины = 0
    промахи = []

    for r in rows:
        боевой = bool(r["is_request"])
        if args.rerun:
            разбор = analyzer.screen_call(
                r["head_text"], "",
                api_key=(s.yandex_api_key if яндексом else s.openai_api_key) or "",
                model=s.yandex_model if яндексом else s.analysis_model,
                engine=голова, folder=s.yandex_folder or "", own_company=s.own_company)
            save_trial(conn, call_uid=r["call_uid"], engine=голова,
                       head_text=r["head_text"],
                       verdict_json=json.dumps(разбор, ensure_ascii=False),
                       made_at=datetime.now(timezone.utc).isoformat())
            conn.commit()
        else:
            разбор = json.loads(r["trial"] or "{}")

        пробный = решение(разбор)
        запись = истины.get(r["call_uid"])
        if запись is None:
            без_истины += 1
            continue
        истина = bool(запись["заявка"])
        if пробный == истина:
            совпало += 1
        else:
            разошлось += 1
            промахи.append((r["call_uid"], "ПРОМАХ", пробный,
                            str(запись.get("слова_владельца") or "")[:62]))

    судимо = совпало + разошлось
    print(f"голова «{голова}», размеченных {len(rows)}\n")
    print(f"   совпало с владельцем:  {совпало} из {судимо}")
    print(f"   промахов:              {разошлось}")
    print(f"   нет в эталоне:         {без_истины}")
    if промахи:
        print("\n   разобрать вручную:")
        for uid, вид, ответ, прим in промахи:
            print(f"     {uid}  модель: {'заявка' if ответ else 'не заявка'}, "
                  f"а надо {'заявка' if not ответ else 'не заявка'}")
            if прим:
                print(f"              «{прим}»")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
