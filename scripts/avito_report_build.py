#!/usr/bin/env python
"""Отчёт по объявлениям Авито: звонки, заявки, сделки, расходы и цена результата (xlsx). Только чтение.

    /home/claude/dashboard/.venv/bin/python -I scripts/avito_report_build.py --out /home/agent/avito-report/report.xlsx

Источники:
  * звонки Авито (CallTracking) — таблица `avito_calls` дашборда (даёт объявление `item_id`, клиента и время звонка);
  * заявки CRM — `data/ad_report/orders.jsonl` (ad_report_fetch_orders.py; тип техники, этап, причина проигрыша);
  * объявления, статистика и расходы Авито — `data/avito/*.json` (avito_items_pull.py).
Звонок Авито связывается с заявкой по телефону клиента: заявка создана от 2 минут до звонка до 30 минут после него
(ближайшая). Заявка относится к объявлению, по которому пришёл этот звонок. Обращения только в чате Авито сюда
не входят: чаты ещё не подключены (в расходах они учтены, в заявках — нет).
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

MSK = timezone(timedelta(hours=3))
DB = "/home/claude/dashboard/data/dashboard.db"
DATA = Path("/home/claude/dashboard/data")
ORDER_URL = "https://app.synergycrm.ru/orders/"
HEAD = PatternFill("solid", fgColor="2F4B6E")
THIN = Side(style="thin", color="BBBBBB")
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
BOLD = Font(name="Arial", size=10, bold=True)
SUB = PatternFill("solid", fgColor="E8EEF5")
NO_TRANSPORT = "Не определён"


def digits(v) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())[-10:]


def group(stage: str, kind: str) -> str:
    if kind == "won" or stage == "Сделка":
        return "Сделка"
    if kind == "lost" or "провалена" in stage.lower():
        return "Проиграна"
    if stage == "Пропущенный":
        return "Пропущенный"
    if kind == "opened" or stage == "Новый":
        return "Новый"
    return "В работе"


def style(ws, widths=None, header_row=1, height=36):
    for c in ws[header_row]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = HEAD
        c.alignment = Alignment(wrap_text=True, vertical="center")
        c.border = BORDER
    f_body, a_body = Font(name="Arial", size=10), Alignment(wrap_text=True, vertical="top")
    for row in ws.iter_rows(min_row=header_row + 1):
        for c in row:
            c.font = f_body
            c.alignment = a_body
            c.border = BORDER
    for col, w in (widths or {}).items():
        ws.column_dimensions[col].width = w
    ws.row_dimensions[header_row].height = height


def per(a, b):
    return (a / b) if b else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--since", default="2026-01-01")
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    calls = [dict(r) for r in db.execute("SELECT * FROM avito_calls WHERE local_date >= ? ORDER BY call_time", (a.since,))]
    for c in calls:
        c["_t"] = datetime.fromisoformat(c["call_time"]).astimezone(MSK).replace(tzinfo=None)
        c["_p"] = digits(c["buyer_phone"])
    items = {int(r["id"]): r for r in json.loads((DATA / "avito/items.json").read_text(encoding="utf-8"))}
    stats = json.loads((DATA / "avito/stats_by_month.json").read_text(encoding="utf-8"))
    spend_file = DATA / "avito/spend_month.json"
    spend_acc = json.loads(spend_file.read_text(encoding="utf-8")) if spend_file.exists() else {}

    # ---- заявки CRM ----
    orders = []
    for line in open(DATA / "ad_report/orders.jsonl", encoding="utf-8"):
        o = json.loads(line)
        p = digits(o.get("client_phone") or o.get("contact_phone"))
        if not p:
            continue
        o["_p"] = p
        o["_t"] = datetime.fromisoformat(o["created_at"]).astimezone(MSK).replace(tzinfo=None)
        o["_g"] = group(str(o.get("stage") or ""), str(o.get("stage_kind") or ""))
        t = str(o.get("transport_rel") or o.get("transport_field") or "").strip()
        o["_type"] = t if t and t not in ("-", "—") else ""
        loss = str(o.get("loss_rel") or o.get("loss_field") or "").strip()
        o["_loss"] = "" if loss in ("-", "—") else loss
        orders.append(o)
    by_phone = collections.defaultdict(list)
    for o in orders:
        by_phone[o["_p"]].append(o)

    # ---- звонок Авито -> заявка (ближайшая, создана от -2 мин до +30 мин) ----
    used_orders: set = set()
    for c in sorted(calls, key=lambda c: c["_t"]):
        best = None
        for o in by_phone.get(c["_p"], []):
            d = (o["_t"] - c["_t"]).total_seconds()
            if -120 <= d <= 1800 and (best is None or abs(d) < abs(best[0])):
                best = (d, o)
        c["_order"] = best[1] if best else None

    # ---- агрегаты по объявлению ----
    item_ids = {c["item_id"] for c in calls if c["item_id"]}
    for per_month in stats.values():
        item_ids |= {int(i) for i in per_month}
    agg: dict = {}
    for iid in item_ids:
        agg[iid] = dict(calls=0, talked=0, orders={}, impr=0, views=0, contacts=0, phone=0, chat=0, fav=0, spend=0.0, bonus=0.0,
                       chats=0, chats_un=0, chat_orders=0)
    for c in calls:
        if not c["item_id"]:
            continue
        g = agg[c["item_id"]]
        g["calls"] += 1
        g["talked"] += c["talk_sec"] > 0
        if c["_order"] is not None:
            g["orders"].setdefault(c["_order"]["id"], c["_order"])
    for month, per_item in stats.items():
        for iid, m in per_item.items():
            g = agg[int(iid)]
            g["impr"] += m.get("impressions", 0)
            g["views"] += m.get("views", 0)
            g["contacts"] += m.get("contacts", 0)
            g["phone"] += m.get("contactsShowPhone", 0)
            g["chat"] += m.get("contactsMessenger", 0)
            g["fav"] += m.get("favorites", 0)
            money = (m.get("spending", 0) or 0) / 100.0                    # деньги, копейки -> рубли
            g["spend"] += money
            g["bonus"] += max((m.get("allSpending", 0) or 0) / 100.0 - money, 0.0)   # allSpending = деньги + бонусы Авито

    # ---- чаты Авито: сообщения клиента, ответ менеджера, телефон в тексте -> заявка CRM ----
    since_ts = int(datetime.fromisoformat(a.since).replace(tzinfo=MSK).timestamp())
    chats = {r["chat_id"]: dict(r) for r in db.execute("SELECT * FROM avito_chats WHERE created >= ?", (since_ts,))}
    msgs = collections.defaultdict(list)
    for r in db.execute("SELECT * FROM avito_messages WHERE chat_id IN (SELECT chat_id FROM avito_chats WHERE created >= ?) ORDER BY created", (since_ts,)):
        msgs[r["chat_id"]].append(dict(r))
    PH = re.compile(r"(?<!\d)(?:\+?7|8)?[\s\-(]*(\d{3})[\s\-)]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})(?!\d)")

    def chat_phones(text):
        out = set()
        for m in PH.finditer(text or ""):
            ph = "".join(m.groups())
            if ph[0] == "9" or ph[:3] in ("812", "495", "499"):
                out.add(ph)
        return out

    assigned = {oid for g in agg.values() for oid in g["orders"]}
    chat_rows = []
    for cid, ch in chats.items():
        ms = [m for m in msgs.get(cid, []) if m["type"] != "system"]
        ins = [m for m in ms if m["direction"] == "in"]
        if not ins:
            continue
        outs = [m for m in ms if m["direction"] == "out" and m["created"] >= ins[0]["created"]]
        phs = set()
        for m in ins:
            if m["type"] == "text":
                phs |= chat_phones(m["text"])
        best = None
        for ph in phs:
            for o in by_phone.get(ph, []):
                d = o["_t"].replace(tzinfo=MSK).timestamp() - ch["created"]
                if -3600 <= d <= 7 * 86400 and (best is None or abs(d) < abs(best[0])):
                    best = (d, o)
        first_dt = datetime.fromtimestamp(ins[0]["created"], MSK).replace(tzinfo=None)
        row = dict(ch=ch, first=ins[0], first_dt=first_dt, n_in=len(ins), answered=bool(outs),
                   resp=(outs[0]["created"] - ins[0]["created"]) if outs else None, phones=phs, order=best[1] if best else None)
        chat_rows.append(row)
        iid = ch.get("item_id")
        if iid:
            g = agg.setdefault(iid, dict(calls=0, talked=0, orders={}, impr=0, views=0, contacts=0, phone=0, chat=0, fav=0, spend=0.0,
                                         bonus=0.0, chats=0, chats_un=0, chat_orders=0))
            g["chats"] += 1
            g["chats_un"] += not row["answered"]
            o = row["order"]
            if o is not None and o["id"] not in assigned:   # заявка, которой не нашлось звонка: пришла через чат
                o["_via"] = "чат"
                g["orders"][o["id"]] = o
                g["chat_orders"] += 1
                assigned.add(o["id"])

    def card(iid):
        it = items.get(iid) or {}
        return (it.get("title") or f"объявление {iid} (нет в списке профиля)"), it.get("url") or f"https://www.avito.ru/{iid}", it.get("status") or "", it.get("price")

    def ad_type(g):
        cnt = collections.Counter(o["_type"] for o in g["orders"].values() if o["_type"])
        return cnt.most_common(1)[0][0] if cnt else NO_TRANSPORT

    def outcome(g):
        cnt = collections.Counter(o["_g"] for o in g["orders"].values())
        return cnt

    wb = Workbook()

    # ============ Объявления ============
    wa = wb.active
    wa.title = "Объявления"
    head = ["Объявление", "Тип техники (по заявкам)", "Статус на Авито", "Цена, ₽", "Показы", "Просмотры", "Контакты", "из них позвонили", "из них написали",
            "В избранное", "Звонков Авито", "из них с разговором", "Заявок CRM", "Сделок", "Проиграно", "В работе/новых", "Доля сделок от заявок",
            "Расход деньгами, ₽", "Оплачено бонусами, ₽", "Цена контакта, ₽", "Цена звонка, ₽", "Цена заявки, ₽", "Цена сделки, ₽", "Главная причина проигрыша", "Чатов (клиент написал)", "Чатов без ответа", "Заявок только из чатов", "Ссылка на объявление"]
    wa.append(head)
    rows_sorted = sorted(agg.items(), key=lambda kv: (-kv[1]["calls"], -kv[1]["spend"]))
    for iid, g in rows_sorted:
        title, url, status, price = card(iid)
        oc = outcome(g)
        loss = collections.Counter(o["_loss"] for o in g["orders"].values() if o["_g"] == "Проиграна" and o["_loss"])
        n_ord = len(g["orders"])
        wa.append([title, ad_type(g), status, price, g["impr"], g["views"], g["contacts"], g["phone"], g["chat"], g["fav"], g["calls"], g["talked"],
                   n_ord, oc["Сделка"], oc["Проиграна"], oc["Новый"] + oc["В работе"] + oc["Пропущенный"], per(oc["Сделка"], n_ord),
                   round(g["spend"], 2), round(g["bonus"], 2) or None, per(g["spend"], g["contacts"]), per(g["spend"], g["calls"]), per(g["spend"], n_ord),
                   per(g["spend"], oc["Сделка"]), loss.most_common(1)[0][0] if loss else None, g["chats"], g["chats_un"], g["chat_orders"], url])
    last = wa.max_row if False else len(rows_sorted) + 1
    wa.append(["ИТОГО", "", "", "", *[sum(g[k] for g in agg.values()) for k in ("impr", "views", "contacts", "phone", "chat", "fav", "calls", "talked")],
               sum(len(g["orders"]) for g in agg.values()), sum(outcome(g)["Сделка"] for g in agg.values()),
               sum(outcome(g)["Проиграна"] for g in agg.values()), None, None, round(sum(g["spend"] for g in agg.values()), 2),
               round(sum(g["bonus"] for g in agg.values()), 2), None, None, None, None, None, None,
               sum(g["chats"] for g in agg.values()), sum(g["chats_un"] for g in agg.values()), sum(g["chat_orders"] for g in agg.values()), None])
    style(wa, widths={"A": 44, "B": 26, "C": 11, "D": 9, "E": 9, "F": 10, "G": 10, "H": 10, "I": 10, "J": 9, "K": 10, "L": 11, "M": 9, "N": 8,
                      "O": 10, "P": 11, "Q": 11, "R": 13, "S": 12, "T": 11, "U": 11, "V": 11, "W": 12, "X": 26, "Y": 11, "Z": 11, "AA": 12, "AB": 38}, height=60)
    for r in wa.iter_rows(min_row=2):
        for k in (16,):
            r[k].number_format = "0%"
        for k in (17, 18, 19, 20, 21, 22):
            r[k].number_format = "# ##0"
        r[27].hyperlink = r[27].value if str(r[27].value or "").startswith("http") else None
        if r[27].hyperlink:
            r[27].font = Font(name="Arial", size=10, color="0563C1", underline="single")
    for c in wa[wa.max_row]:
        c.font = BOLD
        c.fill = SUB
    wa.freeze_panes = "B2"
    wa.auto_filter.ref = f"A1:AB{wa.max_row - 1}"

    # ============ По месяцам ============
    wm = wb.create_sheet("По месяцам")
    wm.append(["Месяц", "Объявление", "Тип техники", "Показы", "Просмотры", "Контакты", "Звонков Авито", "Заявок CRM", "Сделок", "Расход деньгами, ₽",
               "Цена звонка, ₽", "Цена заявки, ₽"])
    calls_m = collections.defaultdict(list)
    for c in calls:
        if c["item_id"]:
            calls_m[(c["call_time"][:7], c["item_id"])].append(c)
    months = sorted({m for m in stats} | {k[0] for k in calls_m})
    month_tot = collections.defaultdict(lambda: collections.Counter())
    for m in reversed(months):
        ids = {int(i) for i in stats.get(m, {})} | {k[1] for k in calls_m if k[0] == m}
        rows_m = []
        for iid in ids:
            st = stats.get(m, {}).get(str(iid), {})
            cs = calls_m.get((m, iid), [])
            ords = {c["_order"]["id"]: c["_order"] for c in cs if c["_order"]}
            spend = (st.get("spending", 0) or 0) / 100.0
            bonus = max((st.get("allSpending", 0) or 0) / 100.0 - spend, 0.0)
            if not (cs or spend or st.get("views")):
                continue
            rows_m.append((len(cs), spend, iid, st, cs, ords))
        for n_calls, spend, iid, st, cs, ords in sorted(rows_m, key=lambda x: (-x[0], -x[1])):
            title = card(iid)[0]
            won = sum(1 for o in ords.values() if o["_g"] == "Сделка")
            wm.append([m, title, ad_type(agg[iid]), st.get("impressions"), st.get("views"), st.get("contacts"), n_calls, len(ords), won, round(spend, 2),
                       per(spend, n_calls), per(spend, len(ords))])
            for k, v in (("calls", n_calls), ("orders", len(ords)), ("won", won), ("spend", spend), ("bonus", bonus), ("views", st.get("views") or 0), ("contacts", st.get("contacts") or 0)):
                month_tot[m][k] += v
    style(wm, widths={"A": 10, "B": 44, "C": 24, "D": 9, "E": 11, "F": 10, "G": 11, "H": 10, "I": 8, "J": 12, "K": 12, "L": 12}, height=40)
    for r in wm.iter_rows(min_row=2):
        r[10].number_format = r[11].number_format = "# ##0"
    wm.freeze_panes = "C2"
    wm.auto_filter.ref = wm.dimensions

    # ============ Типы техники + рейтинг ============
    wt = wb.create_sheet("Типы техники")
    wt.append(["Тип техники (по заявкам)", "Объявлений", "Просмотры", "Контакты", "Звонков Авито", "Заявок CRM", "Сделок", "Проиграно", "Доля сделок от заявок",
               "Расход деньгами, ₽", "Цена звонка, ₽", "Цена заявки, ₽", "Цена сделки, ₽"])
    types = collections.defaultdict(lambda: dict(ads=0, views=0, contacts=0, calls=0, orders=0, won=0, lost=0, spend=0.0))
    for iid, g in agg.items():
        t = types[ad_type(g)]
        oc = outcome(g)
        t["ads"] += 1
        t["views"] += g["views"]; t["contacts"] += g["contacts"]; t["calls"] += g["calls"]; t["orders"] += len(g["orders"])
        t["won"] += oc["Сделка"]; t["lost"] += oc["Проиграна"]; t["spend"] += g["spend"]
    type_rows = sorted(types.items(), key=lambda kv: (kv[0] == NO_TRANSPORT, -kv[1]["calls"]))
    for name, t in type_rows:
        wt.append([name, t["ads"], t["views"], t["contacts"], t["calls"], t["orders"], t["won"], t["lost"], per(t["won"], t["orders"]), round(t["spend"], 2),
                   per(t["spend"], t["calls"]), per(t["spend"], t["orders"]), per(t["spend"], t["won"])])
    style(wt, widths={"A": 30, "B": 11, "C": 11, "D": 10, "E": 12, "F": 11, "G": 9, "H": 11, "I": 13, "J": 12, "K": 12, "L": 12, "M": 12}, height=44)
    for r in wt.iter_rows(min_row=2):
        r[8].number_format = "0%"
        for k in (9, 10, 11, 12):
            r[k].number_format = "# ##0"
    n_t = sum(1 for name, _ in type_rows if name != NO_TRANSPORT)
    if n_t:
        ch = BarChart()
        ch.type = "bar"
        ch.title = "Рейтинг типов техники по звонкам с Авито"
        ch.height, ch.width = max(8, 0.7 * n_t + 3), 22
        ch.add_data(Reference(wt, min_col=5, max_col=5, min_row=1, max_row=1 + n_t), titles_from_data=True)
        ch.set_categories(Reference(wt, min_col=1, min_row=2, max_row=1 + n_t))
        ch.legend = None
        ch.x_axis.scaling.orientation = "maxMin"
        ch.dataLabels = DataLabelList()
        ch.dataLabels.showVal = True
        wt.add_chart(ch, "O2")
    wt.freeze_panes = "B2"

    # ============ Причины проигрыша ============
    wl = wb.create_sheet("Проигрыши")
    reasons = collections.Counter()
    per_ad_loss = collections.defaultdict(collections.Counter)
    for iid, g in agg.items():
        for o in g["orders"].values():
            if o["_g"] == "Проиграна":
                r_ = o["_loss"] or "Причина не указана"
                reasons[r_] += 1
                per_ad_loss[iid][r_] += 1
    top_reasons = [r for r, _ in reasons.most_common(8)]
    wl.append(["Объявление", "Проиграно всего"] + top_reasons + ["Прочие причины"])
    for iid, cnt in sorted(per_ad_loss.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(cnt.values())
        wl.append([card(iid)[0], tot] + [cnt[r] or None for r in top_reasons] + [tot - sum(cnt[r] for r in top_reasons) or None])
    wl.append(["ВСЕГО", sum(reasons.values())] + [reasons[r] for r in top_reasons] + [sum(reasons.values()) - sum(reasons[r] for r in top_reasons)])
    style(wl, widths={"A": 44, "B": 11}, height=44)
    for col in range(3, 3 + len(top_reasons) + 1):
        wl.column_dimensions[wl.cell(1, col).column_letter].width = 14
    for c in wl[wl.max_row]:
        c.font = BOLD
        c.fill = SUB
    wl.freeze_panes = "B2"

    # ============ Звонки ============
    wc = wb.create_sheet("Звонки Авито")
    wc.append(["Дата и время", "Телефон клиента", "Объявление", "Тип техники", "Длительность, с", "Заявка CRM", "Этап", "Причина проигрыша", "Ссылка на заявку"])
    for c in sorted(calls, key=lambda c: c["_t"], reverse=True):
        o = c["_order"]
        wc.append([c["_t"], "+7" + c["_p"] if c["_p"] else "", card(c["item_id"])[0] if c["item_id"] else "объявление не определено",
                   (o["_type"] or None) if o else None, c["talk_sec"], f"№{o.get('number') or o['id']}" if o else "заявки не найдено",
                   (o.get("stage") or None) if o else None, (o["_loss"] or None) if o and o["_g"] == "Проиграна" else None, ORDER_URL + str(o["id"]) if o else None])
    style(wc, widths={"A": 17, "B": 16, "C": 44, "D": 24, "E": 12, "F": 16, "G": 22, "H": 24, "I": 40}, height=34)
    for r in wc.iter_rows(min_row=2):
        r[0].number_format = "DD.MM.YYYY HH:MM"
        if r[8].value:
            r[8].hyperlink = r[8].value
            r[8].font = Font(name="Arial", size=10, color="0563C1", underline="single")
    wc.freeze_panes = "A2"
    wc.auto_filter.ref = wc.dimensions

    # ============ Чаты (по месяцам) ============
    wch = wb.create_sheet("Чаты")
    wch.append(["Месяц", "Чатов (клиент написал)", "Менеджер ответил", "Без ответа", "% без ответа", "Медиана первого ответа, мин",
                "Ответили за 15 мин, %", "Клиент написал телефон", "Из них есть заявка в CRM"])
    by_m = collections.defaultdict(list)
    for r in chat_rows:
        by_m[r["first_dt"].strftime("%Y-%m")].append(r)
    for m in sorted(by_m):
        rr = by_m[m]
        resp = [r["resp"] for r in rr if r["resp"] is not None]
        wch.append([m, len(rr), sum(1 for r in rr if r["answered"]), sum(1 for r in rr if not r["answered"]),
                    per(sum(1 for r in rr if not r["answered"]), len(rr)), round(statistics.median(resp) / 60) if resp else None,
                    per(sum(1 for x in resp if x <= 900), len(resp)), sum(1 for r in rr if r["phones"]),
                    sum(1 for r in rr if r["phones"] and r["order"])])
    allr = chat_rows
    resp_all = [r["resp"] for r in allr if r["resp"] is not None]
    wch.append(["ВСЕГО", len(allr), sum(1 for r in allr if r["answered"]), sum(1 for r in allr if not r["answered"]),
                per(sum(1 for r in allr if not r["answered"]), len(allr)), round(statistics.median(resp_all) / 60) if resp_all else None,
                per(sum(1 for x in resp_all if x <= 900), len(resp_all)), sum(1 for r in allr if r["phones"]),
                sum(1 for r in allr if r["phones"] and r["order"])])
    style(wch, widths={"A": 10, "B": 14, "C": 14, "D": 12, "E": 12, "F": 16, "G": 14, "H": 14, "I": 16}, height=48)
    for r in wch.iter_rows(min_row=2):
        r[4].number_format = r[6].number_format = "0%"
    for c in wch[wch.max_row]:
        c.font = BOLD
        c.fill = SUB
    wch.freeze_panes = "A2"
    # чаты без ответа за последние 90 дней — для разбора
    wun = wb.create_sheet("Чаты без ответа")
    wun.append(["Дата первого сообщения", "Объявление", "Клиент (имя в Авито)", "Сообщений клиента", "Первое сообщение клиента", "Телефон в тексте",
                "Заявка в CRM (по телефону)", "Ссылка на объявление"])
    cutoff = datetime.now(MSK).replace(tzinfo=None) - timedelta(days=90)
    for r in sorted([r for r in chat_rows if not r["answered"] and r["first_dt"] >= cutoff], key=lambda r: r["first_dt"], reverse=True):
        ch = r["ch"]
        o = r["order"]
        wun.append([r["first_dt"], ch.get("item_title") or card(ch["item_id"])[0] if ch.get("item_id") else "", ch.get("client_name") or "",
                    r["n_in"], (r["first"]["text"] or "")[:300] or f"({r['first']['type']})", ("+7" + sorted(r["phones"])[0]) if r["phones"] else None,
                    (f"№{o.get('number') or o['id']} ({o.get('stage') or ''})") if o else None, ch.get("item_url") or None])
    style(wun, widths={"A": 17, "B": 44, "C": 20, "D": 11, "E": 70, "F": 16, "G": 30, "H": 40}, height=40)
    for r in wun.iter_rows(min_row=2):
        r[0].number_format = "DD.MM.YYYY HH:MM"
        if r[7].value:
            r[7].hyperlink = r[7].value
            r[7].font = Font(name="Arial", size=10, color="0563C1", underline="single")
    wun.freeze_panes = "A2"
    wun.auto_filter.ref = wun.dimensions

    # ============ Расходы профиля ============
    we = wb.create_sheet("Расходы профиля")
    we.append(["Месяц", "Размещение (тариф), ₽", "Продвижение, ₽", "Комиссия, ₽", "Прочее, ₽", "Всего, ₽", "Просмотры (по объявлениям)", "Контактов", "Звонков Авито", "Заявок CRM", "По объявлениям: деньги, ₽", "По объявлениям: бонусы, ₽"])
    spend_rows = {}
    for g in (spend_acc.get("result") or {}).get("groupings") or []:
        d = {sp["slug"]: sp["value"] for sp in g.get("spendings") or []}
        spend_rows[g["date"][:7]] = d
    for m in sorted(set(spend_rows) | set(month_tot)):
        d = spend_rows.get(m, {})
        tot = d.get("all") if d.get("all") is not None else sum(d.get(k, 0) or 0 for k in ("presence", "promotion", "commission", "rest"))
        we.append([m, d.get("presence"), d.get("promotion"), d.get("commission"), d.get("rest"), tot or None,
                   month_tot[m]["views"] or None, month_tot[m]["contacts"] or None, month_tot[m]["calls"] or None, month_tot[m]["orders"] or None,
                   round(month_tot[m]["spend"]) or None, round(month_tot[m]["bonus"]) or None])
    style(we, widths={"A": 10, "B": 16, "C": 14, "D": 12, "E": 11, "F": 12, "G": 16, "H": 11, "I": 12, "J": 11, "K": 16, "L": 16}, height=44)
    for r in we.iter_rows(min_row=2):
        for k in (1, 2, 3, 4, 5, 10, 11):
            r[k].number_format = "# ##0"
    if we.max_row > 2:
        ch2 = BarChart()
        ch2.type = "col"
        ch2.title = "Расходы на Авито и звонки по месяцам"
        ch2.height, ch2.width = 9, 22
        ch2.add_data(Reference(we, min_col=6, max_col=6, min_row=1, max_row=we.max_row), titles_from_data=True)
        ch2.set_categories(Reference(we, min_col=1, min_row=2, max_row=we.max_row))
        we.add_chart(ch2, "N2")

    # ============ Сводка (первым листом) ============
    ws = wb.create_sheet("Сводка", 0)
    tot_spend = sum(g["spend"] for g in agg.values())
    tot_bonus = sum(g["bonus"] for g in agg.values())
    n_calls, n_talked = len(calls), sum(1 for c in calls if c["talk_sec"] > 0)
    all_orders = {}
    for g in agg.values():
        all_orders.update(g["orders"])
    won = sum(1 for o in all_orders.values() if o["_g"] == "Сделка")
    lost = sum(1 for o in all_orders.values() if o["_g"] == "Проиграна")
    ws.append(["Показатель", "Значение", "Пояснение"])
    summary = [
        ("Период", f"с {datetime.fromisoformat(a.since):%d.%m.%Y} по {datetime.now(MSK):%d.%m.%Y}", "звонки по времени звонка (МСК), расходы и просмотры по месяцам"),
        ("Профиль Авито", "СК Техноресурс", "одна из учётных записей; остальные линии (Автолайн, Москва, HR) — отдельные профили, в отчёт не входят"),
        ("Объявлений со звонками или расходами", len(agg), ""),
        ("Расход на Авито деньгами, ₽", round(tot_spend), "размещение и продвижение по объявлениям; совпадает с методом расходов профиля Авито"),
        ("Оплачено бонусами Авито, ₽", round(tot_bonus), "бонусы списываются сверх денег (в основном апрель–июнь); в цену не входят — см. цену сделки с бонусами ниже"),
        ("Просмотры", sum(g["views"] for g in agg.values()), "из них контактов: " + f"{sum(g['contacts'] for g in agg.values()):,}".replace(",", " ")),
        ("Звонков с Авито", n_calls, f"с разговором {n_talked}"),
        ("Заявок CRM (по звонкам и по чатам)", len(all_orders), "по звонкам: тот же телефон, заявка от −2 до +30 мин; по чатам: клиент сам написал телефон, заявка от −1 часа до +7 суток"),
        ("из них найдено только по чатам", sum(g["chat_orders"] for g in agg.values()), "заявки, у которых нет звонка с Авито, но телефон совпал с телефоном из переписки"),
        ("Чатов с сообщением клиента", len(chat_rows), f"менеджер не ответил в {per(sum(1 for r in chat_rows if not r['answered']), len(chat_rows)):.0%} чатов; медиана первого ответа {round(statistics.median([r['resp'] for r in chat_rows if r['resp'] is not None]) / 60) if any(r['resp'] is not None for r in chat_rows) else 0} мин"),
        ("Звонков, для которых заявка нашлась", sum(1 for c in calls if c["_order"]), f"{per(sum(1 for c in calls if c['_order']), n_calls):.0%} звонков"),
        ("Сделок", won, f"проиграно {lost}"),
        ("Доля сделок от заявок", per(won, len(all_orders)), ""),
        ("Цена звонка, ₽", round(per(tot_spend, n_calls) or 0), "расход деньгами / звонков Авито"),
        ("Цена заявки, ₽", round(per(tot_spend, len(all_orders)) or 0), "расход деньгами / заявок по звонкам"),
        ("Цена сделки, ₽", round(per(tot_spend, won) or 0), "расход деньгами / сделок: самая важная цифра"),
        ("Цена сделки с учётом бонусов, ₽", round(per(tot_spend + tot_bonus, won) or 0), "(деньги + бонусы) / сделок"),
        ("Что учтено неполностью", "чаты без телефона", "заявка из чата находится, только если клиент сам написал телефон (около 11% чатов); остальные обращения в чате в заявки не попали, а расходы на них в цену входят"),
    ]
    for r in summary:
        ws.append(list(r))
    style(ws, widths={"A": 44, "B": 24, "C": 100}, height=30)
    for r in ws.iter_rows(min_row=2):
        if isinstance(r[1].value, float) and r[1].value < 1:
            r[1].number_format = "0.0%"
        elif isinstance(r[1].value, int):
            r[1].number_format = "# ##0"
    wb.save(a.out)
    print(f"{a.out}: объявлений {len(agg)}, звонков {n_calls}, заявок {len(all_orders)}, сделок {won}, расход {tot_spend:,.0f} ₽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
