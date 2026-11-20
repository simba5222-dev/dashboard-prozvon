#!/usr/bin/env python
"""Слияние дублей компаний Synergy (по ИНН): данные и связи переносятся в основную карточку, дубль архивируется. ПО УМОЛЧАНИЮ — сухой прогон.

    sudo -u claude .venv/bin/python scripts/company_merge_run.py --inn 9724239657 [--inn ...] [--apply]

Основную карточку и план берём из data/dups/merge_plan.json (scripts/company_merge_plan.py). На каждую группу:
  1. инвентаризация реальных связей дубля через API (сделки, контакты, заявки, счета, договоры, платежи, банковские реквизиты, задачи);
  2. резервная копия (карточки + списки связей) в data/dups/backup/ — для отката;
  3. дополняем пустые поля основной данными дубля; расхождения не затираем, дописываем в «Примечание» основной;
  4. переносим сделки (смена компании) и контакты (добавляем связь с основной); результат проверяем чтением;
  5. архивируем дубль (дата архивации) и пишем в таблицу company_merges.
Если у дубля есть связи, которые скрипт пока не умеет переносить (заявки, счета, договоры, платежи, реквизиты, задачи) — группа ПРОПУСКАЕТСЯ целиком.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import collector as co  # noqa: E402
from app.config import get_settings  # noqa: E402

RELS = ("deals", "contacts", "orders", "invoices", "contracts", "invoice-payments", "bank-details", "tasks")
MOVABLE = {"deals", "contacts", "orders", "invoices", "invoice-payments", "bank-details"}   # orders едут вслед за контактами (проверяем)
SNAP_SKIP = {"updated-at", "cached-at", "as-string"}
TABLE = """CREATE TABLE IF NOT EXISTS company_merges(inn TEXT, master_id TEXT, dup_id TEXT, moved_json TEXT, merged_at TEXT, PRIMARY KEY(master_id, dup_id))"""


def rel_delete(cl, path: str, body: dict) -> bool:
    """Снять связь «многие-ко-многим» (DELETE по адресу связи). Не критично: дубль всё равно уходит в архив."""
    import httpx
    st = get_settings()
    try:
        r = httpx.request("DELETE", f"{st.synergy_url.rstrip('/')}/{path}", json=body, timeout=30,
                          headers={"Authorization": f"Bearer {st.synergy_api_token}", "Content-Type": "application/vnd.api+json"})
        return r.status_code in (200, 204)
    except Exception:  # noqa: BLE001
        return False


def links(cl, cid: str, rel: str) -> list[str]:
    out, page = [], 1
    while True:
        rows = cl.get(f"companies/{cid}/{rel}", **{"page[number]": page}).get("data") or []
        out += [str(r["id"]) for r in rows]
        if len(rows) < 50:
            return out
        page += 1


def after_check_contact(cl, ct: str, m: str) -> bool:
    return m in {str(c["id"]) for c in cl.get(f"contacts/{ct}/companies").get("data") or []}


def inventory(cl, cid: str) -> dict[str, list[str]]:
    return {r: links(cl, cid, r) for r in RELS}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inn", action="append", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--simple", action="store_true", help="только простые группы: у дублей нет связей или только сделки и контакты; остальные откладываем")
    args = ap.parse_args()
    s = get_settings()
    cl = co.SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token)
    conn = sqlite3.connect(str(ROOT / "data" / "dashboard.db"), timeout=60)
    conn.execute(TABLE)
    plan = {g["inn"]: g for g in json.loads((ROOT / "data" / "dups" / "merge_plan.json").read_text(encoding="utf-8"))["groups"]}
    rc = 0
    for inn in args.inn:
        try:
            rc |= one_group(cl, conn, plan, inn, args)
        except Exception as exc:  # noqa: BLE001 — одна проблемная группа не должна останавливать остальные
            print(f"  ОШИБКА в группе {inn}: {type(exc).__name__}: {str(exc)[:160]} — дубли НЕ архивированы, группа остаётся для ручного разбора")
            rc = 1
    return rc


def one_group(cl, conn, plan, inn, args) -> int:
    rc = 0
    if True:
        g = plan.get(inn)
        if not g:
            print(f"ИНН {inn}: в плане слияния нет"); return 1
        m = g["master"]["id"]
        print(f"\n=== ИНН {inn}: основная {m} «{g['master']['name']}» ===")
        inv_m = inventory(cl, m)
        invs = {d["id"]: inventory(cl, d["id"]) for d in g["duplicates"]}
        allowed = {"deals", "contacts"} if args.simple else MOVABLE
        blockers = {d: {r: ids for r, ids in iv.items() if ids and r not in allowed} for d, iv in invs.items()}
        # уже слитые раньше дубли (в архиве и без связей) не трогаем: повторный запуск безопасен и дёшев
        g = dict(g)
        g["duplicates"] = [d for d in g["duplicates"] if not (not any(invs[d["id"]].values()) and cl.get(f"companies/{d['id']}")["data"]["attributes"].get("archived-at"))]
        invs = {d["id"]: invs[d["id"]] for d in g["duplicates"]}
        if not g["duplicates"]:
            print("  уже слита раньше — пропуск")
            return 0
        for d, iv in invs.items():
            print(f"  дубль {d}: связи " + (", ".join(f"{r}={len(v)}" for r, v in iv.items() if v) or "нет"))
        if any(blockers.values()):
            print(("  ОТЛОЖЕНА (не простая):" if args.simple and not any(r not in MOVABLE for b in blockers.values() for r in b) else "  ПРОПУСК группы: есть связи, которые скрипт не переносит:"), {d: {r: len(v) for r, v in b.items()} for d, b in blockers.items() if b}); return 1
        if not args.apply:
            print("  сухой прогон: будет дополнено полей", len(g["fill_into_master"]), "| перенесено сделок", sum(len(iv["deals"]) for iv in invs.values()),
                  "| контактов", sum(len(iv["contacts"]) for iv in invs.values()), "| архивировано дублей", len(g["duplicates"]))
            return rc
        # --- резервная копия ---
        snap = {"inn": inn, "time": datetime.now(timezone.utc).isoformat(), "master": cl.get(f"companies/{m}")["data"], "master_links": inv_m,
                "dups": {d["id"]: {"card": cl.get(f"companies/{d['id']}")["data"], "links": invs[d["id"]]} for d in g["duplicates"]}}
        bk = ROOT / "data" / "dups" / "backup" / f"{inn}_{int(time.time())}.json"
        bk.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print("  резервная копия:", bk.name)
        # --- дополняем основную ---
        patch = dict(g["fill_into_master"])
        note_add = [f"{c['field']}: у дубля {c['dup_id']} «{str(c['dup'])[:80]}»" for c in g["conflicts"]]
        if note_add:
            old = (snap["master"]["attributes"].get("note") or "").strip()
            patch["note"] = (old + "\n" if old else "") + "Слияние дублей " + datetime.now().strftime("%d.%m.%Y") + ": данные дублей расходились — " + "; ".join(note_add)
        if patch:
            cl.patch(f"companies/{m}", {"data": {"type": "companies", "id": m, "attributes": patch}})
            back = cl.get(f"companies/{m}")["data"]["attributes"]
            bad = [k for k, v in patch.items() if str(back.get(k) or "").strip() != str(v).strip()]
            print("  основная дополнена:", sorted(patch), "| не применилось:", bad or "нет")
            if bad:
                return 1
        # --- переносим связи ---
        ok = True
        def snap_of(path):   # поля объекта без служебных — чтобы убедиться, что перенос ничего не испортил
            a = cl.get(path)["data"]["attributes"]
            return {k: v for k, v in a.items() if k not in SNAP_SKIP}
        for d in g["duplicates"]:
            iv = invs[d["id"]]
            before = {("invoices", i): snap_of(f"invoices/{i}") for i in iv["invoices"]}
            before.update({("invoice-payments", i): snap_of(f"invoice-payments/{i}") for i in iv["invoice-payments"]})
            for deal in iv["deals"]:
                cl.patch(f"deals/{deal}", {"data": {"type": "deals", "id": deal, "relationships": {"company": {"data": {"type": "companies", "id": m}}}}})
            for ct in iv["contacts"]:
                if not after_check_contact(cl, ct, m):   # повторное добавление уже имеющейся связи Synergy отклоняет (400)
                    cl.post(f"contacts/{ct}/relationships/companies", {"data": [{"type": "companies", "id": m}]})
                if after_check_contact(cl, ct, m):
                    rel_delete(cl, f"contacts/{ct}/relationships/companies", {"data": [{"type": "companies", "id": d["id"]}]})
            for od in iv["orders"]:   # заявка привязана к компании связью «companies» (много-ко-многим)
                have = {str(x["id"]) for x in (cl.get(f"orders/{od}", include="companies")["data"]["relationships"].get("companies", {}).get("data") or [])}
                if m not in have:
                    cl.post(f"orders/{od}/relationships/companies", {"data": [{"type": "companies", "id": m}]})
                rel_delete(cl, f"orders/{od}/relationships/companies", {"data": [{"type": "companies", "id": d["id"]}]})
            for inv_id in iv["invoices"]:
                cl.patch(f"invoices/{inv_id}", {"data": {"type": "invoices", "id": inv_id, "relationships": {"payer": {"data": {"type": "companies", "id": m}}}}})
            for pay in iv["invoice-payments"]:
                # смена плательщика у платежа: обычный PATCH объекта Synergy молча игнорирует, работает только адрес связи (204)
                cl.patch(f"invoice-payments/{pay}/relationships/payer", {"data": {"type": "companies", "id": m}})
            have_acc = {cl.get(f"company-bank-details/{b}")["data"]["attributes"].get("number") for b in inv_m["bank-details"]}
            for bd in iv["bank-details"]:
                bda = cl.get(f"company-bank-details/{bd}")["data"]["attributes"]
                num = bda.get("number")
                if num in have_acc:
                    print(f"    реквизит {bd} (счёт {num}) уже есть у основной — не переношу")
                    iv["bank-details"] = [x for x in iv["bank-details"] if x != bd]
                    continue
                body = {"data": {"type": "company-bank-details", "id": bd, "relationships": {"company": {"data": {"type": "companies", "id": m}}}}}
                if not (bda.get("name") or "").strip():   # без названия Synergy отвечает 422 — подставляем банк из строки счёта
                    body["data"]["attributes"] = {"name": (num or "").split(",", 1)[-1].strip() if "," in (num or "") else "Реквизиты"}
                cl.patch(f"company-bank-details/{bd}", body)
            after_m = inventory(cl, m)
            fails = [f"{r}:{x}" for r in ("deals", "contacts", "orders", "invoices", "invoice-payments", "bank-details") for x in iv[r] if x not in after_m[r]]
            left = {r: links(cl, d["id"], r) for r in ("deals", "invoices", "invoice-payments", "orders")}
            changed = [f"{k[0]}:{k[1]}" for k, v in before.items() if snap_of(f"{k[0]}/{k[1]}") != v]
            print(f"  дубль {d['id']}: перенос {'ОК' if not fails else 'НЕ ПОДТВЕРЖДЁН ' + str(fails)}; осталось у дубля {({r: len(v) for r, v in left.items() if v}) or 'ничего'}; "
                  f"поля счетов/платежей изменились: {changed or 'нет'}")
            if fails or any(left.values()) or changed:
                ok = False; rc = 1; break
            conn.execute("INSERT OR REPLACE INTO company_merges VALUES (?,?,?,?,?)", (inn, m, d["id"], json.dumps(iv), datetime.now(timezone.utc).isoformat()))
            conn.commit()
        if not ok:
            print("  остановка: дубли НЕ архивированы"); return 1
        # --- архивируем дубли ---
        for d in g["duplicates"]:
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            cur = cl.get(f"companies/{d['id']}")["data"]["attributes"]
            if not cur.get("archived-at"):   # уже архивный дубль (часто без названия) повторно не правим: Synergy ответит 422
                attrs = {"archived-at": stamp}
                if not (cur.get("name") or "").strip():
                    attrs["name"] = f"Без названия (дубль ИНН {inn})"   # без названия Synergy не сохраняет карточку
                cl.patch(f"companies/{d['id']}", {"data": {"type": "companies", "id": d["id"], "attributes": attrs}})
            back = cl.get(f"companies/{d['id']}")["data"]["attributes"]
            print(f"  дубль {d['id']}: архив {'ОК ' + str(back.get('archived-at'))[:19] if back.get('archived-at') else 'НЕ ПРИМЕНИЛСЯ'}")
            if not back.get("archived-at"):
                rc = 1
                continue
            # наши запоминалки: «ИНН → id созданной компании» (скрипты платежей/счетов) и копия справочника — не должны вести на архивный дубль
            mp = ROOT / "data" / "payments" / "companies_created.json"
            if mp.exists():
                mem = json.loads(mp.read_text(encoding="utf-8"))
                fixed = [k for k, v in mem.items() if str(v) == str(d["id"])]
                for k in fixed:
                    mem[k] = m
                if fixed:
                    mp.write_text(json.dumps(mem, ensure_ascii=False), encoding="utf-8")
                    print(f"    companies_created.json: ИНН {fixed} теперь ведёт на основную {m}")
            conn.execute("UPDATE company_index SET archived=1 WHERE id=?", (str(d["id"]),))
            conn.commit()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
