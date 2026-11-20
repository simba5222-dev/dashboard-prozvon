#!/usr/bin/env python
"""Проставить в загруженных из 1С платежах Synergy: кто платил (компания + её счёт) и куда пришли деньги (наша организация + наш счёт).

    sudo -u claude .venv/bin/python scripts/link_payment_requisites.py [--apply] [--only НОМЕР_1С]

Берёт `data/payments/requisites2.json` (выгрузка из 1С) и таблицу `onec_payments_loaded`. Правит только связи платежа
(receiver, receiver-bank-detail, payer, payer-bank-detail), остальное в платеже не трогает. Решение владельца 17.11.2026.
"""
import argparse, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.collector import SynergyClient
from app.config import Settings
from app.db import connect

D = Path(__file__).resolve().parents[1] / "data" / "payments"


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); ap.add_argument("--only")
    a = ap.parse_args()
    s = Settings()
    cl = SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token, timeout_sec=60.0, min_interval_sec=s.synergy_min_interval_sec, retries=s.synergy_retries)
    conn = connect(s.db_path)
    d = json.loads((D / f"requisites_{os.environ.get('PAY_TAG', '2026-11')}.json").read_text(encoding="utf-8"))
    conn.execute("CREATE TABLE IF NOT EXISTS onec_payments_linked (synergy_id TEXT PRIMARY KEY, linked_at TEXT NOT NULL)")
    done_ids = {r[0] for r in conn.execute("SELECT synergy_id FROM onec_payments_linked")}   # уже привязанные не трогаем: так запуск по таймеру дёшев
    loaded = {n: i for n, i in conn.execute("SELECT doc_number, synergy_id FROM onec_payments_loaded").fetchall() if i not in done_ids or a.only}
    mem = json.loads((D / "companies_created.json").read_text(encoding="utf-8"))
    orgs = {x["attributes"]["inn"]: x["id"] for x in cl.get("org-details", per_page=50)["data"]}
    ours = {}
    for page in range(1, 6):
        rows = cl.get("account-bank-details", per_page=50, **{"page[number]": page}).get("data") or []
        for x in rows: ours[x["attributes"]["number"]] = x["id"]
        if len(rows) < 50: break
    comp_cache, bank_cache = {}, {}
    def company(inn):
        if inn in mem: return mem[inn]
        if inn not in comp_cache:
            from app import company_index as ci
            import sqlite3
            rows = ci.companies_by_inn(cl, sqlite3.connect(s.db_path, timeout=60), inn)   # своя копия справочника: filter[inn] находит не всё
            comp_cache[inn] = max(rows, key=lambda x: (cl.get(f"companies/{x['id']}/deals", per_page=1)["meta"]["record-count"], -int(x["id"])))["id"] if rows else None
        return comp_cache[inn]
    def company_bank(cid, number):
        if cid not in bank_cache:
            bank_cache[cid] = {x["attributes"]["number"]: x["id"] for x in cl.get(f"companies/{cid}/bank-details", per_page=50)["data"]}
        return bank_cache[cid].get(number)
    done = 0
    for x in d["docs"]:
        num = x["number"]
        if num not in loaded or (a.only and num != a.only): continue
        rel = {}
        if x["org_inn"] in orgs: rel["receiver"] = {"data": {"type": "org-details", "id": orgs[x["org_inn"]]}}
        if x["our_acc"] and x["our_acc"]["number"] in ours: rel["receiver-bank-detail"] = {"data": {"type": "account-bank-details", "id": ours[x["our_acc"]["number"]]}}
        cid = company(x["cp"]["inn"]) if x["cp"]["inn"] not in orgs else None
        if cid:
            rel["payer"] = {"data": {"type": "companies", "id": cid}}
            bid = company_bank(cid, (x["cp_acc"] or {}).get("number"))
            if bid: rel["payer-bank-detail"] = {"data": {"type": "company-bank-details", "id": bid}}
        print(f"{num} (платёж {loaded[num]}): получатель {orgs.get(x['org_inn'])} счёт {ours.get(x['our_acc'].get('number'))} | плательщик {cid} счёт {rel.get('payer-bank-detail',{}).get('data',{}).get('id')} {x['cp']['name'][:22]}")
        if a.apply:
            cl.patch(f"invoice-payments/{loaded[num]}", {"data": {"type": "invoice-payments", "id": loaded[num], "relationships": rel}})
            r = cl.get(f"invoice-payments/{loaded[num]}", include="payer,payer-bank-detail,receiver,receiver-bank-detail")
            got = {k: (v or {}).get("data", {}) and v["data"]["id"] for k, v in r["data"]["relationships"].items() if k in rel}
            print("    после записи:", got)
            if "receiver" in rel and "payer" in rel:   # неполную привязку (например, плательщик ещё не найден) не запоминаем — повторится
                conn.execute("INSERT OR REPLACE INTO onec_payments_linked VALUES (?, datetime('now'))", (loaded[num],)); conn.commit()
            done += 1
            if a.only: break
    print("обработано:", done)

main()
