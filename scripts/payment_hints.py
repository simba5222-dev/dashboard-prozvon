#!/usr/bin/env python
"""Подсказки «к какой сделке разнести платёж» в описании платежа Synergy (вариант 1, решение владельца 18.11.2026).

    sudo -u claude .venv/bin/python scripts/payment_hints.py --file data/payments/incoming_2026-11.json            показать, что будет дописано
    sudo -u claude .venv/bin/python scripts/payment_hints.py --file ... --apply                                      дописать в уже загруженные платежи

Автоматически ничего не привязываем: надёжного ключа между платежом 1С и сделкой Synergy нет (проверено 17.11.2026 на
179 оплатах: однозначно по сумме находится 12%). Поэтому в описание платежа дописывается подсказка: что написано в счёте
и реализации 1С (комментарий обычно содержит менеджера и даты) и какие сделки клиента (по «ИНН З») похожи: номер документа 1С
в названии сделки, совпала сумма, либо недавние сделки этого клиента. Платёж сотрудник разносит сам.
Функция make_hint используется и загрузчиком платежей — новые платежи получают подсказку при создании.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MARK = "ПОДСКАЗКА ПО СДЕЛКЕ"
WINDOW_DAYS = 120   # сделки клиента, созданные не раньше чем за столько дней до платежа
RECENT_DAYS = 45    # если сумма не совпала — показываем сделки клиента за этот срок
MAX_SHOWN = 5


def _num(v):
    return float(v) if v not in (None, "") else None


def deals_for_inn(cl, inn: str, cache: dict) -> list[dict]:
    """Сделки клиента по полю «ИНН З» (custom-16356), новые первыми. Поле — единственное, по которому Synergy фильтрует надёжно."""
    if inn not in cache:
        out = []
        for page in (1, 2):
            rows = cl.get("deals", per_page=100, sort="-created-at", **{"filter[custom-16356]": inn, "page[number]": page}).get("data") or []
            for x in rows:
                a = x["attributes"]
                cu = a.get("customs") or {}
                if inn in str(cu.get("custom-16356") or ""):   # фильтр иногда отдаёт лишнее
                    out.append({"no": a.get("number"), "name": (a.get("name") or "").strip(), "amount": _num(a.get("amount")),
                                "sz": _num(cu.get("custom-11983")), "created": a["created-at"][:10]})
            if len(rows) < 100:
                break
        cache[inn] = out
    return cache[inn]


def _fd(iso: str) -> str:
    return datetime.fromisoformat(iso[:10]).strftime("%d.%m.%Y") if iso else ""


def _money(v) -> str:
    return f"{float(v):,.0f}".replace(",", " ")


def _deal(d: dict) -> str:
    s = f"№{d['no']} от {_fd(d['created'])}"
    if d["name"]:
        s += f" «{d['name'][:28]}»"
    sums = [v for v in (d["sz"], d["amount"]) if v]
    return s + (f", сумма {_money(sums[0])} ₽" if sums else "")


def make_hint(item: dict, deals: list[dict]) -> str:
    pay_day = date.fromisoformat(item["date"][:10])
    lines = item.get("lines") or []
    parts = []
    # 1. что написано в 1С — по нему сотрудник часто узнаёт сделку сам
    for l in lines[:3]:
        if l.get("inv"):
            t = f"счёт 1С {l['inv']} от {_fd(l['inv_date'])} на {_money(l['inv_sum'] or 0)} ₽"
            if l.get("inv_comment"):
                t += f", комментарий «{l['inv_comment'][:90]}»"
            parts.append(t)
        if l.get("rel"):
            t = f"реализация {l['rel']} от {_fd(l['rel_date'])} на {_money(l['rel_sum'] or 0)} ₽"
            if l.get("rel_comment"):
                t += f", комментарий «{l['rel_comment'][:90]}»"
            parts.append(t)
    out = [MARK + " (автоматическая, проверьте):"]
    if parts:
        out.append("В 1С: " + "; ".join(parts) + ".")
    # 2. сделки клиента
    if not item.get("inn"):
        out.append("ИНН плательщика в 1С не указан, сделки не искали.")
        return " ".join(out)
    if not deals:
        out.append(f"В Synergy нет сделок с «ИНН З» {item['inn']}: проверьте, под какой компанией заведена заявка.")
        return " ".join(out)
    keys = {l[k] for l in lines for k in ("inv", "rel") if l.get(k)}
    sums = {float(item["amount"])} | {float(l[k]) for l in lines for k in ("sum", "inv_sum", "rel_sum") if l.get(k)}
    window = [d for d in deals if pay_day - timedelta(days=WINDOW_DAYS) <= date.fromisoformat(d["created"]) <= pay_day + timedelta(days=7)]
    by_key = [d for d in deals if any(k in d["name"] for k in keys)]
    by_sum = [d for d in window if d["amount"] in sums or d["sz"] in sums]
    if by_key:
        out.append("Номер документа 1С есть в названии сделки: " + "; ".join(_deal(d) for d in by_key[:MAX_SHOWN]) + ".")
    if by_sum and not by_key:
        word = "Сумма совпала со сделкой" if len(by_sum) == 1 else f"Сумма совпала с {len(by_sum)} сделками"
        out.append(f"{word}: " + "; ".join(_deal(d) for d in by_sum[:MAX_SHOWN]) + (" и др" if len(by_sum) > MAX_SHOWN else "") + ".")
    if not by_key and not by_sum:
        recent = [d for d in deals if date.fromisoformat(d["created"]) >= pay_day - timedelta(days=RECENT_DAYS)]
        if recent:
            out.append("Сумма ни с одной сделкой не совпала (счёт может покрывать несколько заявок). Сделки клиента за 45 дней: "
                       + "; ".join(_deal(d) for d in recent[:MAX_SHOWN]) + (" и др" if len(recent) > MAX_SHOWN else "") + ".")
        else:
            out.append(f"Сумма не совпала; сделок клиента за последние {RECENT_DAYS} дней нет (всего у клиента {len(deals)}, последняя от {_fd(deals[0]['created'])}).")
    return " ".join(out)


def main() -> int:
    from app.collector import SynergyClient
    from app.config import Settings
    from app.db import connect

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", help="только документ 1С с этим номером (проба)")
    args = ap.parse_args()
    s = Settings()
    cl = SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token, timeout_sec=60.0, min_interval_sec=s.synergy_min_interval_sec, retries=s.synergy_retries)
    conn = connect(s.db_path)
    items = {i["number"]: i for i in json.loads(Path(args.file).read_text(encoding="utf-8"))}
    cache: dict = {}
    done = skipped = 0
    for num, pid in conn.execute("SELECT doc_number, synergy_id FROM onec_payments_loaded ORDER BY synergy_id").fetchall():
        if num not in items or (args.only and num != args.only):
            continue
        r = cl.get(f"invoice-payments/{pid}", include="deal")
        desc = str(r["data"]["attributes"].get("description") or "")
        if MARK in desc or (r["data"]["relationships"].get("deal") or {}).get("data"):
            skipped += 1   # подсказка уже есть, либо сотрудник уже разнёс платёж по сделке
            continue
        hint = make_hint(items[num], deals_for_inn(cl, items[num]["inn"], cache) if items[num].get("inn") else [])
        print(f"{num} (платёж {pid}): {hint[:260]}")
        if args.apply:
            cl.patch(f"invoice-payments/{pid}", {"data": {"type": "invoice-payments", "id": pid, "attributes": {"description": (desc + " " + hint).strip()}}})
        done += 1
    print(f"итого: {'дописано' if args.apply else 'будет дописано'} {done}, пропущено {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
