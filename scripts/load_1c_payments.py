#!/usr/bin/env python
"""Загрузка входящих платежей из 1С в раздел «Платежи» Synergy (без привязки к сделкам).

    sudo -u claude .venv/bin/python scripts/load_1c_payments.py --file data/payments/incoming_2026-11-16.json            показать, что будет создано
    sudo -u claude .venv/bin/python scripts/load_1c_payments.py --file ... --only СКБП-003396 --apply                  создать один (проба)
    sudo -u claude .venv/bin/python scripts/load_1c_payments.py --file ... --apply                                      создать все

Файл готовит `invoices/onec_incoming.py` на офисе (поступления на расчётный счёт из 1С, только чтение).
Решение владельца 17.11.2026: загрузить ВСЕ входящие платежи в «Платежи» без автопривязки к сделкам — разносят сотрудники
вручную. Поэтому сделку/плательщика не указываем, а всё, что нужно для разноски, пишем в сам платёж:
  amount, direction=income, status=executed, payment-type=non_cash, purpose (назначение из 1С), custom-number (номер п/п банка),
  operation-*-at (дата документа 1С), customs.custom-30007 «ИНН контрагента», description (человекочитаемая сводка: документ 1С,
  вид операции, плательщик и ИНН, организация, счёт покупателю, п/п).
Повторов нет: документ 1С отмечается в таблице `onec_payments_loaded` и меткой «[1С:НОМЕР]» в описании платежа; перед
созданием сверяемся и с базой, и с уже существующими платежами Synergy. Создание — `post_once` (без автоповтора).
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

from app.collector import SynergyClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema  # noqa: E402

KINDS = {"ОплатаПокупателя": "Оплата покупателя", "ВозвратОтПоставщика": "Возврат от поставщика",
         "ПолучениеЗайма": "ПОЛУЧЕНИЕ ЗАЙМА (не оплата клиента)", "ПереводСДругогоСчета": "ПЕРЕВОД МЕЖДУ СВОИМИ СЧЕТАМИ (не оплата клиента)"}
FIELD_INN = "custom-30007"
# Платежи, пришедшие на МСК ТЕХНОРЕСУРС, пока исключены (решение владельца 17.11.2026, «пока»).
# ИНН наших организаций: платежи между своими компаниями — внутренние переводы, в «Платежи» не грузим (решение владельца 17.11.2026)
OUR_INNS = {"7804660264", "7811634064", "7804701601", "7814715762", "2901228614", "2901237633", "290122828563", "290130353916", "290128783284", "290126875243"}
# Получатель и его счёт: у всех 92 приходов в Synergy это запись №7 (наша организация); без счёта получателя создание даёт 422 «Выберите счёт»
RECEIVER_ID = "7"


def fdate(iso: str) -> str:
    return datetime.fromisoformat(iso[:19]).strftime("%d.%m.%Y") if iso else ""


def build(item: dict, hint: str = "") -> tuple[str, dict]:
    token = f"[1С:{item['number']}]"
    kind = KINDS.get(item["kind"], item["kind"])
    parts = [f"{token} Загружено из 1С: поступление на р/с {item['number']} от {fdate(item['date'])}.",
             f"Вид операции: {kind}.",
             f"Плательщик: {item['counterparty'] or 'не указан'}" + (f", ИНН {item['inn']}" if item["inn"] else "") + ".",
             f"Организация: {item['org']}."]
    if item["pp_number"]:
        parts.append(f"П/п банка № {item['pp_number']} от {fdate(item['pp_date'])}.")
    if item["invoice_number"]:
        parts.append(f"Закрывает счёт покупателю 1С {item['invoice_number']} от {fdate(item['invoice_date'])}.")
    parts.append("Сделка не указана: разнести вручную.")
    if hint:
        parts.append(hint)
    when = item["date"][:19] + "+03:00"
    attrs = {"amount": float(item["amount"]), "direction": "income", "status": "not_executed", "payment-type": "non_cash",
             "purpose": item["purpose"][:1000], "description": " ".join(parts),
             "operation-executed-at": when, "operation-accounted-at": when, "operation-created-at": when}
    if re.fullmatch(r"\d{1,9}", str(item["pp_number"])):
        attrs["custom-number"] = int(item["pp_number"])
    customs = {}
    if re.fullmatch(r"\d{10,12}", str(item["inn"] or "")):
        customs[FIELD_INN] = int(item["inn"])
    if customs:
        attrs["customs"] = customs
    return token, attrs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True)
    ap.add_argument("--apply", action="store_true", help="создать платежи (без флага — только показ)")
    ap.add_argument("--only", help="только документ 1С с этим номером (проба)")
    args = ap.parse_args()

    s = Settings()
    cl = SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token, timeout_sec=60.0,
                       min_interval_sec=s.synergy_min_interval_sec, retries=s.synergy_retries)
    conn = connect(s.db_path)
    init_schema(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS onec_payments_loaded (
        doc_ref TEXT PRIMARY KEY, doc_number TEXT NOT NULL, synergy_id TEXT NOT NULL, amount REAL NOT NULL, loaded_at TEXT NOT NULL)""")
    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    items = [i for i in items if i["kind"] == "ОплатаПокупателя" and i["inn"] not in OUR_INNS and "МСК" not in i["org"]]   # решение владельца 17.11.2026: только оплаты покупателей
    if args.only:
        items = [i for i in items if i["number"] == args.only]
    loaded = {r[0] for r in conn.execute("SELECT doc_ref FROM onec_payments_loaded")}

    # что уже есть в Synergy (метка документа 1С в описании) — защита, если запись в нашей базе потеряется
    existing: dict[str, str] = {}
    page = 1
    while True:
        rows = cl.get("invoice-payments", per_page=50, sort="-created-at", **{"page[number]": page}).get("data") or []
        for r in rows:
            m = re.search(r"\[1С:([^\]]+)\]", str((r.get("attributes") or {}).get("description") or ""))
            if m:
                existing[m.group(1)] = r["id"]
        if len(rows) < 50:
            break
        page += 1

    from payment_hints import deals_for_inn, make_hint   # подсказка «к какой сделке разнести» — в описание платежа
    hint_cache: dict = {}
    made = skipped = failed = 0
    total = 0.0
    for it in items:
        if it["ref"] in loaded or it["number"] in existing:
            token, attrs = build(it)
        elif args.apply:
            try:
                hint = make_hint(it, deals_for_inn(cl, it["inn"], hint_cache) if it.get("inn") else [])
            except Exception as exc:   # подсказка — вспомогательная: её сбой не должен останавливать загрузку платежей
                print(f"  подсказку для {it['number']} получить не удалось: {str(exc)[:80]}")
                hint = ""
            token, attrs = build(it, hint)
        else:
            token, attrs = build(it)
        if it["ref"] in loaded or it["number"] in existing:
            print(f"  пропуск {it['number']}: уже загружен (Synergy id {existing.get(it['number'], '—')})")
            skipped += 1
            continue
        line = f"{it['number']} | {fdate(it['date'])} | {float(it['amount']):>12,.2f} ₽ | {KINDS.get(it['kind'], it['kind'])[:24]:24} | {it['counterparty'][:30]}"
        if not args.apply:
            print("  будет создан:", line)
            continue
        resp, dropped = None, []
        while True:
            try:
                resp = cl.post_once("invoice-payments", {"data": {"type": "invoice-payments", "attributes": attrs, "relationships": {
                    "receiver": {"data": {"type": "org-details", "id": RECEIVER_ID}},
                    "receiver-bank-detail": {"data": {"type": "account-bank-details", "id": RECEIVER_ID}}}}})
                break
            except httpx.HTTPStatusError as exc:
                # 400 «Param not allowed»: Synergy отклоняет запрос ДО создания, поэтому безопасно убрать это поле и повторить
                m = re.search(r"(?:^|\W)([a-z\-]+(?:\.[a-z0-9\-]+)?) is not allowed", exc.response.text)
                if exc.response.status_code == 400 and m and m.group(1) in attrs and len(dropped) < 6:
                    attrs.pop(m.group(1))
                    dropped.append(m.group(1))
                    continue
                if exc.response.status_code == 400 and m and m.group(1).startswith("customs") and "customs" in attrs and len(dropped) < 6:
                    attrs.pop("customs")
                    dropped.append("customs")
                    continue
                print(f"  ОШИБКА {it['number']}: HTTP {exc.response.status_code} {exc.response.text[:300]}" + (f" (уже убрал поля: {dropped})" if dropped else ""))
                resp = None
                break
        if resp is None:
            failed += 1
            if args.only or failed >= 3:   # на пробе и при серии отказов останавливаемся
                break
            continue
        if dropped:
            print(f"  (Synergy не принял поля при создании, убраны: {', '.join(dropped)})")
        pid = str(resp["data"]["id"])
        conn.execute("INSERT OR REPLACE INTO onec_payments_loaded VALUES (?, ?, ?, ?, ?)",
                     (it["ref"], it["number"], pid, float(it["amount"]), datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        made += 1
        total += float(it["amount"])
        print(f"  создан платёж Synergy {pid} <- {line}")
    print(f"итого: создано {made} на {total:,.2f} ₽, пропущено {skipped}, ошибок {failed}" + ("" if args.apply else " (показ без записи)"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
