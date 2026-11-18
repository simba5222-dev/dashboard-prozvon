#!/usr/bin/env python
"""Найти заявки, которые завёл менеджер, а разбор прозевал.

    sudo -u claude .venv/bin/python scripts/find_missed.py --days 7

Вторая половина обратной связи. Поле «Коммент для ИИ» ловит наши **лишние**
заявки — коллега их видит и пишет, почему это не заявка. А **пропущенные**
не видит никто: заявки от нас нет, комментировать нечего.

Зато их видно по признаку, который владелец завёл 02.10.2026:

    «Реклама»    — заявку создала автоматизация CRM по звонку на общий номер
    пусто        — менеджер завёл её сам
    «Создано ИИ» — завёл разбор звонка

Заявка с пустым признаком плюс входящий звонок рядом по времени, по которому
мы сказали «не заявка», — кандидат в пропуски.

**Именно кандидат, а не промах.** 02.10.2026 из пяти найденных настоящим
оказался один: по двум разговор был ни о чём, а один менеджер завёл по
ошибке — клиент просил продлить аренду, и заявка там не нужна. Так что
список требует человека, а не автоматического зачисления в ошибки.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402

ПРИЗНАК = "custom-26243"
ТЕЛЕФОН = "custom-276"
logger = logging.getLogger("пропуски")


def цифры(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))[-10:]


def main() -> int:
    ap = argparse.ArgumentParser(description="Заявки менеджера, которые разбор прозевал.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--pages", type=int, default=6, help="сколько страниц заявок листать")
    ap.add_argument("--window", type=int, default=7200,
                    help="сколько секунд между звонком и заявкой считать связью")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    s = get_settings()
    conn = connect(s.db_path)
    client = SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token,
                           timeout_sec=30.0, min_interval_sec=s.synergy_min_interval_sec,
                           retries=s.synergy_retries)
    граница = (datetime.now() - __import__("datetime").timedelta(days=args.days)).date().isoformat()

    заявки, стоп = [], False
    for стр in range(1, args.pages + 1):
        rows = (client.get("orders", sort="-created-at",
                           **{"page[number]": стр}).get("data") or [])
        if not rows:
            break
        for r in rows:
            a = r.get("attributes") or {}
            когда = str(a.get("created-at") or "")
            if когда[:10] < граница:
                стоп = True
                continue
            cu = a.get("customs") or {}
            признак = (cu.get(ПРИЗНАК) or [""])[0] if cu.get(ПРИЗНАК) else ""
            if признак:                       # «Реклама» или «Создано ИИ» — не наш случай
                continue
            тел = цифры(cu.get(ТЕЛЕФОН))
            if тел:
                заявки.append((r["id"], a.get("number"), когда, тел))
        if стоп:
            break

    print(f"заявок менеджера (признак пуст) за {args.days} дней: {len(заявки)}\n")
    кандидаты = []
    for oid, номер, когда, тел in заявки:
        row = conn.execute(
            """SELECT k.uid, k.started_at, k.duration_sec, k.vats_login,
                      s.is_request, s.head_text
               FROM calls k JOIN screens s ON s.call_uid = k.uid
               WHERE k.direction = 'in'
                 AND REPLACE(REPLACE(REPLACE(k.client_phone,'+',''),' ',''),'-','') LIKE ?
                 AND ABS(strftime('%s', k.started_at) - strftime('%s', ?)) < ?
               ORDER BY ABS(strftime('%s', k.started_at) - strftime('%s', ?)) LIMIT 1""",
            (f"%{тел}", когда[:19], args.window, когда[:19]),
        ).fetchone()
        if row and not row["is_request"]:
            кандидаты.append((oid, номер, row))

    if not кандидаты:
        print("кандидатов в пропуски нет")
        return 0
    print(f"КАНДИДАТЫ В ПРОПУСКИ: {len(кандидаты)}")
    print("(кандидат, а не промах: менеджер тоже ошибается — проверяйте по тексту)\n")
    for oid, номер, row in кандидаты:
        print(f"— заявка {oid} №{номер} ← звонок {row['uid']}, "
              f"{row['started_at'][:16].replace('T', ' ')}, {row['duration_sec']} с, "
              f"{row['vats_login']}")
        текст = (row["head_text"] or "").strip()
        for строка in текст.split("\n")[:4]:
            if строка.strip():
                print(f"      {строка.strip()[:88]}")
        if not текст:
            print("      расшифровки нет")
        print()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
