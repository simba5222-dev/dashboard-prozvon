#!/usr/bin/env python
"""Сумма платежей сделки Synergy -> поле сделки «Сумма платежей» (custom-30778, число, руб.).

    sudo -u claude .venv/bin/python scripts/sync_deal_payments.py --deal 3643402            показать, что будет записано
    sudo -u claude .venv/bin/python scripts/sync_deal_payments.py --deal 3643402 --apply    записать

Зачем: в конструкторе формул Synergy нет метода «сумма платежей сделки» (проверено 17.11.2026 по всем формулам аккаунта),
а сами платежи лежат в связи сделки `invoice-payments`. Поэтому сумму считаем здесь и кладём в обычное числовое поле;
«Остаток» дальше можно сделать обычной формулой Synergy: «Сумма» − «Сумма платежей».

Что считается: платежи сделки с направлением `income` (приход) и статусом `executed` (проведён). Просроченные/ожидаемые
(`overdue`, `not_executed`) и расходы (`outcome`) не входят — это договорённость по первому тесту, меняется флагами.
Пишем только одно поле `customs.custom-30778`, остальное в сделке не трогаем. Пустую сумму не пишем (не затираем чужое).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import Settings  # noqa: E402

FIELD = "custom-30778"


def sync_all(cl, dirs, stats, apply) -> int:
    """Все платежи одним проходом (include=deal даёт сделку и её поля без отдельных запросов), сумма по каждой сделке.
    Пишем только если число в поле отличается. Сделка, у которой платежи есть, но ни один не проведён, получает 0
    (сумма стала меньше — например, платёж из проведённого сделали непроведённым)."""
    sums, olds, seen = {}, {}, {}
    page = 1
    while True:
        d = cl.get("invoice-payments", per_page=50, include="deal", **{"page[number]": page})
        for inc in d.get("included") or []:
            if inc["type"] == "deals":
                olds[inc["id"]] = (inc["attributes"].get("customs") or {}).get(FIELD)
        for r in d["data"]:
            x = (r["relationships"].get("deal") or {}).get("data")
            if not x:
                continue
            a = r["attributes"]
            seen[x["id"]] = seen.get(x["id"], 0) + 1
            if a.get("direction") in dirs and a.get("status") in stats:
                sums[x["id"]] = sums.get(x["id"], 0.0) + float(a.get("amount") or 0)
        if len(d["data"]) < 50:
            break
        page += 1
    changed = 0
    for deal in seen:
        total = sums.get(deal, 0.0)
        old = olds.get(deal)
        if old is not None and abs(float(old) - total) < 0.005:
            continue
        if old is None and total == 0:
            continue
        print(f"сделка {deal}: {old} -> {total:,.2f}" + ("" if apply else " (показ)"))
        if apply:
            cl.patch(f"deals/{deal}", {"data": {"type": "deals", "id": str(deal), "attributes": {"customs": {FIELD: total}}}})
        changed += 1
    print(f"сделок с платежами {len(seen)}, изменилось {changed}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deal", help="номер сделки (id в Synergy)")
    ap.add_argument("--all", action="store_true", help="пересчитать все сделки, у которых есть платежи (для таймера)")
    ap.add_argument("--apply", action="store_true", help="записать (без флага — только показать)")
    ap.add_argument("--directions", default="income", help="направления через запятую: income, outcome")
    ap.add_argument("--statuses", default="executed", help="статусы через запятую: executed, overdue, not_executed")
    args = ap.parse_args()

    s = Settings()
    cl = SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token, timeout_sec=30.0,
                       min_interval_sec=s.synergy_min_interval_sec, retries=s.synergy_retries)
    dirs, stats = set(args.directions.split(",")), set(args.statuses.split(","))
    if args.all:
        return sync_all(cl, dirs, stats, args.apply)
    if not args.deal:
        ap.error("нужен --deal или --all")

    deal = cl.get(f"deals/{args.deal}")["data"]
    attrs = deal["attributes"]
    pays = cl.get(f"deals/{args.deal}/invoice-payments").get("data") or []
    total = 0.0
    print(f"сделка {args.deal}: «{attrs.get('name')}» №{attrs.get('number')}; платежей {len(pays)}")
    for p in pays:
        a = p["attributes"]
        amount = float(a.get("amount") or 0)
        counted = a.get("direction") in dirs and a.get("status") in stats
        print(f"  платёж №{a.get('number')}: {amount:,.2f} ₽, {a.get('direction')}, {a.get('status')} -> {'считаем' if counted else 'не считаем'}")
        if counted:
            total += amount
    old = (attrs.get("customs") or {}).get(FIELD)
    print(f"сумма платежей: {total:,.2f} ₽; сейчас в поле {FIELD}: {old}")
    if not args.apply:
        print("это показ без записи; для записи добавьте --apply")
        return 0
    if total <= 0:
        print("сумма нулевая — не пишу (чужое пустотой не затираем)")
        return 0
    if old is not None and abs(float(old) - total) < 0.005:
        print("в поле уже стоит эта сумма — запись не нужна")
        return 0
    cl.patch(f"deals/{args.deal}", {"data": {"type": "deals", "id": str(args.deal), "attributes": {"customs": {FIELD: total}}}})
    after = cl.get(f"deals/{args.deal}")["data"]["attributes"]
    print(f"записано. Проверка чтением: {FIELD} = {(after.get('customs') or {}).get(FIELD)}; название «{after.get('name')}», сумма сделки {after.get('amount')} — без изменений")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
