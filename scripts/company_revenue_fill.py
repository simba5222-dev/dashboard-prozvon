#!/usr/bin/env python
"""Выручка компаний из ГИР БО (bo.nalog.gov.ru) -> поле «Оборот» (custom-34, млн ₽).

Без --apply ничего в Synergy не пишет: только показывает, что записало бы.
Пишет ТОЛЬКО в пустые поля (--overwrite снимает это ограничение).
Аргументы: --limit N, --apply, --check N (сверка с уже заполненными, без записи).
"""
import argparse, json, sys, time
from pathlib import Path
import httpx as requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIELD = "custom-34"
H = {"User-Agent": "Mozilla/5.0"}


def revenue(inn):
    """-> (млн ₽ | None, период | None, причина)"""
    for t in range(3):
        try:
            r = requests.get("https://bo.nalog.gov.ru/advanced-search/organizations/search",
                             params={"query": inn, "page": 0, "size": 5}, headers=H, timeout=25)
            if r.status_code == 429:
                time.sleep(15 * (t + 1)); continue
            r.raise_for_status()
            break
        except Exception as e:  # noqa
            if t == 2:
                return None, None, f"ошибка: {str(e)[:50]}"
            time.sleep(5)
    items = [x for x in r.json().get("content", []) if x.get("inn", "").replace("<strong>", "").replace("</strong>", "") == inn]
    if not items:
        return None, None, "нет в ГИР БО"
    bfo = items[0].get("bfo") or {}
    if bfo.get("gainSum") is None:
        return None, None, "нет отчётности"
    return round(bfo["gainSum"] / 1000), bfo.get("period"), "ок"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--stubs", action="store_true", help="менять значения меньше 10 млн")
    ap.add_argument("--check", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args()
    d = json.load(open(ROOT / "data/dups/companies_full.json"))
    g = lambda c: (c["attrs"].get("customs") or {}).get(FIELD)
    has = lambda v: v not in (None, "", [])
    inn = lambda c: (c["attrs"].get("inn") or "").strip()
    live = [c for c in d if not c["attrs"].get("archived-at") and len(inn(c)) == 10]
    if a.check:
        rows = [c for c in live if has(g(c))][: a.check]
        ok = 0
        for c in rows:
            v, p, why = revenue(inn(c)); time.sleep(1)
            print(f"{c['attrs']['name'][:32]:32} {inn(c)}  CRM={g(c):>9.0f}  ГИР БО={v} ({p}) {why}")
            ok += v is not None and abs(v - g(c)) <= max(1, 0.02 * g(c))
        print(f"совпало {ok} из {len(rows)}")
        return
    stub = lambda c: has(g(c)) and float(g(c)) < 10
    todo = [c for c in live if a.overwrite or not has(g(c)) or (a.stubs and stub(c))]
    todo = todo[: a.limit] if a.limit else todo
    cl = None
    if a.apply:
        from app import collector as co
        from app.config import get_settings
        s = get_settings(); cl = co.SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token)
    fp = ROOT / "data/dups/revenue_run.json"
    res = json.load(open(fp)) if (a.apply and fp.exists()) else {}
    cache = {}
    stat = {"записано": 0, "заглушек заменено": 0, "нет данных": 0, "совпало": 0, "ошибок": 0}
    for i, c in enumerate(todo, 1):
        if res.get(c["id"], {}).get("записано"):
            continue
        n = inn(c)
        if n not in cache:
            cache[n] = revenue(n); time.sleep(1)
        v, p, why = cache[n]
        old = g(c)
        r = {"inn": n, "название": c["attrs"].get("name"), "было": old, "млн": v, "год": p, "итог": why}
        if v is None:
            stat["нет данных"] += 1
        elif old is not None and has(old) and abs(float(old) - v) < 0.5:
            stat["совпало"] += 1
        elif a.apply:
            try:
                cl.patch(f"companies/{c['id']}", {"data": {"type": "companies", "id": str(c["id"]),
                         "attributes": {"customs": {FIELD: v}}}})
                r["записано"] = True; stat["записано"] += 1
                stat["заглушек заменено"] += has(old)
            except Exception as e:  # noqa
                r["ошибка"] = str(e)[:120]; stat["ошибок"] += 1
        res[c["id"]] = r
        if i % 25 == 0:
            json.dump(res, open(fp, "w"), ensure_ascii=False, indent=1)
            print(i, len(todo), stat, flush=True)
    json.dump(res, open(fp, "w"), ensure_ascii=False, indent=1)
    print("ГОТОВО", len(todo), stat, "(записано)" if a.apply else "(сухой прогон)", flush=True)


main()
