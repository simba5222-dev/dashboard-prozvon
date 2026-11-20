#!/usr/bin/env python
"""Баланс расчётов с покупателями из 1С → поля компании в Synergy «Баланс по СК» (custom-30791) и «Баланс по АЛ» (custom-30792).

    sudo -u claude .venv/bin/python scripts/sync_balances.py [--apply] [--file data/call_plan/balances_1c.json]

Без --apply — сухой прогон: ничего не пишет, готовит data/call_plan/balances_plan.json.
Вход — JSON от onec_balances.py (читает 1С на офисе, только чтение). Компании ищем по ИНН (`filter[inn]`): если в CRM несколько
карточек с одним ИНН (дубли), пишем во все. Знак: плюс — контрагент должен нам; минус — мы должны ему (аванс/переплата).
Наши собственные организации (ИНН из справочника 1С) не пишем — это внутренние расчёты.
Если у компании раньше записали баланс, а в 1С он теперь нулевой (или пропал) — записываем 0, чтобы не висел долг, которого нет.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import collector as co  # noqa: E402
from app.config import get_settings  # noqa: E402

FIELD = {"СК": "custom-30791", "АЛ": "custom-30792"}
TABLE = """CREATE TABLE IF NOT EXISTS company_balances(company_id TEXT, org TEXT, amount REAL, inn TEXT, written_at TEXT, PRIMARY KEY(company_id, org))"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="записать не больше N карточек (пробная запись)")
    ap.add_argument("--file", default=str(ROOT / "data" / "call_plan" / "balances_1c.json"))
    args = ap.parse_args()
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    own = set(data.get("own_inns") or [])
    s = get_settings()
    cl = co.SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token)
    conn = sqlite3.connect(str(ROOT / "data" / "dashboard.db"), timeout=60)
    conn.execute(TABLE)
    prev = {(r[0], r[1]): r[2] for r in conn.execute("SELECT company_id, org, amount FROM company_balances")}
    plan, missing, intercompany = [], [], []
    advances = 0
    wanted: set[tuple[str, str]] = set()
    cache: dict[str, list[dict]] = {}

    # ВСЕ компании CRM одним проходом: фильтр Synergy `filter[inn]` ненадёжен (не находит часть компаний, особенно недавно заведённых)
    index: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    page = 1
    while True:
        rows = None
        for _ in range(20):
            try:
                rows = cl.get("companies", **{"page[number]": page}).get("data") or []
                break
            except Exception:  # noqa: BLE001 — лимит Synergy общий: ждём
                import time
                time.sleep(15)
        if rows is None:
            raise RuntimeError(f"страница компаний {page} не отдалась")
        if not rows:
            break
        for c in rows:
            a = c["attributes"]
            k = re.sub(r"\D", "", str(a.get("inn") or ""))
            if k:
                index.setdefault(k, []).append(c)
            else:
                by_name.setdefault(re.sub(r"[^а-яa-z0-9]", "", re.sub(r"\b(ооо|ао|зао|пао|ип)\b", "", str(a.get("name") or "").lower())), []).append(c)
        page += 1
    print(f"компаний в CRM выгружено: {sum(len(v) for v in index.values()) + sum(len(v) for v in by_name.values())}, из них без ИНН: {sum(len(v) for v in by_name.values())}")

    def crm(inn: str) -> list[dict]:
        return index.get(inn, [])

    for org, by_inn in data["orgs"].items():
        field = FIELD[org]
        for inn, x in by_inn.items():
            if inn in own:
                intercompany.append({"org": org, "name": x["name"], "inn": inn, "balance": x["balance"]})
                continue
            found = [c for c in crm(inn) if re.sub(r"\D", "", str((c["attributes"] or {}).get("inn") or "")) == inn]
            if not found:
                nm = re.sub(r"[^а-яa-z0-9]", "", re.sub(r"\b(ооо|ао|зао|пао|ип)\b", "", str(x["name"] or "").lower()))
                cand = [{"id": c["id"], "name": c["attributes"].get("name")} for c in by_name.get(nm, [])]
                if abs(x["balance"]) >= 0.5:
                    missing.append({"org": org, "name": x["name"], "inn": inn, "balance": x["balance"], "crm_by_name_no_inn": cand})
                continue
            for c in found:
                cur = (c["attributes"].get("customs") or {}).get(field)
                plan.append({"company_id": c["id"], "crm_name": c["attributes"].get("name"), "inn": inn, "org": org, "field": field,
                             "balance": x["balance"], "crm_now": cur, "dup": len(found) > 1})
                wanted.add((c["id"], org))
    # раньше писали, а в 1С теперь нет — обнуляем
    for (cid, org), amt in prev.items():
        if (cid, org) not in wanted and abs(amt or 0) >= 0.005:
            plan.append({"company_id": cid, "crm_name": "(обнуление)", "inn": "", "org": org, "field": FIELD[org], "balance": 0.0,
                         "crm_now": amt, "dup": False})
    todo = [p for p in plan if abs(float(p["crm_now"] or 0) - p["balance"]) >= 0.005]
    for p in plan:
        if abs(float(p["crm_now"] or 0) - p["balance"]) < 0.005 and (p["company_id"], p["org"]) not in prev and args.apply:
            conn.execute("INSERT OR REPLACE INTO company_balances VALUES (?,?,?,?,?)",
                         (p["company_id"], p["org"], p["balance"], p["inn"], datetime.now(timezone.utc).isoformat()))
    conn.commit()
    if args.limit:
        todo = todo[: args.limit]
    print(f"из них с минусом (мы должны контрагенту): {sum(1 for p in plan if p['balance'] < -0.005)}")
    print(f"записей к записи: {len(plan)}, из них изменится: {len(todo)}; нет в CRM: {len(missing)}; наши организации (пропущено): {len(intercompany)}")
    for org in FIELD:
        pos = [p for p in plan if p["org"] == org and p["balance"] > 0.005]
        neg = [p for p in plan if p["org"] == org and p["balance"] < -0.005]
        print(f"  {org}: нам должны {len(pos)} компаний на {sum(p['balance'] for p in pos):,.0f} ₽; мы должны {len(neg)} компаниям на {-sum(p['balance'] for p in neg):,.0f} ₽".replace(",", " "))
    (ROOT / "data" / "call_plan" / "balances_plan.json").write_text(
        json.dumps({"plan": plan, "missing": missing, "intercompany": intercompany}, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.apply:
        ok = fail = 0
        for p in todo:
            try:
                cl.patch(f"companies/{p['company_id']}", {"data": {"type": "companies", "id": str(p["company_id"]),
                                                                     "attributes": {"customs": {p["field"]: p["balance"]}}}})
                back = (cl.get(f"companies/{p['company_id']}")["data"]["attributes"].get("customs") or {}).get(p["field"])
                if abs(float(back or 0) - p["balance"]) < 0.005:
                    ok += 1
                    conn.execute("INSERT OR REPLACE INTO company_balances VALUES (?,?,?,?,?)",
                                 (p["company_id"], p["org"], p["balance"], p["inn"], datetime.now(timezone.utc).isoformat()))
                    conn.commit()
                else:
                    fail += 1
                    print("  не подтверждено:", p["company_id"], p["crm_name"], back)
            except Exception as exc:  # noqa: BLE001
                fail += 1
                print("  ошибка:", p["company_id"], str(exc)[:80])
        print(f"записано и проверено {ok}, ошибок {fail}")
        return 1 if fail else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
