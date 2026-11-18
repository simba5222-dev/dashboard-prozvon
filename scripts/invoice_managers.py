#!/usr/bin/env python
"""ФИО менеджера, выставляющего счёт, — из комментария счёта в 1С (решение владельца 18.11.2026).

    sudo -u claude .venv/bin/python scripts/invoice_managers.py --file data/payments/invoices_2026-11.json            показать охват
    sudo -u claude .venv/bin/python scripts/invoice_managers.py --file ... --apply                                    записать в таблицу `inv_manager`

Почему не «ответственный» из 1С: он показывает, кто ввёл счёт (часто бухгалтер), а менеджер сделки пишет свою фамилию в комментарии
(«Коробейников/ 03.11.26г», «Колычев /02.11.26», «Силивиров / 07-08.11.2026»). Фамилию из комментария сопоставляем со списком
сотрудников 1С (`data/payments/roster_1c.json`, выгрузка `invoices/onec_users.py`) и берём ФИО целиком. Неоднозначные фамилии
разрешаются в пользу ответственного счёта, если он один из кандидатов. Не нашли — менеджер не определён (пустое значение),
угадывать не будем. Страница счетов показывает колонку «Менеджер» из таблицы `inv_manager`.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROSTER = Path(__file__).resolve().parents[1] / "data" / "payments" / "roster_1c.json"
NOT_PEOPLE = re.compile(r"[A-Za-z0-9<>@:+_]|бухгалтер|администратор|ассистент|сервис|автомат|служебн|менеджер|сверк|test|бух\b", re.I)
# слова из комментариев, которые совпадают с фамилиями или похожи на них, но менеджерами не являются
STOP = {"кран", "самосвал", "манипулятор", "экскаватор", "минипогрузчик", "переработка", "доплата", "аванс", "оплата", "договор", "услуги", "работы"}


def load_roster(path: Path = ROSTER) -> list[str]:
    names = json.loads(path.read_text(encoding="utf-8"))
    return [n for n in names if not NOT_PEOPLE.search(n) and 2 <= len(n.split()) <= 4]


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[А-Яа-яЁё]+", text or "") if len(w) >= 4 and w.lower() not in STOP]


def manager_of(comment: str, roster: list[str], hint: str = "") -> tuple[str, str]:
    """(ФИО, как нашли): 'точно' — слово комментария равно фамилии сотрудника; 'похоже' — почти равно (Клычев ~ Колычев); пусто — не нашли.
    Сравниваем только с фамилией (первое слово ФИО): имена в комментариях («Иван Кудрин») сотрудников не определяют."""
    exact: dict[str, str] = {}
    fuzzy: dict[str, str] = {}
    for w in _words(comment):
        wl = w.lower()
        for fio in roster:
            sur = fio.split()[0].lower()
            if len(sur) < 4:
                continue
            if wl == sur or (len(wl) >= 5 and len(sur) >= 5 and wl[:-1] == sur[:-1] and wl[-1] in "аяыиеу" and sur[-1] in "аяыиеу"):   # Леонова / Леоновой
                exact.setdefault(fio, w)
            elif len(wl) >= 5 and len(sur) >= 5 and difflib.SequenceMatcher(None, wl, sur).ratio() >= 0.88:
                fuzzy.setdefault(fio, w)
    for found, how in ((exact, "точно"), (fuzzy, "похоже")):
        if found:
            cands = sorted(found, key=lambda f: -len(f.split()))   # полное ФИО с отчеством — раньше короткой записи
            if hint:   # однофамильцы — ответственный счёта как подсказка
                h = [c for c in cands if c == hint or c.split()[:2] == hint.split()[:2]]
                if h:
                    return sorted(h, key=lambda f: -len(f.split()))[0], how
            return cands[0], how
    return "", ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    roster = load_roster()
    items = json.loads(Path(a.file).read_text(encoding="utf-8"))
    res = {i["number"]: manager_of(i.get("comment") or "", roster, i.get("manager") or "") for i in items}
    withc = [i for i in items if i.get("comment")]
    found = [i for i in items if res[i["number"]][0]]
    print(f"счетов {len(items)}, с комментарием {len(withc)}, менеджер определён у {len(found)} (точно {sum(1 for i in found if res[i['number']][1] == 'точно')}, похоже {sum(1 for i in found if res[i['number']][1] == 'похоже')})")
    print("не определён, хотя комментарий есть:", [(i["number"], i["comment"][:40]) for i in withc if not res[i["number"]][0]][:12])
    for i in found[:14]:
        print(f"  {i['number']} «{i['comment'][:38]}» -> {res[i['number']][0]} ({res[i['number']][1]}) | ответственный 1С: {i['manager']}")
    if not a.apply:
        return 0
    from app.config import Settings
    from app.db import connect
    conn = connect(Settings().db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS inv_manager (synergy_id TEXT PRIMARY KEY, doc_number TEXT, fio TEXT NOT NULL, how TEXT, comment TEXT)")
    ids = dict(conn.execute("SELECT doc_number, synergy_id FROM onec_invoices_loaded").fetchall())
    n = 0
    for i in items:
        sid, (fio, how) = ids.get(i["number"]), res[i["number"]]
        if not sid:
            continue
        conn.execute("DELETE FROM inv_manager WHERE synergy_id=?", (sid,))
        if fio:
            conn.execute("INSERT INTO inv_manager VALUES (?,?,?,?,?)", (sid, i["number"], fio, how, i.get("comment") or ""))
            n += 1
    conn.commit()
    print(f"записано в inv_manager: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
