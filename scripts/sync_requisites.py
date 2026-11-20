#!/usr/bin/env python
"""Реквизиты из 1С в Synergy: наши организации и счета, компании-плательщики и их счета.

    sudo -u claude .venv/bin/python scripts/sync_requisites.py orgs      [--apply] [--limit N]   наши организации (org-details)
    sudo -u claude .venv/bin/python scripts/sync_requisites.py accounts  [--apply] [--limit N]   наши банковские счета (account-bank-details)
    sudo -u claude .venv/bin/python scripts/sync_requisites.py payers    [--apply] [--limit N]   компании-плательщики и их счета

Вход — `data/payments/requisites2.json` (готовит выгрузка из 1С, только чтение). Без --apply — только показ.
Ничего не дублируем: организацию ищем по ИНН, счёт по номеру, компанию по ИНН (при дублях берём ту, где больше сделок).
Создание — post_once (без автоповтора). Запрос 500 — это «неизвестно»: пишем в вывод и останавливаемся, повтор
только после проверки, не создалось ли (поиск по ИНН/номеру в начале каждого запуска это и делает).
Решение владельца 17.11.2026.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import Settings  # noqa: E402

import os
TAG = os.environ.get("PAY_TAG", "2026-11")   # период выгрузки: файлы incoming_<TAG>.json и requisites_<TAG>.json
DATA = Path(__file__).resolve().parents[1] / "data" / "payments" / f"requisites_{TAG}.json"


def client() -> SynergyClient:
    s = Settings()
    return SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token, timeout_sec=60.0,
                         min_interval_sec=s.synergy_min_interval_sec, retries=s.synergy_retries)


def all_pages(cl: SynergyClient, path: str, **kw) -> list[dict]:
    out, page = [], 1
    while True:
        rows = cl.get(path, per_page=50, **{"page[number]": page}, **kw).get("data") or []
        out += rows
        if len(rows) < 50:
            return out
        page += 1


def acct_attrs(a: dict) -> dict:
    b = a["bank"] or {}
    last = (a["number"] or "")[-4:]
    return {"name": f"{b.get('name') or 'Банк'} …{last}", "bank-name": b.get("name") or "", "bik": b.get("bik") or "",
            "corr-number": b.get("corr") or "", "number": a["number"], "active": True, "kind": "calculated_invoice", "is-default": False}


def create(cl: SynergyClient, path: str, payload: dict, what: str) -> dict | None:
    try:
        r = cl.post_once(path, payload)
        print(f"  создано {what}: id {r['data']['id']}")
        return r["data"]
    except httpx.HTTPStatusError as exc:
        print(f"  ОШИБКА {what}: HTTP {exc.response.status_code} {exc.response.text[:300]}")
        return None


def cmd_orgs(cl, d, apply, limit):
    have = {x["attributes"]["inn"]: x["id"] for x in all_pages(cl, "org-details")}
    n = 0
    for o in d["orgs"]:
        if o["inn"] in have:
            print(f"есть: {o['name']} (id {have[o['inn']]})")
            continue
        attrs = {"full-name": o["full"] or o["name"], "short-name": o["short"] or o["name"], "inn": o["inn"]}
        if o["kpp"]:
            attrs["kpp"] = o["kpp"]
        if o["okpo"]:
            attrs["okpo"] = o["okpo"]
        print(f"{'создаю' if apply else 'будет создана'} организация: {attrs['short-name']} ИНН {o['inn']}")
        if apply:
            if not create(cl, "org-details", {"data": {"type": "org-details", "attributes": attrs}}, o["name"]):
                return
            n += 1
            if limit and n >= limit:
                return


def cmd_accounts(cl, d, apply, limit):
    orgs = {x["attributes"]["inn"]: x["id"] for x in all_pages(cl, "org-details")}
    have = {x["attributes"]["number"] for x in all_pages(cl, "account-bank-details")}
    need = []
    for o in d["orgs"]:
        for a in o["accounts"]:
            if a["closed"] or a["deleted"] or a["number"].startswith("42102"):
                continue
            need.append((o, a))
    for doc in d["docs"]:   # счёт, на который пришли деньги, должен быть в любом случае
        a = doc["our_acc"]
        if a and not any(a["number"] == x[1]["number"] for x in need):
            need.append((next(o for o in d["orgs"] if o["inn"] == doc["org_inn"]), a))
    n = 0
    seen = set()
    for o, a in need:
        if a["number"] in have or a["number"] in seen:
            continue
        seen.add(a["number"])
        if o["inn"] not in orgs:
            print(f"нет организации {o['name']} в Synergy — сначала `orgs`")
            continue
        attrs = acct_attrs(a)
        print(f"{'создаю' if apply else 'будет создан'} счёт {o['name'][:22]:22} {attrs['name']} р/с {a['number']}")
        if apply:
            r = create(cl, "account-bank-details", {"data": {"type": "account-bank-details", "attributes": attrs,
                       "relationships": {"org-detail": {"data": {"type": "org-details", "id": orgs[o["inn"]]}}}}}, a["number"])
            if not r:
                return
            n += 1
            if limit and n >= limit:
                return
    print(f"всего счетов нужно {len(seen)+len([1 for o,a in need if a['number'] in have])}, уже было, создаётся: {len(seen)}")


MAP = DATA.parent / "companies_created.json"   # ИНН -> id компании, созданной нами: поиск Synergy по ИНН видит новые записи с большой задержкой


def remembered() -> dict:
    return json.loads(MAP.read_text(encoding="utf-8")) if MAP.exists() else {}


def _idx_conn():
    import sqlite3
    from app.config import get_settings
    return sqlite3.connect(get_settings().db_path, timeout=60)


def find_company(cl, inn: str) -> dict | None:
    """Компания по ИНН. Ищем в своей копии справочника (app/company_index.py): `filter[inn]` Synergy находит не все карточки,
    и по этой причине раньше создавались дубли."""
    mem = remembered().get(inn)
    if mem:
        return {"id": mem, "attributes": {"inn": inn}}
    from app import company_index as ci
    rows = ci.companies_by_inn(cl, _idx_conn(), inn)
    if not rows:
        return None
    # больше сделок; при равенстве — самая старая карточка (новые чаще всего дубли)
    best = max(rows, key=lambda x: (cl.get(f"companies/{x['id']}/deals", per_page=1)["meta"]["record-count"], -int(x["id"])))
    return best


def remember_created(inn: str, comp_id: str, name: str = "", kpp: str = "") -> None:
    from app import company_index as ci
    m = remembered(); m[inn] = comp_id; MAP.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    ci.remember(_idx_conn(), comp_id, inn, name, kpp)


def cmd_payers(cl, d, apply, limit):
    seen, n = {}, 0
    for doc in d["docs"]:
        cp = doc["cp"]
        if doc["number"] and cp["inn"] in seen:
            continue
        seen[cp["inn"]] = 1
    for inn in list(seen):
        doc = next(x for x in d["docs"] if x["cp"]["inn"] == inn)
        cp = doc["cp"]
        if inn in {o["inn"] for o in d["orgs"]}:
            print(f"пропуск {cp['name']}: это наша организация")
            continue
        comp = find_company(cl, inn)
        if comp:
            print(f"компания есть: {cp['name']} (id {comp['id']})")
        else:
            attrs = {"name": cp["name"], "full-name": cp["full"] or cp["name"], "inn": inn}
            if cp["kpp"]:
                attrs["kpp"] = cp["kpp"]
            print(f"{'создаю' if apply else 'будет создана'} компания {cp['name']} ИНН {inn}")
            if not apply:
                continue
            comp = create(cl, "companies", {"data": {"type": "companies", "attributes": attrs}}, cp["name"])
            if not comp:
                return
            remember_created(inn, comp["id"], cp["name"], cp.get("kpp") or "")
            n += 1
        # счета плательщика
        have = {x["attributes"].get("number") for x in cl.get(f"companies/{comp['id']}/bank-details", per_page=50).get("data") or []} if comp else set()
        for dd in d["docs"]:
            a = dd["cp_acc"]
            if dd["cp"]["inn"] != inn or not a or a["number"] in have:
                continue
            have.add(a["number"])
            attrs = acct_attrs(a)
            attrs.pop("kind"); attrs.pop("is-default")   # у счетов компаний этих полей нет
            print(f"  {'добавляю' if apply else 'будет добавлен'} счёт плательщика {attrs['name']} р/с {a['number']}")
            if apply and not create(cl, "company-bank-details", {"data": {"type": "company-bank-details", "attributes": attrs,
                                    "relationships": {"company": {"data": {"type": "companies", "id": comp["id"]}}}}}, a["number"]):
                return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["orgs", "accounts", "payers"])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    d = json.loads(DATA.read_text(encoding="utf-8"))
    # только оплаты покупателей (решение владельца 17.11.2026): возвраты, займы и переводы в Synergy не грузим
    inc = {i["number"]: i for i in json.loads((DATA.parent / f"incoming_{TAG}.json").read_text(encoding="utf-8"))}
    own = {o["inn"] for o in d["orgs"]}   # тот же отбор, что у load_1c_payments.py: без своих организаций и без МСК
    d["docs"] = [x for x in d["docs"] if x["number"] in inc and inc[x["number"]]["kind"] == "ОплатаПокупателя"
                 and inc[x["number"]]["inn"] not in own and "МСК" not in inc[x["number"]]["org"]]
    {"orgs": cmd_orgs, "accounts": cmd_accounts, "payers": cmd_payers}[a.what](client(), d, a.apply, a.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
