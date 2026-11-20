#!/usr/bin/env python
"""Полная выгрузка компаний Synergy со всеми атрибутами (только чтение) → data/dups/companies_full.json. Основа для поиска дублей."""
from __future__ import annotations
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx  # noqa: E402
from app import collector as co  # noqa: E402
from app.config import get_settings  # noqa: E402

OUT = ROOT / "data" / "dups"
OUT.mkdir(exist_ok=True)
s = get_settings()
cl = co.SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token)
rows, page = [], 1
while True:
    for _ in range(20):
        try:
            p = cl.get("companies", **{"page[number]": page})
            break
        except httpx.HTTPError:
            time.sleep(15)
    else:
        raise RuntimeError(f"стр. {page}")
    d = p.get("data") or []
    if not d:
        break
    for c in d:
        rows.append({"id": c["id"], "attrs": c["attributes"], "rel": {k: bool((v or {}).get("data")) for k, v in c["relationships"].items()}})
    page += 1
(OUT / "companies_full.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
print("ГОТОВО", len(rows))
