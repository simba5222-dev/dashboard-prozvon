#!/usr/bin/env python
"""Отчёт по рекламным линиям: звонки в АТС против заявок в CRM (xlsx).

    /home/claude/dashboard/.venv/bin/python -I scripts/ad_report_build.py \
        --orders data/ad_report/orders.jsonl --calls /home/agent/ad-report/history_in.jsonl \
        --since 2026-01-01 --out /home/agent/ad-report/report.xlsx

Звонки — входящие из истории ВАТС (выгрузка с офиса), заявки — из Synergy (ad_report_fetch_orders.py).
Рекламная линия — запись `lines.kind = 'рекламная'` (справочник дашборда). Заявка считается рекламной,
если в её карточке (custom-278) стоит номер такой линии. «Дозвонились» — разговор длиннее нуля секунд
(статусу ВАТС не верим, см. skill /реклама).
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

MSK = timezone(timedelta(hours=3))
DB = "/home/claude/dashboard/data/dashboard.db"
ORDER_URL = "https://app.synergycrm.ru/orders/"
NO_TRANSPORT = "Не указан"
HEAD_FILL = PatternFill("solid", fgColor="2F4B6E")
THIN = Side(style="thin", color="BBBBBB")
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
GROUPS = ("Новый", "В работе", "Сделка", "Проиграна", "Пропущенный")


def digits(v) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def stage_group(name: str, kind: str) -> str:
    if kind == "won" or name == "Сделка":
        return "Сделка"
    if kind == "lost" or "провалена" in name.lower():
        return "Проиграна"
    if name == "Пропущенный":
        return "Пропущенный"
    if kind == "opened" or name == "Новый":
        return "Новый"
    return "В работе"


def transport_of(o: dict) -> str:
    for key in ("transport_rel", "transport_field"):
        v = str(o.get(key) or "").strip()
        if v and v not in ("-", "—"):
            return v
    return NO_TRANSPORT


def style_sheet(ws, header_row: int = 1, widths: dict | None = None) -> None:
    for c in ws[header_row]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")
        c.border = BORDER
    for row in ws.iter_rows(min_row=header_row + 1):
        for c in row:
            c.font = Font(name="Arial", size=10)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            c.border = BORDER
    for col, w in (widths or {}).items():
        ws.column_dimensions[col].width = w
    ws.row_dimensions[header_row].height = 34


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", required=True)
    ap.add_argument("--calls", required=True)
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    since = a.since
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    lines = {r[0]: {"name": r[1], "scope": bool(r[2])} for r in db.execute(
        "SELECT phone10, name, in_scope FROM lines WHERE kind = 'рекламная'")}

    # ---- звонки ----
    calls_by_line: dict[str, collections.Counter] = {p: collections.Counter() for p in lines}
    calls_by_month: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    calls_lm: dict[tuple, collections.Counter] = collections.defaultdict(collections.Counter)
    call_rows: list[dict] = []
    for raw in open(a.calls, encoding="utf-8"):
        c = json.loads(raw)
        p = digits(c.get("diversion"))[-10:]
        if p not in lines:
            continue
        day = datetime.fromisoformat(str(c["start"]).replace("Z", "+00:00")).astimezone(MSK)
        if day.date().isoformat() < since:
            continue
        talked = int(c.get("duration") or 0) > 0
        call_rows.append({"uid": c.get("uid"), "p": p, "c": digits(c.get("client"))[-10:], "t": day.replace(tzinfo=None),
                          "status": str(c.get("status")), "dur": int(c.get("duration") or 0)})
        lm = calls_lm[(day.strftime("%Y-%m"), p)]
        lm["all"] += 1
        lm["success"] += str(c.get("status")) == "success"
        lm["missed"] += str(c.get("status")) == "missed"
        lm["talked"] += talked
        for bucket in (calls_by_line[p], calls_by_month[day.strftime("%Y-%m")]):
            bucket["all"] += 1
            bucket["talked"] += talked
        calls_by_month[day.strftime("%Y-%m")]["all_lines"] += 0

    # ---- заявки ----
    orders = []
    no_line_ad = 0
    for raw in open(a.orders, encoding="utf-8"):
        o = json.loads(raw)
        created = datetime.fromisoformat(str(o["created_at"]).replace("Z", "+00:00")).astimezone(MSK)
        if created.date().isoformat() < since:
            continue
        p = digits(o.get("line"))[-10:]
        if p not in lines:
            if "Реклама" in str(o.get("mark") or "") or "Реклама" in str(o.get("source_mark") or ""):
                no_line_ad += 1
            continue
        o["_p"], o["_created"] = p, created.replace(tzinfo=None)
        o["_transport"] = transport_of(o)
        o["_group"] = stage_group(str(o.get("stage") or ""), str(o.get("stage_kind") or ""))
        loss = str(o.get("loss_rel") or o.get("loss_field") or "").strip()
        o["_loss"] = "" if loss in ("-", "—") else loss
        orders.append(o)
    # ---- сопоставление заявок со звонками ВАТС: тот же телефон клиента на той же линии, заявка создана
    # от 2 минут до начала звонка до 10 минут после него; на один звонок — одна «своя» заявка ----
    call_idx: dict[tuple, list] = collections.defaultdict(list)
    for cr in call_rows:
        call_idx[(cr["p"], cr["c"])].append(cr)
    taken: collections.Counter = collections.Counter()
    for o in sorted(orders, key=lambda o: o["_created"]):
        phone = digits(o.get("client_phone") or o.get("contact_phone"))[-10:]
        best = None
        for cr in call_idx.get((o["_p"], phone), []) if phone else []:
            d = (o["_created"] - cr["t"]).total_seconds()
            if -120 <= d <= 600 and (best is None or abs(d) < abs(best[0])):
                best = (d, cr)
        o["_call_t"], o["_call_res"] = None, ""
        if not phone:
            o["_match"] = "сверить нельзя: нет телефона клиента"
        elif best is None:
            o["_match"] = "звонка нет: в ВАТС нет такого звонка"
        else:
            cr = best[1]
            taken[cr["uid"]] += 1
            o["_match"] = "звонок найден" if taken[cr["uid"]] == 1 else "ещё одна заявка на тот же звонок"
            o["_call_t"] = cr["t"]
            o["_call_res"] = "принят" if cr["status"] == "success" else "пропущен"
    nocall_lm: collections.Counter = collections.Counter()   # заявок без звонка по (месяц, линия)
    nophone_lm: collections.Counter = collections.Counter()  # заявок, которые не проверить (нет телефона)
    tot_lm: collections.Counter = collections.Counter()
    for o in orders:
        k = (o["_created"].strftime("%Y-%m"), o["_p"])
        tot_lm[k] += 1
        if o["_match"].startswith("звонка нет"):
            nocall_lm[k] += 1
        elif o["_match"].startswith("сверить нельзя"):
            nophone_lm[k] += 1
    order_lines = {o["_p"] for o in orders}
    # «звонок без заявки» достоверен, только если у заявок этого месяца и линии телефон известен почти везде
    reliable = lambda m, p: tot_lm[(m, p)] > 0 and nophone_lm[(m, p)] / tot_lm[(m, p)] < 0.1  # noqa: E731
    calls_noorder = [cr for cr in call_rows if cr["uid"] not in taken and cr["p"] in order_lines
                     and reliable(cr["t"].strftime("%Y-%m"), cr["p"])]
    noorder_lm: collections.Counter = collections.Counter((cr["t"].strftime("%Y-%m"), cr["p"]) for cr in calls_noorder)
    orders.sort(key=lambda o: o["_created"], reverse=True)

    by_line: dict[str, list] = collections.defaultdict(list)
    by_month: dict[str, list] = collections.defaultdict(list)
    by_type: dict[str, list] = collections.defaultdict(list)
    for o in orders:
        by_line[o["_p"]].append(o)
        by_month[o["_created"].strftime("%Y-%m")].append(o)
        by_type[o["_transport"]].append(o)

    all_calls = sum(c["all"] for c in calls_by_line.values())
    all_talked = sum(c["talked"] for c in calls_by_line.values())
    with_type = sum(1 for o in orders if o["_transport"] != NO_TRANSPORT)
    pct = lambda x, y: (x / y) if y else None  # noqa: E731

    wb = Workbook()
    # ================= Сводка =================
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["Показатель", "Значение", "Пояснение"])
    rows = [
        ("Период", f"с {datetime.fromisoformat(since):%d.%m.%Y} по {datetime.now(MSK):%d.%m.%Y}", "заявки по дате создания, звонки по дате начала (время московское)"),
        ("Рекламных линий в справочнике", len(lines), "15 номеров из ВАТС; часть исключена из разбора решениями владельца (колонка «В разборе» на листе «По линиям»)"),
        ("Входящих звонков на рекламные линии (АТС)", all_calls, "все входящие, включая недозвоны"),
        ("из них дозвонились (разговор > 0 с)", all_talked, ""),
        ("Заявок в CRM с рекламной линией в карточке", len(orders), "номер линии (custom-278) совпал с рекламной линией"),
        ("Заявок на один звонок", pct(len(orders), all_calls), "заявок / всех входящих звонков"),
        ("Заявок на один дозвонившийся звонок", pct(len(orders), all_talked), "может быть больше 100%: Synergy заводит карточку и на недозвон, и повторные звонки одного клиента"),
        ("Заявок с указанным типом транспорта", with_type, ""),
        ("Заявок без типа транспорта", len(orders) - with_type, "служебные карточки и звонки без запроса на технику"),
        ("Доля заявок с типом транспорта", pct(with_type, len(orders)), ""),
        ("Ещё заявок с меткой «Реклама», но без рекламной линии в карточке", no_line_ad, "в отчёт не входят; смотреть, если нужна полная картина по метке"),
    ]
    for r in rows:
        ws.append(list(r))
    ws.append([])
    for g in GROUPS:
        n = sum(1 for o in orders if o["_group"] == g)
        ws.append([f"На этапе «{g}»", n, f"{pct(n, len(orders)) or 0:.1%} от заявок"])
    ws.append([])
    mcount = collections.Counter(o["_match"].split(":")[0] for o in orders)
    for label, key, note in (("Заявок, для которых найден звонок в ВАТС", "звонок найден", "тот же телефон клиента на той же линии, заявка создана от −2 до +10 минут от начала звонка"),
                             ("Заявок без звонка в ВАТС (расходятся)", "звонка нет", "телефон клиента известен, но такого звонка на линии нет: заявка не по звонку (форма, повторная карточка)"),
                             ("Ещё заявок на тот же звонок", "ещё одна заявка на тот же звонок", "вторая и следующие карточки на один звонок"),
                             ("Заявок, которые не проверить", "сверить нельзя", "в карточке и в контакте нет телефона клиента: сверять нечем")):
        ws.append([label, mcount[key], note])
    style_sheet(ws, widths={"A": 58, "B": 26, "C": 88})
    for r in ws.iter_rows(min_row=2):
        if isinstance(r[1].value, float) and r[1].value < 50:
            r[1].number_format = "0.0%"
        elif isinstance(r[1].value, int):
            r[1].number_format = "# ##0"
    # помесячно
    start_row = ws.max_row + 3
    ws.cell(start_row - 1, 1, "Звонки в АТС и заявки в CRM по месяцам").font = Font(name="Arial", bold=True, size=12)
    head = ["Месяц", "Входящих на рекламные линии", "Дозвонились", "Заявок в CRM", "Из них с типом транспорта", "Заявок на 100 звонков"]
    for i, h in enumerate(head, 1):
        c = ws.cell(start_row, i, h)
        c.font = Font(name="Arial", bold=True, color="FFFFFF"); c.fill = HEAD_FILL; c.border = BORDER
        c.alignment = Alignment(wrap_text=True, vertical="center")
    months = sorted(set(calls_by_month) | set(by_month))
    for k, m in enumerate(months, 1):
        cm = calls_by_month.get(m, collections.Counter())
        om = by_month.get(m, [])
        vals = [m, cm["all"], cm["talked"], len(om), sum(1 for o in om if o["_transport"] != NO_TRANSPORT),
                round(100 * len(om) / cm["all"], 1) if cm["all"] else None]
        for i, v in enumerate(vals, 1):
            c = ws.cell(start_row + k, i, v); c.font = Font(name="Arial", size=10); c.border = BORDER
    ch = BarChart(); ch.type = "col"; ch.title = "Звонки в АТС и заявки в CRM по месяцам"
    ch.y_axis.title = "Количество"; ch.height = 9; ch.width = 20
    ch.add_data(Reference(ws, min_col=2, max_col=2, min_row=start_row, max_row=start_row + len(months)), titles_from_data=True)
    ch.add_data(Reference(ws, min_col=4, max_col=4, min_row=start_row, max_row=start_row + len(months)), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=start_row + 1, max_row=start_row + len(months)))
    ws.add_chart(ch, f"H{start_row - 1}")

    # ================= По линиям =================
    wl = wb.create_sheet("По линиям")
    wl.append(["Линия", "Номер", "В разборе", "Входящих звонков", "Дозвонились", "Заявок в CRM", "Заявок на 100 звонков",
               "С типом транспорта", "Новый", "В работе", "Сделка", "Проиграна", "Пропущенный"])
    for p in sorted(lines, key=lambda p: -calls_by_line[p]["all"]):
        os_ = by_line.get(p, [])
        cnt = collections.Counter(o["_group"] for o in os_)
        wl.append([lines[p]["name"], p, "да" if lines[p]["scope"] else "нет", calls_by_line[p]["all"], calls_by_line[p]["talked"],
                   len(os_), round(100 * len(os_) / calls_by_line[p]["all"], 1) if calls_by_line[p]["all"] else None,
                   sum(1 for o in os_ if o["_transport"] != NO_TRANSPORT)] + [cnt[g] for g in GROUPS])
    tot = ["ИТОГО", "", "", all_calls, all_talked, len(orders), round(100 * len(orders) / all_calls, 1) if all_calls else None, with_type] + \
          [sum(1 for o in orders if o["_group"] == g) for g in GROUPS]
    wl.append(tot)
    style_sheet(wl, widths={"A": 24, "B": 13, "C": 10, "D": 13, "E": 13, "F": 12, "G": 13, "H": 13, "I": 9, "J": 9, "K": 9, "L": 10, "M": 12})
    for c in wl[wl.max_row]:
        c.font = Font(name="Arial", size=10, bold=True)
    wl.freeze_panes = "A2"

    # ================= Заявки =================
    wo = wb.create_sheet("Заявки")
    wo.append(["Заявка", "Дата создания", "Линия", "Номер линии", "Телефон клиента", "Тип транспорта", "Этап", "Статус",
               "Причина проигрыша", "Сумма, ₽", "Метка", "Сверка со звонком в ВАТС", "Время звонка", "Звонок", "Ссылка"])
    for o in orders:
        wo.append([f"№{o.get('number') or o['id']}", o["_created"], lines[o["_p"]]["name"], o["_p"], o.get("client_phone") or "",
                   o["_transport"], o.get("stage") or "", o["_group"], o["_loss"] if o["_group"] == "Проиграна" else (o["_loss"] or ""),
                   o.get("amount") or None, o.get("mark") or "", o["_match"], o["_call_t"], o["_call_res"], ORDER_URL + str(o["id"])])
    style_sheet(wo, widths={"A": 12, "B": 17, "C": 20, "D": 13, "E": 16, "F": 26, "G": 24, "H": 13, "I": 26, "J": 12, "K": 14, "L": 36, "M": 17, "N": 10, "O": 42})
    FILL_UNCHECK = PatternFill("solid", fgColor="D9D9D9")  # проверить нельзя (нет телефона)
    FILL_NOCALL = PatternFill("solid", fgColor="FFE699")   # звонка нет
    FILL_DOUBLE = PatternFill("solid", fgColor="F8CBAD")   # вторая заявка на один звонок
    for r in wo.iter_rows(min_row=2):
        r[1].number_format = "DD.MM.YYYY HH:MM"
        r[12].number_format = "DD.MM.YYYY HH:MM"
        r[9].number_format = "# ##0"
        r[14].hyperlink = r[14].value
        r[14].font = Font(name="Arial", size=10, color="0563C1", underline="single")
        if r[11].value != "звонок найден":
            fill = (FILL_DOUBLE if r[11].value.startswith("ещё одна") else
                    FILL_UNCHECK if r[11].value.startswith("сверить нельзя") else FILL_NOCALL)
            for c in r[:14]:
                c.fill = fill
    wo.freeze_panes = "B2"
    wo.auto_filter.ref = wo.dimensions

    # ================= Звонки без заявки =================
    wc = wb.create_sheet("Звонки без заявки")
    wc.append(["Дата и время звонка", "Линия", "Номер линии", "Телефон клиента", "Результат", "Длительность, с"])
    for cr in sorted(calls_noorder, key=lambda c: c["t"], reverse=True):
        wc.append([cr["t"], lines[cr["p"]]["name"], cr["p"], cr["c"], "принят" if cr["status"] == "success" else "пропущен", cr["dur"] or None])
    style_sheet(wc, widths={"A": 19, "B": 22, "C": 13, "D": 16, "E": 12, "F": 14})
    for r in wc.iter_rows(min_row=2):
        r[0].number_format = "DD.MM.YYYY HH:MM"
    wc.freeze_panes = "A2"
    wc.auto_filter.ref = wc.dimensions

    # ================= Типы транспорта: выбор периода и дат, формулы =================
    from datetime import date as _date
    from openpyxl.worksheet.datavalidation import DataValidation
    types = sorted((t for t in by_type if t != NO_TRANSPORT), key=lambda t: (-len(by_type[t]), t))
    n_all = len(orders)
    last_order_row = len(orders) + 1
    D = lambda col: f"'Заявки'!${col}$2:${col}${last_order_row}"  # noqa: E731  диапазон столбца листа «Заявки»

    # справочник периодов на служебном листе «Списки»
    wlist = wb.create_sheet("Списки")
    wlist.append(["Период", "С", "По"])
    month_names = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    y = datetime.fromisoformat(since).year
    periods = [("Весь период", _date(y, 1, 1), _date(y, 12, 31))]
    for i, mn in enumerate(month_names, 1):
        last = _date(y, i + 1, 1) - timedelta(days=1) if i < 12 else _date(y, 12, 31)
        periods.append((f"{mn} {y}", _date(y, i, 1), last))
    for q in range(4):
        periods.append((f"{q + 1} квартал {y}", _date(y, 3 * q + 1, 1), (_date(y, 3 * q + 4, 1) - timedelta(days=1)) if q < 3 else _date(y, 12, 31)))
    periods += [("Последние 7 дней", "=TODAY()-6", "=TODAY()"), ("Последние 30 дней", "=TODAY()-29", "=TODAY()"),
                ("Текущий месяц", "=DATE(YEAR(TODAY()),MONTH(TODAY()),1)", "=EOMONTH(TODAY(),0)"),
                ("Прошлый месяц", "=EOMONTH(TODAY(),-2)+1", "=EOMONTH(TODAY(),-1)"),
                ("Свой период (даты ниже)", None, None)]
    for row in periods:
        wlist.append(list(row))
    for r in wlist.iter_rows(min_row=2):
        r[1].number_format = r[2].number_format = "DD.MM.YYYY"
    style_sheet(wlist, widths={"A": 28, "B": 13, "C": 13})
    n_per = len(periods) + 1

    wt = wb.create_sheet("Типы транспорта")
    INPUT = PatternFill("solid", fgColor="FFF2CC")
    wt["A1"], wt["B1"] = "Период", "Весь период"
    wt["A2"], wt["B2"] = "Свои даты: с", _date(y, 1, 1)
    wt["A3"], wt["B3"] = "Свои даты: по", datetime.now(MSK).date()
    wt["A4"], wt["B4"] = "Действует с", f'=IF($B$1="Свой период (даты ниже)",$B$2,INDEX(Списки!$B$2:$B${n_per},MATCH($B$1,Списки!$A$2:$A${n_per},0)))'
    wt["A5"], wt["B5"] = "Действует по", f'=IF($B$1="Свой период (даты ниже)",$B$3,INDEX(Списки!$C$2:$C${n_per},MATCH($B$1,Списки!$A$2:$A${n_per},0)))'
    wt["A6"], wt["B6"] = "Заявок в периоде", f'=COUNTIFS({D("B")},">="&$B$4,{D("B")},"<"&($B$5+1))'
    wt["C1"] = "← выберите период из списка; для произвольных дат выберите «Свой период» и впишите даты в жёлтые ячейки"
    for ref in ("B1", "B2", "B3"):
        wt[ref].fill = INPUT
    for ref in ("B2", "B3", "B4", "B5"):
        wt[ref].number_format = "DD.MM.YYYY"
    wt["B6"].number_format = "# ##0"
    for r in range(1, 7):
        wt.cell(r, 1).font = Font(name="Arial", size=10, bold=True)
        wt.cell(r, 2).font = Font(name="Arial", size=10, bold=(r in (1, 6)))
        wt.cell(r, 2).alignment = Alignment(horizontal="center")
        wt.cell(r, 2).border = BORDER
    wt["C1"].font = Font(name="Arial", size=9, italic=True, color="7F7F7F")
    dv = DataValidation(type="list", formula1=f"=Списки!$A$2:$A${n_per}", allow_blank=False, showDropDown=False)
    dv.error, dv.errorTitle = "Выберите период из списка", "Неверный период"
    wt.add_data_validation(dv)
    dv.add("B1")
    dvd = DataValidation(type="date", operator="greaterThan", formula1="36526", allow_blank=False)
    dvd.error, dvd.errorTitle = "Введите дату, например 01.09.2026", "Неверная дата"
    wt.add_data_validation(dvd)
    dvd.add("B2"); dvd.add("B3")

    HR = 8  # строка шапки таблицы
    wt.cell(HR, 1, "Тип транспорта")
    for j, h in enumerate(["Заявок", "Доля", *GROUPS, "Сделок от завершённых", "служебное"], 2):
        wt.cell(HR, j, h)
    type_order = types + [NO_TRANSPORT]
    first, last = HR + 1, HR + len(type_order)
    def cnt(col_type, extra=""):
        return f'COUNTIFS({D("F")},{col_type},{D("B")},">="&$B$4,{D("B")},"<"&($B$5+1){extra})'
    for k, t in enumerate(type_order):
        r = first + k
        wt.cell(r, 1, t)
        wt.cell(r, 2, "=" + cnt(f"$A{r}"))
        wt.cell(r, 3, f'=IF($B$6=0,"",B{r}/$B$6)')
        for j, g in enumerate(GROUPS, 4):
            wt.cell(r, j, "=" + cnt(f"$A{r}", f',{D("H")},{wt.cell(HR, j).coordinate.replace(str(HR), "$" + str(HR))}'))
        wt.cell(r, 9, f'=IFERROR(F{r}/(F{r}+G{r}),"")')
        wt.cell(r, 10, f'=IF(A{r}="{NO_TRANSPORT}",-1,B{r}-ROW()*0.000001)')
    tot = last + 1
    wt.cell(tot, 1, "Итого")
    for j in range(2, 9):
        if j != 3:
            wt.cell(tot, j, f"=SUM({wt.cell(first, j).coordinate}:{wt.cell(last, j).coordinate})")
    wt.cell(tot, 3, f'=IF($B$6=0,"",B{tot}/$B$6)')
    wt.cell(tot, 9, f'=IFERROR(F{tot}/(F{tot}+G{tot}),"")')

    # рейтинг: первые 15 типов выбранного периода, сортировка формулами
    TOP = 15
    for j, h in enumerate(["Место", "Тип транспорта (рейтинг)", "Заявок"], 12):
        wt.cell(HR, j, h)
    for k in range(1, TOP + 1):
        r = HR + k
        wt.cell(r, 12, k)
        big = f"LARGE($J${first}:$J${last},L{r})"
        wt.cell(r, 13, f'=IF({big}<=0,"",INDEX($A${first}:$A${last},MATCH({big},$J${first}:$J${last},0)))')
        wt.cell(r, 14, f'=IF(M{r}="","",INDEX($B${first}:$B${last},MATCH(M{r},$A${first}:$A${last},0)))')

    for c in wt[HR]:
        if c.value:
            c.font = Font(name="Arial", bold=True, color="FFFFFF"); c.fill = HEAD_FILL; c.border = BORDER
            c.alignment = Alignment(wrap_text=True, vertical="center")
    for r in wt.iter_rows(min_row=first, max_row=tot):
        for c in r[:10]:
            c.font = Font(name="Arial", size=10); c.border = BORDER
        r[2].number_format = "0.0%"; r[8].number_format = "0%"
        r[9].font = Font(name="Arial", size=8, color="BFBFBF")
    for c in wt[tot]:
        c.font = Font(name="Arial", size=10, bold=True)
    for c in wt[last]:
        c.font = Font(name="Arial", size=10, italic=True)
    for r in wt.iter_rows(min_row=HR + 1, max_row=HR + TOP, min_col=12, max_col=14):
        for c in r:
            c.font = Font(name="Arial", size=10); c.border = BORDER
    for col, w in {"A": 30, "B": 14, "C": 9, "D": 9, "E": 10, "F": 9, "G": 11, "H": 13, "I": 15, "J": 9, "K": 3, "L": 8, "M": 30, "N": 10}.items():
        wt.column_dimensions[col].width = w
    wt.column_dimensions["J"].hidden = True
    wt.row_dimensions[HR].height = 34
    wt.freeze_panes = f"A{HR + 1}"

    rating = BarChart(); rating.type = "bar"; rating.title = "Рейтинг типов транспорта по числу заявок (за выбранный период)"
    rating.style = 10; rating.height = 11; rating.width = 22
    rating.add_data(Reference(wt, min_col=14, max_col=14, min_row=HR, max_row=HR + TOP), titles_from_data=True)
    rating.set_categories(Reference(wt, min_col=13, min_row=HR + 1, max_row=HR + TOP))
    rating.y_axis.title = "Заявок"; rating.legend = None
    rating.x_axis.scaling.orientation = "maxMin"   # самый частый тип сверху
    rating.dataLabels = DataLabelList(); rating.dataLabels.showVal = True
    wt.add_chart(rating, f"L{HR + TOP + 3}")
    wb.calculation.fullCalcOnLoad = True

    # ================= По месяцам и линиям (сверка с историей ВАТС) =================
    orders_lm = collections.Counter((o["_created"].strftime("%Y-%m"), o["_p"]) for o in orders)
    wm = wb.create_sheet("По месяцам и линиям")
    wm.append(["Месяц", "Линия", "Номер", "В разборе", "Звонков всего (как «Все» в истории ВАТС)", "Принятых", "Пропущенных",
               "Дозвонились (разговор > 0 с)", "Заявок в CRM", "Заявок на 100 звонков",
               "Заявок без звонка (расходятся)", "Звонков без заявки (если телефон в заявках известен)"])
    all_months = sorted({m for m, _ in calls_lm} | {m for m, _ in orders_lm}, reverse=True)
    for m in all_months:
        month_lines = [p for p in lines if calls_lm[(m, p)]["all"] or orders_lm[(m, p)]]
        month_lines.sort(key=lambda p: -calls_lm[(m, p)]["all"])
        t = collections.Counter()
        for p in month_lines:
            cl = calls_lm[(m, p)]
            wm.append([m, lines[p]["name"], p, "да" if lines[p]["scope"] else "нет", cl["all"], cl["success"], cl["missed"], cl["talked"],
                       orders_lm[(m, p)], round(100 * orders_lm[(m, p)] / cl["all"], 1) if cl["all"] else None,
                       nocall_lm[(m, p)], noorder_lm[(m, p)] if (p in order_lines and reliable(m, p)) else None])
            t.update({"all": cl["all"], "success": cl["success"], "missed": cl["missed"], "talked": cl["talked"], "orders": orders_lm[(m, p)],
                      "nocall": nocall_lm[(m, p)], "noorder": noorder_lm[(m, p)] if (p in order_lines and reliable(m, p)) else 0})
        wm.append([m, "Все линии", "", "", t["all"], t["success"], t["missed"], t["talked"], t["orders"],
                   round(100 * t["orders"] / t["all"], 1) if t["all"] else None, t["nocall"], t["noorder"]])
        for c in wm[wm.max_row]:
            c.font = Font(name="Arial", size=10, bold=True)
            c.fill = PatternFill("solid", fgColor="E8EEF5")
    style_sheet(wm, widths={"A": 10, "B": 24, "C": 13, "D": 10, "E": 20, "F": 11, "G": 13, "H": 16, "I": 12, "J": 14, "K": 17, "L": 14})
    wm.row_dimensions[1].height = 48
    for r in wm.iter_rows(min_row=2):
        if r[1].value == "Все линии":
            for c in r:
                c.font = Font(name="Arial", size=10, bold=True)
                c.fill = PatternFill("solid", fgColor="E8EEF5")
    wm.freeze_panes = "C2"
    wm.auto_filter.ref = wm.dimensions
    wb.move_sheet(wm, offset=-(len(wb.sheetnames) - 1 - wb.sheetnames.index("По линиям") - 1))

    # ================= Транспорт по линиям (число и доля) =================
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.utils import get_column_letter
    line_order = [p for p in sorted(lines, key=lambda p: -len(by_line.get(p, []))) if by_line.get(p)]
    cross = collections.defaultdict(collections.Counter)
    for o in orders:
        cross[o["_transport"]][o["_p"]] += 1
    type_order = types + [NO_TRANSPORT]
    for title, share in (("Транспорт по линиям", False), ("Транспорт по линиям, %", True)):
        wx = wb.create_sheet(title)
        wx.append(["Тип транспорта"] + [lines[p]["name"] for p in line_order] + ["Всего"])
        for t in type_order:
            tot_t = sum(cross[t].values())
            wx.append([t] + [(cross[t][p] / len(by_line[p]) if share else cross[t][p]) if cross[t][p] else None for p in line_order]
                      + [tot_t / n_all if share and n_all else tot_t])
        wx.append(["Всего заявок"] + [(1.0 if share else len(by_line[p])) for p in line_order] + [1.0 if share else n_all])
        style_sheet(wx, widths={"A": 30})
        for i in range(2, len(line_order) + 3):
            wx.column_dimensions[get_column_letter(i)].width = 13
        for r in wx.iter_rows(min_row=2):
            for c in r[1:]:
                c.number_format = "0%" if share else "# ##0"
                c.alignment = Alignment(horizontal="center", vertical="top")
        for c in wx[wx.max_row]:
            c.font = Font(name="Arial", size=10, bold=True)
        for r in wx.iter_rows(min_row=2, max_row=wx.max_row - 1, min_col=len(line_order) + 2, max_col=len(line_order) + 2):
            for c in r:
                c.font = Font(name="Arial", size=10, bold=True)
        # подсветка: чем больше, тем темнее (только внутри таблицы, без итогов)
        area = f"B2:{get_column_letter(len(line_order) + 1)}{wx.max_row - 1}"
        wx.conditional_formatting.add(area, ColorScaleRule(start_type="min", start_color="FFFFFF", end_type="max", end_color="5B9BD5"))
        wx.freeze_panes = "B2"

    # ================= Причины проигрыша =================
    wr = wb.create_sheet("Причины проигрыша")
    wr.append(["Причина проигрыша", "Заявок", "Доля от проигранных"])
    lost = [o for o in orders if o["_group"] == "Проиграна"]
    reasons = collections.Counter(o["_loss"] or "Причина не указана" for o in lost)
    for reason, n in reasons.most_common():
        wr.append([reason, n, n / len(lost)])
    style_sheet(wr, widths={"A": 40, "B": 10, "C": 18})
    for r in wr.iter_rows(min_row=2):
        r[2].number_format = "0.0%"

    wb.move_sheet(wb["Списки"], offset=len(wb.sheetnames) - 1 - wb.sheetnames.index("Списки"))
    wb.save(a.out)
    print(f"{a.out}: звонков {all_calls}, дозвонились {all_talked}, заявок {len(orders)}, с типом {with_type}, "
          f"проигранных {len(lost)}, типов транспорта {len(types)}; без линии с меткой «Реклама»: {no_line_ad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
