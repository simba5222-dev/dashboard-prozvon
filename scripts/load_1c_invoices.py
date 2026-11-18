#!/usr/bin/env python
"""Счета на оплату покупателям из 1С -> раздел «Счета» Synergy, привязанные к компаниям (решение владельца 18.11.2026).

    sudo -u claude .venv/bin/python scripts/load_1c_invoices.py --file data/payments/invoices_2026-11.json              показать план
    sudo -u claude .venv/bin/python scripts/load_1c_invoices.py --file ... --only CКБП-002879 --apply                  один счёт (проба)
    sudo -u claude .venv/bin/python scripts/load_1c_invoices.py --file ... --apply                                      все

Файл готовит `invoices/onec_invoices.py` на офисе (только чтение 1С). Что пишем в счёт Synergy:
  номер 1С (custom-number), дату, сумму, НДС, плательщика = компания Synergy по ИНН (нет компании — создаём), нашу организацию
  (org-details по ИНН), ответственного = менеджер из 1С (если нашёлся среди пользователей Synergy), комментарий 1С и строки услуг
  (invoice-positions). Номер сделки в счёте 1С не указан, поэтому к сделке не привязываем.
Повторов нет: счёт отмечается в таблице `onec_invoices_loaded` и ищется среди последних счетов Synergy по номеру 1С.
Создание — post_once, без автоповтора. Ответ 500/таймаут — «неизвестно»: останавливаемся и проверяем руками.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import Settings  # noqa: E402
from app.db import connect  # noqa: E402
import sync_requisites as sr  # noqa: E402  — компании по ИНН (с памятью созданных) и их создание

DATA = Path(__file__).resolve().parents[1] / "data" / "payments"


def fdate(iso: str) -> str:
    return datetime.fromisoformat(iso[:19]).strftime("%d.%m.%Y")


def purpose_of(inv: dict) -> str:
    """Назначение платежа счёта — только услуги из счёта и общая сумма (решение владельца 18.11.2026):
    «услуги автокрана, ул. Автогенная д 6, 19.11.2026. Сумма общая 22 000 руб.» Без «Оплата по счёту № …» и без строки про НДС."""
    texts = []
    for l in inv["lines"]:
        t = " ".join((l.get("text") or "").split()).strip(" .")
        if t:
            texts.append(t[0].lower() + t[1:] if t[:6].lower() == "услуги" else t)
    what = "; ".join(texts)   # без обрезки: у поля «Назначение платежа» в Synergy лимита нет (проверено до 5 000 знаков)
    total = f"{float(inv['amount']):,.2f}".replace(",", " ").replace(".00", "")
    return (what + ". " if what else "") + f"Сумма общая {total} руб."


def reason_of(inv: dict, has_user: bool) -> str:
    """Служебный текст: кто выставил и комментарий менеджера из 1С (идёт в «Основание»)."""
    return " · ".join(x for x in (f"Выставлен от: {inv['org_name']}", "" if has_user else f"Менеджер (1С): {inv['manager']}" if inv["manager"] else "", inv["comment"]) if x)[:500]


def vat_of(inv: dict) -> tuple[str, str | None]:
    rates = {re.sub(r"\D", "", str(l.get("vat_rate") or "")) for l in inv["lines"] if l.get("vat_rate")}
    rates.discard("")
    if inv.get("vat_included") and len(rates) == 1:
        return "include", f"{float(next(iter(rates))):.1f}"
    return "without", None


def users_map(cl) -> dict[str, str]:
    """«фамилия имя» (в нижнем регистре) -> id пользователя Synergy."""
    out, page = {}, 1
    while True:
        rows = cl.get("users", per_page=50, **{"page[number]": page}).get("data") or []   # корневые списки Synergy отдают по 50
        for u in rows:
            a = u["attributes"]
            if a.get("disabled"):   # отключённого пользователя ответственным не назначить (400 RESPONSIBLE_USER_INACTIVE)
                continue
            out[f"{a.get('last-name') or ''} {a.get('first-name') or ''}".strip().lower()] = u["id"]
        if len(rows) < 50:
            return out
        page += 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    s = Settings()
    cl = sr.client()
    conn = connect(s.db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS onec_invoices_loaded (
        doc_ref TEXT PRIMARY KEY, doc_number TEXT NOT NULL, synergy_id TEXT NOT NULL, loaded_at TEXT NOT NULL)""")
    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if args.only:
        items = [i for i in items if i["number"] == args.only]
    loaded = {r[0] for r in conn.execute("SELECT doc_ref FROM onec_invoices_loaded")}
    # что уже есть в Synergy — защита на случай потери нашей записи (по номеру 1С в custom-number)
    existing, page, top_no = {}, 1, 0
    while True:
        rows = cl.get("invoices", per_page=50, sort="-created-at", **{"page[number]": page}).get("data") or []
        for r in rows:
            existing[str(r["attributes"].get("custom-number"))] = r["id"]
            top_no = max(top_no, int(r["attributes"].get("number") or 0))
        if len(rows) < 50 or page >= 12:
            break
        page += 1
    orgs = {x["attributes"]["inn"]: x["id"] for x in sr.all_pages(cl, "org-details")}
    users = users_map(cl)
    made = skipped = failed = 0
    for inv in items:
        if inv["ref"] in loaded or inv["number"] in existing:
            skipped += 1
            continue
        comp = sr.find_company(cl, inv["cp_inn"]) if inv["cp_inn"] else None
        mgr = users.get(" ".join(inv["manager"].lower().split()[:2]))
        vat_kind, vat_rate = vat_of(inv)
        line = f"{inv['number']} | {fdate(inv['date'])} | {inv['amount']:>11,.0f} ₽ | {inv['cp_name'][:26]:26} | компания {'#' + comp['id'] if comp else 'НЕТ (будет создана)'} | менеджер {'#' + mgr if mgr else 'нет в Synergy: ' + inv['manager']}"
        if not args.apply:
            print("  будет создан:", line)
            continue
        if not comp:
            attrs = {"name": inv["cp_name"], "full-name": inv["cp_name"], "inn": inv["cp_inn"]}
            if inv["cp_kpp"]:
                attrs["kpp"] = inv["cp_kpp"]
            comp = sr.create(cl, "companies", {"data": {"type": "companies", "attributes": attrs}}, inv["cp_name"])
            if not comp:
                failed += 1
                break
            m = sr.remembered(); m[inv["cp_inn"]] = comp["id"]; sr.MAP.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        attrs = {"custom-number": inv["number"], "amount": float(inv["amount"]), "issued-at": inv["date"][:19] + "+03:00", "payment-type": "non_cash",
                 "status": "not_paid", "vat-kind": vat_kind}
        if vat_rate:
            attrs["vat-rate"] = vat_rate
        # нашу организацию Synergy через API в счёт поставить не даёт (500 на любую org-detail, проверено 18.11.2026), поэтому «кто выставил» — в тексте
        attrs["purpose"] = purpose_of(inv)
        attrs["reason"] = reason_of(inv, bool(mgr))
        attrs["number"] = top_no + 1   # Synergy без номера отвечает 500; нумерация сквозная, берём следующий
        rel = {"payer": {"data": {"type": "companies", "id": comp["id"]}}}
        if mgr:
            rel["responsible"] = {"data": {"type": "users", "id": mgr}}
        dropped = []
        while True:
            try:
                resp = cl.post_once("invoices", {"data": {"type": "invoices", "attributes": attrs, "relationships": rel}})
                break
            except httpx.HTTPStatusError as exc:
                m = re.search(r"(?:^|\W)([a-z\-]+) is not allowed", exc.response.text)
                if exc.response.status_code == 400 and m and m.group(1) in attrs and len(dropped) < 5:   # отклонено до создания — безопасно убрать поле
                    attrs.pop(m.group(1)); dropped.append(m.group(1)); continue
                print(f"  ОШИБКА {inv['number']}: HTTP {exc.response.status_code} {exc.response.text[:400]}" + (f" (убрал поля: {dropped})" if dropped else ""))
                resp = None
                break
        if resp is None:
            failed += 1
            if args.only or failed >= 3:
                break
            continue
        sid = str(resp["data"]["id"])
        conn.execute("INSERT OR REPLACE INTO onec_invoices_loaded VALUES (?,?,?,?)", (inv["ref"], inv["number"], sid, datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        # строки услуг
        for l in inv["lines"]:
            pa = {"name": (l["text"] or "Услуга")[:250], "quantity": float(l.get("qty") or 1), "amount": float(l.get("price") or l.get("sum") or 0), "unit": "шт"}
            try:
                cl.post_once("invoice-positions", {"data": {"type": "invoice-positions", "attributes": pa, "relationships": {"invoice": {"data": {"type": "invoices", "id": sid}}}}})
            except httpx.HTTPStatusError as exc:
                print(f"    строка счёта {inv['number']}: HTTP {exc.response.status_code} {exc.response.text[:300]}")
                break
        top_no = attrs["number"]
        got = float(cl.get(f"invoices/{sid}")["data"]["attributes"].get("amount") or 0)
        if abs(got - float(inv["amount"])) > 0.005:
            print(f"    ВНИМАНИЕ: сумма счёта в Synergy {got:,.2f} ₽ не равна сумме в 1С {float(inv['amount']):,.2f} ₽ ({inv['number']})")
        made += 1
        print(f"  создан счёт Synergy {sid} (№{attrs['number']}) <- {line}" + (f" (Synergy не принял поля: {dropped})" if dropped else ""))
        if args.limit and made >= args.limit:
            break
    print(f"итого: создано {made}, пропущено {skipped}, ошибок {failed}" + ("" if args.apply else " (показ без записи)"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
