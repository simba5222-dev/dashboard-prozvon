#!/usr/bin/env python
"""Компании без ИНН: что у каждой есть в связях. Только чтение. Результат — data/dups/noinn_inventory.json"""
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
from app import collector as co
from app.config import get_settings
from company_merge_run import inventory

d = json.load(open(ROOT / "data/dups/companies_full.json"))
live = [c for c in d if not (c["attrs"].get("inn") or "").strip() and not c["attrs"].get("archived-at")]
s = get_settings(); cl = co.SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token)
out = {}
fp = ROOT / "data/dups/noinn_inventory.json"
for i, c in enumerate(live, 1):
    for t in range(4):
        try:
            inv = inventory(cl, c["id"]); break
        except Exception as e:  # noqa
            time.sleep(10 * (t + 1)); inv = None
    a = c["attrs"]
    out[c["id"]] = {"name": a.get("name"), "phone": a.get("general-phone"), "created": a.get("created-at"),
                    "links": {k: len(v) for k, v in inv.items()} if inv else None}
    if i % 10 == 0:
        json.dump(out, open(fp, "w"), ensure_ascii=False)
        print(i, len(live), flush=True)
json.dump(out, open(fp, "w"), ensure_ascii=False)
print("DONE", flush=True)
