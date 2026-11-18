#!/usr/bin/env python
"""Отчёт по менеджерам «фермерам» (Никитин — поиск техники, Ткачевин — тёплый прозвон):
звонки и заполнение CRM. Только чтение из базы дашборда, результат — xlsx.

    /home/claude/dashboard/.venv/bin/python -I scripts/farmers_report_build.py --out /home/agent/farmers-report/report.xlsx

«Дозвонился» — разговор дольше нуля секунд (статусу ВАТС не верим, см. skill /реклама).
Заполнение CRM — по двум источникам: проверка карточки после звонка (`card_checks`: контакт найден,
потребность, объекты, ИНН, задача) и журнал действий менеджера в Synergy (`activities`).
Для Никитина вместо карточки клиента смотрим, занесена ли названная поставщиком техника в базу «Транспорт»
(`search_checks`).
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from datetime import datetime

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

DB = "/home/claude/dashboard/data/dashboard.db"
MANAGERS = [("никитин", "Никитин Сергей Дмитриевич", "31484", "поиск техники"),
            ("ткачевин", "Ткачевин Михаил Эдгарович", "30057", "тёплый прозвон")]
SHORT = {"никитин": "Никитин", "ткачевин": "Ткачевин"}
VERDICT = {"all_saved": "всё занесено", "missing": "не занесено в карточку", "no_cards": "поставщика нет в базе",
           "unclear": "техника не прозвучала", "ask_more": "сказал, что есть ещё — спросить какая"}
ENTITY = {"Contact": "Контакты", "Transport": "Транспорт (карточки исполнителей)", "Order": "Заявки", "Diary": "Задачи",
          "Company": "Компании", "TelephonyCall": "Звонки (привязка в CRM)"}
HEAD = PatternFill("solid", fgColor="2F4B6E")
THIN = Side(style="thin", color="BBBBBB")
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
BOLD = Font(name="Arial", size=10, bold=True)


def style(ws, header_row=1, widths=None, height=34):
    for c in ws[header_row]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = HEAD
        c.alignment = Alignment(wrap_text=True, vertical="center")
        c.border = BORDER
    for row in ws.iter_rows(min_row=header_row + 1):
        for c in row:
            c.font = Font(name="Arial", size=10)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            c.border = BORDER
    for col, w in (widths or {}).items():
        ws.column_dimensions[col].width = w
    ws.row_dimensions[header_row].height = height


def pct(a, b):
    return (a / b) if b else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--history", required=True, help="farmers_history.jsonl — звонки двух менеджеров из истории ВАТС")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    logins = tuple(m[0] for m in MANAGERS)
    mark = ",".join("?" * len(logins))

    # Звонки берём из истории ВАТС: в базе дашборда они с дублями (один звонок под двумя id) и с пропусками
    # (сверка 17.11.2026: у Ткачевина 675 лишних строк и 436 пропущенных звонков с 16.09). Проверки карточки и
    # поиска техники подтягиваем из базы, сопоставляя звонок по номеру клиента и времени (±20 с).
    from datetime import timedelta, timezone
    MSK = timezone(timedelta(hours=3))
    LOGIN = {"belkov_valeriy_nikolaevich": "ткачевин", "s.nikitin": "никитин"}
    dg = lambda v: "".join(ch for ch in str(v or "") if ch.isdigit())[-10:]  # noqa: E731
    dbcalls = [dict(r) for r in db.execute(
        f"""SELECT k.vats_login, k.client_phone, k.started_at,
                   x.contact_found, x.need_filled, x.objects_filled, x.inn_filled, x.task_created,
                   s.asked_type, s.verdict, s.missing
              FROM calls k
              LEFT JOIN card_checks x ON x.call_uid = k.uid
              LEFT JOIN search_checks s ON s.call_uid = k.uid
             WHERE k.vats_login IN ({mark}) AND k.is_demo = 0""", logins)]
    idx = collections.defaultdict(list)
    for c in dbcalls:
        c["_t"] = datetime.fromisoformat(c["started_at"]).astimezone(MSK)
        idx[(c["vats_login"], dg(c["client_phone"]))].append(c)
    calls = []
    n_enriched = 0
    for line in open(a.history, encoding="utf-8"):
        r = json.loads(line)
        lg = LOGIN[r["user"]]
        t = datetime.fromisoformat(r["start"].replace("Z", "+00:00")).astimezone(MSK)
        best = None
        for c in idx.get((lg, dg(r["client"])), []):
            if abs((c["_t"] - t).total_seconds()) <= 20:
                has = c["contact_found"] is not None or c["verdict"] is not None
                if best is None or (has and not (best["contact_found"] is not None or best["verdict"] is not None)):
                    best = c
        extra = {k: (best[k] if best else None) for k in ("contact_found", "need_filled", "objects_filled", "inn_filled",
                                                            "task_created", "asked_type", "verdict", "missing")}
        n_enriched += best is not None and (best["contact_found"] is not None or best["verdict"] is not None)
        calls.append({"uid": r["uid"], "vats_login": lg, "client_phone": "+" + dg(r["client"]) if r["client"] else "",
                      "direction": r["type"], "started_at": t.isoformat(), "local_date": t.date().isoformat(),
                      "local_hour": t.hour, "duration_sec": int(r.get("duration") or 0), **extra})
    calls.sort(key=lambda c: c["started_at"])
    acts = [dict(r) for r in db.execute(
        "SELECT synergy_user, local_date, entity_type, action FROM activities WHERE synergy_user IN (?, ?)",
        tuple(m[2] for m in MANAGERS))]
    user_of = {m[2]: m[0] for m in MANAGERS}
    for r in acts:
        r["login"] = user_of[str(r["synergy_user"])]

    first_day = min(c["local_date"] for c in calls)
    last_day = max(c["local_date"] for c in calls)
    by_mgr = {lg: [c for c in calls if c["vats_login"] == lg] for lg in logins}
    acts_by = {lg: [r for r in acts if r["login"] == lg] for lg in logins}
    months = sorted({c["local_date"][:7] for c in calls})

    wb = Workbook()

    # ============ Сводка ============
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["Показатель"] + [f"{SHORT[m[0]]}\n({m[3]})" for m in MANAGERS] + ["Пояснение"])
    def stats(lg):
        cs = by_mgr[lg]
        out = [c for c in cs if c["direction"] == "out"]
        talked = [c for c in cs if (c["duration_sec"] or 0) > 0]
        talked_out = [c for c in out if (c["duration_sec"] or 0) > 0]
        days = {c["local_date"] for c in cs}
        sec = sum(c["duration_sec"] or 0 for c in cs)
        ac = acts_by[lg]
        ent = collections.Counter(ENTITY.get(r["entity_type"], "Прочее") for r in ac)
        chk = [c for c in cs if c["contact_found"] is not None]
        found = [c for c in chk if c["contact_found"]]
        sch = [c for c in cs if c["verdict"]]
        since_acts = sum(1 for c in cs if c["local_date"] >= "2026-09-22")
        return dict(since_acts=since_acts, first=min(c["local_date"] for c in cs), last=max(c["local_date"] for c in cs), days=len(days), total=len(cs),
                    out=len(out), inn=len(cs) - len(out), talked=len(talked), talked_out=len(talked_out),
                    hours=sec / 3600, avg=(sec / len(talked)) if talked else None, per_day=len(cs) / len(days) if days else None,
                    acts=len(ac), ent=ent, chk=len(chk), found=len(found),
                    need=sum(1 for c in found if c["need_filled"]), obj=sum(1 for c in found if c["objects_filled"]),
                    innf=sum(1 for c in found if c["inn_filled"]), task=sum(1 for c in found if c["task_created"]),
                    sch=len(sch), verdicts=collections.Counter(c["verdict"] for c in sch))
    S = {lg: stats(lg) for lg in logins}
    rows = [
        ("ЗВОНКИ", None, None, None),
        ("Первый звонок в данных", lambda s: datetime.fromisoformat(s["first"]).date(), "dt", "Источник звонков — история ВАТС: полная, без дублей. У Ткачевина ВАТС даёт звонки с 03.06.2026, у Никитина с 23.09.2026"),
        ("Последний звонок в данных", lambda s: datetime.fromisoformat(s["last"]).date(), "dt", ""),
        ("Дней со звонками", lambda s: s["days"], "n", ""),
        ("Всего звонков", lambda s: s["total"], "n", "входящие и исходящие"),
        ("из них исходящих", lambda s: s["out"], "n", ""),
        ("из них входящих", lambda s: s["inn"], "n", ""),
        ("Дозвонились (разговор > 0 с)", lambda s: s["talked"], "n", "все направления"),
        ("Дозвонились из исходящих", lambda s: s["talked_out"], "n", ""),
        ("Доля дозвона по исходящим", lambda s: pct(s["talked_out"], s["out"]), "p", "дозвонились / исходящих"),
        ("Часов разговора", lambda s: round(s["hours"], 1), "f", ""),
        ("Средняя длительность разговора, сек", lambda s: round(s["avg"]) if s["avg"] else None, "n", "по звонкам с разговором"),
        ("Звонков в день (по дням со звонками)", lambda s: round(s["per_day"], 1) if s["per_day"] else None, "f", ""),
        ("ЗАПОЛНЕНИЕ CRM: ДЕЙСТВИЯ В SYNERGY", None, None, None),
        ("Всего действий в CRM", lambda s: s["acts"], "n", "журнал действий пользователя в Synergy; в дашборде собирается только с 22.09.2026"),
    ] + [(f"  из них: {name}", (lambda n: lambda s: s["ent"][n])(name), "n", "") for name in ENTITY.values()] + [
        ("Действий в CRM на один звонок", lambda s: round(s["acts"] / s["since_acts"], 1) if s["since_acts"] else None, "f", "считается по звонкам с 22.09.2026 (с этой даты есть журнал действий)"),
        ("ЗАПОЛНЕНИЕ КАРТОЧКИ КЛИЕНТА ПОСЛЕ ЗВОНКА (проверка по звонкам)", None, None, None),
        ("Звонков с проверкой карточки", lambda s: s["chk"], "n", "после звонка проверяется карточка контакта в CRM"),
        ("Контакт найден в CRM", lambda s: s["found"], "n", ""),
        ("  потребность заполнена, % от найденных", lambda s: pct(s["need"], s["found"]), "p", "для Никитина (поиск у исполнителей) не применимо"),
        ("  объекты заполнены, %", lambda s: pct(s["obj"], s["found"]), "p", "для Никитина не применимо"),
        ("  ИНН заполнен, %", lambda s: pct(s["innf"], s["found"]), "p", ""),
        ("  задача по итогам звонка поставлена, %", lambda s: pct(s["task"], s["found"]), "p", "для Никитина не применимо"),
        ("ПОИСК ТЕХНИКИ: занесена ли техника исполнителя в базу «Транспорт»", None, None, None),
        ("Разобрано разговоров с исполнителями", lambda s: s["sch"], "n", "только Никитин (с 23.09.2026)"),
    ] + [(f"  {label}", (lambda k: lambda s: s["verdicts"][k])(k), "n", "") for k, label in VERDICT.items()]
    for label, fn, kind, note in rows:
        if fn is None:
            ws.append([label])
            for c in ws[ws.max_row]:
                c.font = BOLD
                c.fill = PatternFill("solid", fgColor="E8EEF5")
            continue
        ws.append([label] + [fn(S[lg]) for lg in logins] + [note])
        for j, lg in enumerate(logins, 2):
            c = ws.cell(ws.max_row, j)
            c.number_format = {"p": "0.0%", "f": "0.0", "n": "# ##0", "dt": "DD.MM.YYYY"}[kind]
            c.alignment = Alignment(horizontal="right")
    style(ws, widths={"A": 62, "B": 22, "C": 22, "D": 82}, height=40)
    for r in ws.iter_rows(min_row=2):
        if r[1].value is None and r[0].value and r[0].value.isupper():
            for c in r:
                c.font = BOLD
                c.fill = PatternFill("solid", fgColor="E8EEF5")
    ws.freeze_panes = "B2"

    # ============ По месяцам ============
    wm = wb.create_sheet("По месяцам")
    wm.append(["Месяц", "Менеджер", "Звонков всего", "Исходящих", "Дозвонились", "Доля дозвона (исходящие)", "Часов разговора",
               "Дней со звонками", "Действий в CRM (с 22.09)", "Проверено карточек", "Контакт найден", "Потребность заполнена, %", "ИНН заполнен, %",
               "Задача поставлена, %", "Разговоров с исполнителями", "Техника занесена, %"])
    for m in months:
        for lg in logins:
            cs = [c for c in by_mgr[lg] if c["local_date"][:7] == m]
            if not cs:
                continue
            out = [c for c in cs if c["direction"] == "out"]
            to = [c for c in out if (c["duration_sec"] or 0) > 0]
            found = [c for c in cs if c["contact_found"]]
            chk = [c for c in cs if c["contact_found"] is not None]
            sch = [c for c in cs if c["verdict"]]
            wm.append([m, SHORT[lg], len(cs), len(out), sum(1 for c in cs if (c["duration_sec"] or 0) > 0), pct(len(to), len(out)),
                       round(sum(c["duration_sec"] or 0 for c in cs) / 3600, 1), len({c["local_date"] for c in cs}),
                       (sum(1 for r in acts_by[lg] if r["local_date"][:7] == m) if m >= "2026-09" else None), len(chk), len(found),
                       pct(sum(1 for c in found if c["need_filled"]), len(found)), pct(sum(1 for c in found if c["inn_filled"]), len(found)),
                       pct(sum(1 for c in found if c["task_created"]), len(found)), len(sch),
                       pct(sum(1 for c in sch if c["verdict"] == "all_saved"), len(sch))])
    style(wm, widths={"A": 10, "B": 13, "C": 11, "D": 11, "E": 12, "F": 14, "G": 12, "H": 12, "I": 12, "J": 12, "K": 11, "L": 14, "M": 13, "N": 13, "O": 14, "P": 13}, height=48)
    for r in wm.iter_rows(min_row=2):
        for k in (5, 11, 12, 13, 15):
            r[k].number_format = "0%"
    wm.freeze_panes = "C2"

    # ============ По дням (+ диаграмма) ============
    wd = wb.create_sheet("По дням")
    wd.append(["Дата", "Никитин: звонков", "Никитин: дозвонились", "Никитин: минут разговора", "Никитин: действий в CRM",
               "Ткачевин: звонков", "Ткачевин: дозвонились", "Ткачевин: минут разговора", "Ткачевин: действий в CRM"])
    all_days = sorted({c["local_date"] for c in calls})
    for d in all_days:
        row = [datetime.fromisoformat(d).date()]
        for lg in logins:
            cs = [c for c in by_mgr[lg] if c["local_date"] == d]
            row += [len(cs), sum(1 for c in cs if (c["duration_sec"] or 0) > 0), round(sum(c["duration_sec"] or 0 for c in cs) / 60),
                    (sum(1 for r in acts_by[lg] if r["local_date"] == d) if d >= "2026-09-22" else None)]
        wd.append(row)
    style(wd, widths={"A": 12, "B": 12, "C": 13, "D": 14, "E": 13, "F": 12, "G": 13, "H": 14, "I": 13}, height=48)
    for r in wd.iter_rows(min_row=2):
        r[0].number_format = "DD.MM.YYYY"
    wd.freeze_panes = "B2"
    ch = BarChart()
    ch.type = "col"
    ch.title = "Звонки по дням"
    ch.height, ch.width = 9, 24
    ch.add_data(Reference(wd, min_col=2, max_col=2, min_row=1, max_row=wd.max_row), titles_from_data=True)
    ch.add_data(Reference(wd, min_col=6, max_col=6, min_row=1, max_row=wd.max_row), titles_from_data=True)
    ch.set_categories(Reference(wd, min_col=1, min_row=2, max_row=wd.max_row))
    ch.x_axis.number_format = "DD.MM"
    wd.add_chart(ch, "K2")
    ch2 = LineChart()
    ch2.title = "Действия в CRM по дням"
    ch2.height, ch2.width = 9, 24
    ch2.add_data(Reference(wd, min_col=5, max_col=5, min_row=1, max_row=wd.max_row), titles_from_data=True)
    ch2.add_data(Reference(wd, min_col=9, max_col=9, min_row=1, max_row=wd.max_row), titles_from_data=True)
    ch2.set_categories(Reference(wd, min_col=1, min_row=2, max_row=wd.max_row))
    ch2.x_axis.number_format = "DD.MM"
    wd.add_chart(ch2, "K22")

    # ============ По рабочим дням (по месяцам) + календарь ============
    from datetime import date as _d, timedelta as _td
    from openpyxl.formatting.rule import ColorScaleRule
    HOLIDAYS = {_d(2026, 11, 4): "праздник (День народного единства)"}   # праздники периода данных
    COLLECT_START = _d(2026, 1, 1)   # звонки теперь из ВАТС и полные за весь период: «неполных» дней нет
    WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    per_day = {lg: collections.defaultdict(lambda: [0, 0, 0]) for lg in logins}   # дата -> [всего, исходящих, дозвонились]
    for c in calls:
        row_ = per_day[c["vats_login"]][_d.fromisoformat(c["local_date"])]
        row_[0] += 1
        row_[1] += c["direction"] == "out"
        row_[2] += (c["duration_sec"] or 0) > 0
    start_of = {lg: _d.fromisoformat(min(c["local_date"] for c in by_mgr[lg])) for lg in logins}
    end_day = _d.fromisoformat(last_day)
    def day_kind(d):
        if d in HOLIDAYS:
            return "праздник"
        return "выходной" if d.weekday() >= 5 else "рабочий"

    ww = wb.create_sheet("По рабочим дням")
    ww.append(["Месяц", "Дата", "День", "Тип дня",
               "Никитин: звонков", "Никитин: исходящих", "Никитин: дозвонились",
               "Ткачевин: звонков", "Ткачевин: исходящих", "Ткачевин: дозвонились", "Примечание"])
    GREY = PatternFill("solid", fgColor="EDEDED")
    RED = PatternFill("solid", fgColor="F8CBAD")
    SUB = PatternFill("solid", fgColor="E8EEF5")
    cal = {lg: collections.defaultdict(dict) for lg in logins}   # lg -> месяц -> {день: всего}
    for m in months:
        y, mo = int(m[:4]), int(m[5:])
        d = _d(y, mo, 1)
        sums = {lg: [0, 0, 0] for lg in logins}          # все звонки месяца
        avg_sums = {lg: [0, 0, 0] for lg in logins}      # только полные дни (для среднего)
        workdays = {lg: 0 for lg in logins}              # рабочие дни с полными данными
        while d.month == mo and d <= end_day:
            kind = day_kind(d)
            vals_row, any_calls = [], False
            for lg in logins:
                if d < start_of[lg]:
                    vals_row += [None, None, None]
                    continue
                v = per_day[lg].get(d, [0, 0, 0])
                any_calls |= v[0] > 0
                vals_row += v
                cal[lg][m][d.day] = v[0]
                full = d >= COLLECT_START
                if kind == "рабочий" and full:
                    workdays[lg] += 1
                for i in range(3):
                    sums[lg][i] += v[i]
                    if kind == "рабочий" and full:
                        avg_sums[lg][i] += v[i]
            if kind == "рабочий" or any_calls:
                label = kind if kind == "рабочий" else (kind + (", но были звонки" if any_calls else ""))
                note = "данные неполные: сбор звонков в дашборде с 16.09.2026" if d < COLLECT_START else ""
                ww.append([m, d, WD[d.weekday()], label] + vals_row + [note])
                r = ww.max_row
                if d < COLLECT_START:
                    for c_ in ww[r]:
                        c_.fill = PatternFill("solid", fgColor="FFF2CC")
                elif kind != "рабочий":
                    for c_ in ww[r]:
                        c_.fill = GREY
                else:
                    for j, lg in enumerate(logins):
                        c_ = ww.cell(r, 5 + 3 * j)
                        if c_.value == 0 and d >= COLLECT_START:
                            c_.fill = RED      # рабочий день без звонков (при полных данных)
            d += _td(days=1)
        ww.append([m, "Итого за месяц", "", ""] + [sums[lg][i] for lg in logins for i in range(3)])
        ww.append([m, "Рабочих дней (с полными данными)", "", ""] + sum([[workdays[lg], None, None] for lg in logins], []))
        ww.append([m, "Среднее на рабочий день", "", ""] + sum([[round(avg_sums[lg][i] / workdays[lg], 1) if workdays[lg] else None for i in range(3)] for lg in logins], []))
        for k_ in range(3):
            for c_ in ww[ww.max_row - k_]:
                c_.font = BOLD
                c_.fill = SUB
    style(ww, widths={"A": 10, "B": 30, "C": 6, "D": 22, "E": 13, "F": 13, "G": 13, "H": 13, "I": 13, "J": 13, "K": 50}, height=48)
    for r in ww.iter_rows(min_row=2):
        if isinstance(r[1].value, _d) and not isinstance(r[1].value, datetime):
            r[1].number_format = "DD.MM.YYYY"
    ww.freeze_panes = "E2"

    def _safe_kind(m, dd):
        try:
            return day_kind(_d(int(m[:4]), int(m[5:]), dd))
        except ValueError:
            return ""

    wk = wb.create_sheet("Календарь звонков")
    hdr = ["День"] + [f"Никитин {m}" for m in months] + [""] + [f"Ткачевин {m}" for m in months]
    wk.append(hdr)
    n = len(months)
    for day in range(1, 32):
        row = [day]
        for lg in (logins[0], None, logins[1]):
            if lg is None:
                row.append(None)
                continue
            for m in months:
                row.append(cal[lg][m].get(day))
        wk.append(row)
    wk.append(["Всего"] + [sum(cal["никитин"][m].values()) or None for m in months] + [None] + [sum(cal["ткачевин"][m].values()) or None for m in months])
    wk.append(["Рабочих дней"] + [sum(1 for dd in range(1, 32) if dd in cal["никитин"][m] and _safe_kind(m, dd) == "рабочий") or None for m in months]
              + [None] + [sum(1 for dd in range(1, 32) if dd in cal["ткачевин"][m] and _safe_kind(m, dd) == "рабочий") or None for m in months])
    style(wk, widths={"A": 14}, height=34)
    for col in range(2, 2 * n + 3):
        wk.column_dimensions[wk.cell(1, col).column_letter].width = 13
    wk.column_dimensions[wk.cell(1, n + 2).column_letter].width = 3
    for rr in (wk.max_row, wk.max_row - 1):
        for c_ in wk[rr]:
            c_.font = BOLD
            c_.fill = SUB
    # выходные дни и праздники — серым в подписи дня (по каждому месяцу цвет ставим на ячейки значений)
    for day in range(1, 32):
        r = day + 1
        for blk, lg in ((0, "никитин"), (n + 1, "ткачевин")):
            for k_, m in enumerate(months):
                col = 2 + blk + k_
                try:
                    kind = day_kind(_d(int(m[:4]), int(m[5:]), day))
                except ValueError:
                    wk.cell(r, col).fill = GREY      # такого числа нет в месяце
                    continue
                if kind != "рабочий":
                    wk.cell(r, col).fill = GREY
    for blk in (0, n + 1):
        rng = f"{wk.cell(2, 2 + blk).coordinate}:{wk.cell(32, 1 + blk + n).coordinate}"
        wk.conditional_formatting.add(rng, ColorScaleRule(start_type="min", start_color="FFFFFF", end_type="max", end_color="5B9BD5"))
    wk.freeze_panes = "B2"
    wk["A35"] = "Серым отмечены выходные, праздники и несуществующие числа месяца; белые клетки без числа — нет данных по менеджеру в этот день."
    wk["A35"].font = Font(name="Arial", size=9, italic=True, color="7F7F7F")

    # ============ Заполнение карточки (+ диаграмма) ============
    wf = wb.create_sheet("Заполнение карточки")
    wf.append(["Месяц", "Менеджер", "Звонков с проверкой", "Контакт найден", "Потребность заполнена", "Объекты заполнены", "ИНН заполнен",
               "Задача поставлена", "% потребность", "% объекты", "% ИНН", "% задача"])
    for m in months:
        for lg in logins:
            found = [c for c in by_mgr[lg] if c["local_date"][:7] == m and c["contact_found"]]
            chk = [c for c in by_mgr[lg] if c["local_date"][:7] == m and c["contact_found"] is not None]
            if not chk:
                continue
            n = len(found)
            v = [sum(1 for c in found if c[k]) for k in ("need_filled", "objects_filled", "inn_filled", "task_created")]
            wf.append([m, SHORT[lg], len(chk), n] + v + [pct(x, n) for x in v])
    style(wf, widths={"A": 10, "B": 13, "C": 14, "D": 12, "E": 14, "F": 13, "G": 11, "H": 13, "I": 12, "J": 11, "K": 9, "L": 10}, height=48)
    for r in wf.iter_rows(min_row=2):
        for k in (8, 9, 10, 11):
            r[k].number_format = "0%"
    wf.freeze_panes = "C2"
    # данные для диаграммы: заполнение карточки по месяцам, только Ткачевин (у Никитина эти поля не применимы)
    wf["N1"], wf["O1"], wf["P1"], wf["Q1"], wf["R1"] = "Месяц (Ткачевин)", "% потребность", "% объекты", "% ИНН", "% задача"
    k = 1
    for r in wf.iter_rows(min_row=2, max_col=12, values_only=True):
        if r[1] == "Ткачевин":
            k += 1
            for j, v in enumerate((r[0], r[8], r[9], r[10], r[11])):
                c = wf.cell(k, 14 + j, v)
                if j:
                    c.number_format = "0%"
    for c in wf[1][13:18]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF"); c.fill = HEAD; c.border = BORDER
        c.alignment = Alignment(wrap_text=True, vertical="center")
    if k > 1:
        bc = BarChart()
        bc.type = "col"
        bc.title = "Ткачевин: заполнение карточки после звонка, % от найденных контактов"
        bc.height, bc.width = 9, 20
        bc.add_data(Reference(wf, min_col=15, max_col=18, min_row=1, max_row=k), titles_from_data=True)
        bc.set_categories(Reference(wf, min_col=14, min_row=2, max_row=k))
        bc.y_axis.number_format = "0%"
        wf.add_chart(bc, "N8")
        for col in "NOPQR":
            wf.column_dimensions[col].width = 14

    # ============ Поиск техники (Никитин) ============
    wp = wb.create_sheet("Поиск техники (Никитин)")
    wp.append(["Месяц"] + list(VERDICT.values()) + ["Всего разобрано", "Техника занесена, %"])
    for m in months:
        sch = [c for c in by_mgr["никитин"] if c["local_date"][:7] == m and c["verdict"]]
        if not sch:
            continue
        cnt = collections.Counter(c["verdict"] for c in sch)
        wp.append([m] + [cnt[k] for k in VERDICT] + [len(sch), pct(cnt["all_saved"], len(sch))])
    style(wp, widths={"A": 10, "B": 14, "C": 18, "D": 18, "E": 18, "F": 22, "G": 14, "H": 14}, height=48)
    for r in wp.iter_rows(min_row=2):
        r[7].number_format = "0%"
    start = wp.max_row + 3
    wp.cell(start - 1, 1, "Какую технику исполнители называли, а в карточке не было (по числу разговоров)").font = BOLD
    miss = collections.Counter()
    asked = collections.Counter()
    for c in by_mgr["никитин"]:
        if c["verdict"]:
            asked[c["asked_type"] or "не названа"] += 1
        if c["missing"]:
            try:
                for t in json.loads(c["missing"]):
                    miss[t] += 1
            except ValueError:
                pass
    wp.cell(start, 1, "Тип техники"); wp.cell(start, 2, "Не занесено, разговоров")
    wp.cell(start, 4, "О чём спрашивал менеджер"); wp.cell(start, 5, "Разговоров")
    for c in (wp.cell(start, 1), wp.cell(start, 2), wp.cell(start, 4), wp.cell(start, 5)):
        c.font = Font(name="Arial", bold=True, color="FFFFFF"); c.fill = HEAD; c.border = BORDER
    for i, (t, n) in enumerate(miss.most_common(20), 1):
        wp.cell(start + i, 1, t); wp.cell(start + i, 2, n)
    for i, (t, n) in enumerate(asked.most_common(20), 1):
        wp.cell(start + i, 4, t); wp.cell(start + i, 5, n)
    wp.column_dimensions["D"].width = 22

    # ============ Действия в CRM ============
    wa = wb.create_sheet("Действия в CRM")
    wa.append(["Менеджер", "Раздел CRM", "Действие", "Всего"] + months)
    cnt = collections.Counter((r["login"], r["entity_type"], r["action"], r["local_date"][:7]) for r in acts)
    keys = sorted({(k[0], k[1], k[2]) for k in cnt}, key=lambda k: (k[0], -sum(v for kk, v in cnt.items() if kk[:3] == k)))
    for lg, et, act in keys:
        per = [cnt[(lg, et, act, m)] or None for m in months]
        wa.append([SHORT[lg], ENTITY.get(et, et), act, sum(p or 0 for p in per)] + per)
    style(wa, widths={"A": 12, "B": 32, "C": 28, "D": 10}, height=34)
    wa.freeze_panes = "A2"
    wa.auto_filter.ref = wa.dimensions

    # ============ Звонки ============
    wc = wb.create_sheet("Звонки")
    wc.append(["Дата", "Время", "Менеджер", "Направление", "Телефон клиента", "Длительность, с", "Дозвонился",
               "Контакт найден в CRM", "Потребность", "Объекты", "ИНН", "Задача", "Что искал (поиск техники)", "Итог по технике"])
    yn = lambda v: None if v is None else ("да" if v else "нет")  # noqa: E731
    for c in sorted(calls, key=lambda c: c["started_at"], reverse=True):
        t = datetime.fromisoformat(c["started_at"])
        wc.append([t.date(), t.strftime("%H:%M"), SHORT[c["vats_login"]], "исходящий" if c["direction"] == "out" else "входящий",
                   c["client_phone"], c["duration_sec"] or 0, "да" if (c["duration_sec"] or 0) > 0 else "нет",
                   yn(c["contact_found"]), yn(c["need_filled"]) if c["contact_found"] else None, yn(c["objects_filled"]) if c["contact_found"] else None,
                   yn(c["inn_filled"]) if c["contact_found"] else None, yn(c["task_created"]) if c["contact_found"] else None,
                   c["asked_type"] or None, VERDICT.get(c["verdict"]) if c["verdict"] else None])
    style(wc, widths={"A": 12, "B": 8, "C": 11, "D": 12, "E": 16, "F": 12, "G": 11, "H": 13, "I": 12, "J": 10, "K": 8, "L": 9, "M": 26, "N": 30}, height=34)
    for r in wc.iter_rows(min_row=2):
        r[0].number_format = "DD.MM.YYYY"
    wc.freeze_panes = "A2"
    wc.auto_filter.ref = wc.dimensions

    wb.save(a.out)
    print(f"{a.out}: звонков {len(calls)} ({', '.join(SHORT[lg] + ' ' + str(len(by_mgr[lg])) for lg in logins)}), "
          f"действий в CRM {len(acts)}, период {first_day} — {last_day}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
