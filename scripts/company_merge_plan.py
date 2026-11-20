#!/usr/bin/env python
"""Сухой прогон слияния дублей компаний Synergy: что с чем сольётся. В CRM НИЧЕГО не пишет.

    .venv/bin/python -I scripts/company_merge_plan.py        читает data/dups/companies_full.json (company_dups_dump.py) и сделки call_plan
Результат: data/dups/merge_plan.json и сводка в stdout. Правила (предложены владельцу 19.11.2026, ждут утверждения):
- дубль = одинаковый ИНН; группы, где КПП различаются, НЕ трогаем (филиалы);
- основная карточка: больше сделок → больше контактов → более старая;
- пустые поля основной дополняются из дублей; расхождения не затираем;
- дубли архивируются, не удаляются; список переносимых связей сохраняется для отката.
"""
from __future__ import annotations
import collections, glob, json, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "data"
dg = lambda v: re.sub(r"\D", "", str(v or ""))
rows = json.loads((D / "dups" / "companies_full.json").read_text(encoding="utf-8"))
cc = json.loads((D / "call_plan" / "company_contacts.json").read_text(encoding="utf-8"))["companies"]
deals, seen = collections.Counter(), set()
for f in glob.glob(str(D / "call_plan" / "deals*.jsonl")):
    for l in open(f, encoding="utf-8"):
        d = json.loads(l)
        if d["id"] not in seen:
            seen.add(d["id"])
            if d.get("company_id"):
                deals[d["company_id"]] += 1
g = collections.defaultdict(list)
for r in rows:
    if dg(r["attrs"].get("inn")):
        g[dg(r["attrs"]["inn"])].append(r)
FIELDS = ("general-phone", "work-phone", "mobile-phone", "other-phone", "email", "other-email", "website", "full-name", "short-name", "ogrn", "kpp",
          "okpo", "okved", "address", "city", "region", "juristic-city", "juristic-street", "juristic-house", "actual-city", "actual-street", "actual-house",
          "manager-name", "manager-position", "description", "note")
plan, skipped_branches, conflicts = [], [], 0
for inn, v in g.items():
    if len(v) < 2:
        continue
    kpps = {dg(r["attrs"].get("kpp")) for r in v if dg(r["attrs"].get("kpp"))}
    if len(kpps) > 1:
        skipped_branches.append({"inn": inn, "cards": [(r["id"], r["attrs"].get("name"), dg(r["attrs"].get("kpp"))) for r in v]})
        continue
    v = sorted(v, key=lambda r: (-deals[r["id"]], -len(cc.get(r["id"], {}).get("contacts", [])), r["attrs"]["created-at"]))
    master, dups = v[0], v[1:]
    fill, diff = {}, []
    for dup in dups:
        for f in FIELDS:
            mv, dv = master["attrs"].get(f), dup["attrs"].get(f)
            if dv and not mv and f not in fill:
                fill[f] = dv
            elif dv and mv and str(dv).strip() != str(mv).strip():
                diff.append({"field": f, "master": mv, "dup": dv, "dup_id": dup["id"]})
    conflicts += len(diff)
    plan.append({"inn": inn, "master": {"id": master["id"], "name": master["attrs"].get("name"), "deals": deals[master["id"]],
                                        "contacts": len(cc.get(master["id"], {}).get("contacts", [])), "created": master["attrs"]["created-at"][:10]},
                 "duplicates": [{"id": d["id"], "name": d["attrs"].get("name"), "deals": deals[d["id"]], "contacts": len(cc.get(d["id"], {}).get("contacts", [])),
                                 "created": d["attrs"]["created-at"][:10], "archived": bool(d["attrs"].get("archived-at"))} for d in dups],
                 "fill_into_master": fill, "conflicts": diff[:12]})
(D / "dups" / "merge_plan.json").write_text(json.dumps({"groups": plan, "branches_skipped": skipped_branches}, ensure_ascii=False, indent=1), encoding="utf-8")
nd = sum(len(p["duplicates"]) for p in plan)
print(f"групп к слиянию: {len(plan)}; карточек-дублей: {nd}; пропущено групп с разными КПП: {len(skipped_branches)}")
print(f"перенести сделок: {sum(d['deals'] for p in plan for d in p['duplicates'])}, контактов: {sum(d['contacts'] for p in plan for d in p['duplicates'])}")
print(f"полей основной, которые дополнятся из дублей: {sum(len(p['fill_into_master']) for p in plan)} (в {sum(1 for p in plan if p['fill_into_master'])} группах)")
print(f"расхождений значений между основной и дублями (не затираем, пишем в примечание): {conflicts} (в {sum(1 for p in plan if p['conflicts'])} группах)")
print("групп, где сделки сразу в нескольких карточках:", sum(1 for p in plan if sum(1 for c in [p['master']] + p['duplicates'] if c['deals'] > 0) > 1))
print("групп, где основная уже архивная:", sum(1 for p in plan if False))
