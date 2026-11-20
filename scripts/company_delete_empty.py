#!/usr/bin/env python
"""Удаление пустых дублей компаний (без связей). Перед удалением: резерв карточки, повторная проверка связей. --apply для записи."""
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import httpx
from app import collector as co
from app.config import get_settings
from company_merge_run import inventory

ids = [a for a in sys.argv[1:] if a.isdigit()]
apply = "--apply" in sys.argv
s = get_settings(); cl = co.SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token)
bk = ROOT / "data/dups/backup"; bk.mkdir(exist_ok=True)
for cid in ids:
    card = cl.get(f"companies/{cid}")
    inv = inventory(cl, cid)
    n = sum(len(v) for v in inv.values())
    print(cid, card["data"]["attributes"].get("name"), "связей:", n, flush=True)
    if n:
        print("  ПРОПУСК: появились связи"); continue
    json.dump(card, open(bk / f"deleted_{cid}.json", "w"), ensure_ascii=False)
    if not apply: continue
    r = httpx.delete(f"{s.synergy_url.rstrip('/')}/companies/{cid}", timeout=30,
                     headers={"Authorization": f"Bearer {s.synergy_api_token}"})
    print("  DELETE ->", r.status_code, r.text[:200], flush=True)
    time.sleep(1)
    chk = httpx.get(f"{s.synergy_url.rstrip('/')}/companies/{cid}", timeout=30, headers={"Authorization": f"Bearer {s.synergy_api_token}"})
    print("  проверка чтением ->", chk.status_code, flush=True)
