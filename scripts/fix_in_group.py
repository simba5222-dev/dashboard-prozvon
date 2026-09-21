#!/usr/bin/env python3
"""Пересчитать признак «звонок прозвона» (`in_group`) по всей истории.

Зачем. `in_group` ставится в момент сбора: звонок считается звонком прозвона,
если он исходящий и фамилия автора — из отдела прозвона. 18.09.2026 в таблицу
сотрудников добавили отдел продаж (ради поиска потерянных заявок во входящих),
а сборщик брал оттуда всех подряд — и с этого дня исходящие продажников тоже
помечались единицей. В отчёт по прозвону попали 545 чужих звонков.

Сам сборщик починен, но старые строки он не перепишет: сохранение звонка
идемпотентно и уже записанные не трогает. Этот скрипт приводит историю в
соответствие с текущим правилом. Его можно запускать сколько угодно раз:
он считает признак заново, а не правит «разницу».

    sudo -u claude .venv/bin/python scripts/fix_in_group.py          # сухой прогон
    sudo -u claude .venv/bin/python scripts/fix_in_group.py --apply  # записать

Без `--apply` ничего не пишется — только показывает, что изменится.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402

# Звонок прозвона — исходящий звонок человека из отдела прозвона. По `active`
# не фильтруем: признак описывает природу звонка, а не сегодняшнюю занятость.
# `{k}` — псевдоним таблицы звонков: в запросе с присоединением сотрудников
# голые `direction` и `vats_login` неоднозначны.
CORRECT = """
    CASE WHEN {k}direction = 'out'
          AND {k}vats_login IN (SELECT vats_login FROM managers WHERE dept = :dept)
         THEN 1 ELSE 0 END
"""
PLAIN = CORRECT.format(k="")
JOINED = CORRECT.format(k="k.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    args = parser.parse_args()

    settings = get_settings()
    conn: sqlite3.Connection = connect(settings.db_path)
    dept = {"dept": settings.group_dept}

    if not conn.execute(
        "SELECT 1 FROM managers WHERE dept = :dept LIMIT 1", dept
    ).fetchone():
        print(f"в таблице сотрудников нет никого с отделом «{settings.group_dept}» — "
              "сначала синхронизируйте группу, иначе скрипт обнулит весь прозвон")
        return 2

    wrong = conn.execute(
        f"SELECT in_group, COUNT(*) n FROM calls WHERE in_group != ({PLAIN}) "
        "GROUP BY in_group", dept,
    ).fetchall()
    to_clear = next((r["n"] for r in wrong if r["in_group"] == 1), 0)
    to_set = next((r["n"] for r in wrong if r["in_group"] == 0), 0)

    if not (to_clear or to_set):
        print("признак совпадает с правилом по всей истории, править нечего")
        return 0

    print(f"снять флаг у {to_clear} звонков (чужие в отчёте прозвона)")
    print(f"поставить флаг у {to_set} звонков")
    for row in conn.execute(
        f"""SELECT COALESCE(m.dept, '—') dept, k.vats_login, COUNT(*) n
            FROM calls k LEFT JOIN managers m ON m.vats_login = k.vats_login
            WHERE k.in_group != ({JOINED}) GROUP BY 1, 2 ORDER BY n DESC LIMIT 20""",
        dept,
    ):
        print(f"  {row['vats_login']:<16} отдел {row['dept']:<10} {row['n']}")

    if not args.apply:
        print("\nсухой прогон: ничего не записано. Повторите с --apply")
        return 0

    with conn:
        conn.execute(f"UPDATE calls SET in_group = ({PLAIN}) "
                     f"WHERE in_group != ({PLAIN})", dept)
    left = conn.execute(
        f"SELECT COUNT(*) n FROM calls WHERE in_group != ({PLAIN})", dept
    ).fetchone()["n"]
    total = conn.execute(
        "SELECT COUNT(*) n FROM calls WHERE in_group = 1"
    ).fetchone()["n"]
    print(f"\nзаписано. Осталось расхождений: {left}. Звонков прозвона в базе: {total}")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
