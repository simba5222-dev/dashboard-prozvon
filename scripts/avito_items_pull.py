#!/usr/bin/env python
"""Выгрузка объявлений Авито, статистики и расходов по месяцам (только чтение) в data/avito/*.json.

    sudo -u claude .venv/bin/python scripts/avito_items_pull.py --since 2026-01-01

Файлы читает сборщик отчёта `scripts/avito_report_build.py`:
  items.json            объявления профиля (название, цена, статус, ссылка, категория)
  stats_by_month.json   по каждому месяцу и объявлению: показы, просмотры, контакты, избранное, расходы (руб.)
  spend_month.json      расходы профиля по месяцам и типам (размещение, продвижение, комиссия, прочее)
Авито жёстко ограничивает частоту запросов (метод расходов — не чаще раза в минуту); клиент ждёт сам, выгрузка занимает минуты.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.avito import AvitoClient, AvitoError  # noqa: E402
from app.config import Settings  # noqa: E402

METRICS = ["impressions", "views", "contacts", "contactsShowPhone", "contactsMessenger", "favorites", "allSpending",
           "spending", "presenceSpending", "promoSpending"]
STATUSES = ["active", "old", "removed", "blocked", "rejected"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-01-01")
    args = ap.parse_args()
    s = Settings()
    cl = AvitoClient(s.avito_client_id or "", s.avito_client_secret or "", s.avito_user_id)
    out = Path(s.db_path).parent / "avito"
    out.mkdir(parents=True, exist_ok=True)

    # 1. объявления всех статусов
    items: dict[int, dict] = {}
    for st in STATUSES:
        page = 1
        while True:
            try:
                d = cl.items(status=st, per_page=100, page=page)
            except AvitoError as exc:
                print(f"объявления {st}: {exc}", flush=True)
                break
            res = d.get("resources") or []
            for r in res:
                items[int(r["id"])] = r
            if len(res) < 100:
                break
            page += 1
        print(f"объявления, статус {st}: всего накоплено {len(items)}", flush=True)
    (out / "items.json").write_text(json.dumps(list(items.values()), ensure_ascii=False), encoding="utf-8")

    # 2. статистика по объявлениям, по месяцам
    start = date.fromisoformat(args.since)
    today = date.today()
    months, y, m = [], start.year, start.month
    while (y, m) <= (today.year, today.month):
        last = calendar.monthrange(y, m)[1]
        months.append((f"{y}-{m:02d}", f"{y}-{m:02d}-01", min(date(y, m, last), today).isoformat()))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    stats: dict[str, dict[str, dict]] = {}
    for label, d1, d2 in months:
        try:
            r = cl.stats_items(d1, d2, METRICS, grouping="item", limit=1000)
        except AvitoError as exc:
            print(f"статистика {label}: {exc}", flush=True)
            continue
        per: dict[str, dict] = {}
        for g in (r.get("result") or {}).get("groupings") or []:
            per[str(g["id"])] = {mm["slug"]: mm["value"] for mm in g.get("metrics") or []}
        stats[label] = per
        print(f"статистика {label}: объявлений {len(per)}", flush=True)
    (out / "stats_by_month.json").write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")

    # 3. расходы профиля по месяцам (метод не чаще раза в минуту; глубина 270 дней)
    try:
        sp = cl.spendings(args.since, today.isoformat(), "month")
        (out / "spend_month.json").write_text(json.dumps(sp, ensure_ascii=False), encoding="utf-8")
        print("расходы профиля по месяцам: получено", flush=True)
    except AvitoError as exc:
        print(f"расходы профиля: {exc}", flush=True)
    print("ГОТОВО", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
