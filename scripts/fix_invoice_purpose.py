#!/usr/bin/env python
"""Переписать «Назначение платежа» уже загруженных счетов как в платёжке; прежний служебный текст — в «Основание».

    sudo -u claude .venv/bin/python scripts/fix_invoice_purpose.py --file data/payments/invoices_2026-11.json [--only CКБП-002879] [--apply]
"""
import argparse, json, sys
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.config import Settings
from app.db import connect
import load_1c_invoices as L
import sync_requisites as sr

ap = argparse.ArgumentParser(); ap.add_argument("--file", required=True); ap.add_argument("--apply", action="store_true"); ap.add_argument("--only")
a = ap.parse_args()
cl = sr.client(); conn = connect(Settings().db_path)
ids = dict(conn.execute("SELECT doc_number, synergy_id FROM onec_invoices_loaded").fetchall())
users = L.users_map(cl); n = 0
for inv in json.loads(Path(a.file).read_text(encoding="utf-8")):
    sid = ids.get(inv["number"])
    if not sid or (a.only and inv["number"] != a.only):
        continue
    has_user = " ".join(inv["manager"].lower().split()[:2]) in users
    attrs = {"purpose": L.purpose_of(inv), "reason": L.reason_of(inv, has_user)}
    if not a.apply:
        print(inv["number"], "->", attrs["purpose"][:200]); n += 1
        if n >= 4: break
        continue
    try:
        cl.patch(f"invoices/{sid}", {"data": {"type": "invoices", "id": sid, "attributes": attrs}})
    except httpx.HTTPStatusError as e:
        if "reason" in e.response.text and "not allowed" in e.response.text:
            attrs.pop("reason"); cl.patch(f"invoices/{sid}", {"data": {"type": "invoices", "id": sid, "attributes": attrs}}); print(inv["number"], "(без «Основания»)")
        else:
            print("ОШИБКА", inv["number"], e.response.status_code, e.response.text[:200]); continue
    n += 1
    if a.only: break
print("обработано:", n)
