import json, openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter as L
R = json.load(open("res.json", encoding="utf-8"))["per_row"]
wb = openpyxl.load_workbook("rep.xlsx")
ws = wb["ЕО_2026"]
print("таблицы:", {k: ws.tables[k].ref for k in ws.tables})
heads = ["ПРОВЕРКА 1С: Итог по строке",
         "Реал.: документ в 1С", "Реал.: контрагент в 1С", "Реал.: ИНН заказчика", "Реал.: ∑ документа 1С", "Реал.: ∑ отчёта по документу", "Реал.: сверка сумм и ставок",
         "Пост.: документ в 1С", "Пост.: исполнитель в 1С", "Пост.: ИНН исполнителя", "Пост.: ∑ документа 1С", "Пост.: ∑ отчёта по документу", "Пост.: сверка сумм и ставок",
         "Часы реал. и пост.", "Долг заказчика в 1С (орг. документа), ₽", "Долг заказчика в 1С (все орг.), ₽"]
import re, collections
D = json.load(open("docs2.json", encoding="utf-8")); refs = json.load(open("refs.json", encoding="utf-8"))
RES = json.load(open("res.json", encoding="utf-8"))
debt_ref = collections.defaultdict(float)
for x in D["bal"]:
    if x["acc"] == "62.01":
        debt_ref[x["e3"]] += (x["dr"] or 0) - (x["cr"] or 0)
real_d = {k[5:]: v for k, v in RES["docs_sel"].items() if k.startswith("реал|")}
heads += ["Реал.: долг по документу 1С, ₽", "Реал.: оплата по 1С", "Оплата: отчёт и 1С"]
c0 = 63
HF, HP = Font(name="Arial", bold=True, color="FFFFFF", size=10), PatternFill("solid", fgColor="C00000")
for k, h in enumerate(heads):
    c = ws.cell(1, c0 + k, h); c.font, c.fill, c.alignment = HF, HP, Alignment(wrap_text=True, vertical="center")
    ws.column_dimensions[L(c0 + k)].width = 40 if k in (0, 6, 12, 18) else 22
RED, GRN, YEL = PatternFill("solid", fgColor="F8CBAD"), PatternFill("solid", fgColor="C6E0B4"), PatternFill("solid", fgColor="FFE699")
F = Font(name="Arial", size=10)
for i, p in R.items():
    i = int(i)
    re_, po = p["реал"], p["пост"]
    vals = [p["verdict"], re_["doc"], re_["cp"], re_["inn"], re_["c1"], re_["rep"], re_["sum"], po["doc"], po["cp"], po["inn"], po["c1"], po["rep"], po["sum"], p["hours_diff"], p["debt_org"], p["debt_all"]]
    for k, v in enumerate(vals):
        c = ws.cell(i, c0 + k, v); c.font = F
        if k in (4, 5, 10, 11, 14, 15) and isinstance(v, (int, float)):
            c.number_format = "#,##0"
    ws.cell(i, c0).fill = GRN if p["verdict"] == "ок" else RED
    for k, bad in ((1, re_["bad"]), (7, po["bad"])):
        ws.cell(i, c0 + k).fill = RED if bad else GRN
    if re_["inn"] == "НЕ совпадает": ws.cell(i, c0 + 3).fill = RED
    if po["inn"] == "НЕ совпадает": ws.cell(i, c0 + 9).fill = RED
    for k in (6, 12):
        if str(vals[k]).startswith("РАСХОЖДЕНИЕ"): ws.cell(i, c0 + k).fill = RED
    if p["hours_diff"]: ws.cell(i, c0 + 13).fill = YEL
    if (p["debt_org"] or 0) > 1: ws.cell(i, c0 + 14).fill = YEL
    if (p["debt_all"] or 0) > 1: ws.cell(i, c0 + 15).fill = YEL
dc = c0 + len(heads) - 3
docs_list = []
for i in R:
    i = int(i)
    T = [t for t, h in RES["resolved"].get(f"{i}|реал", []) if t in real_d]
    mark = str(ws.cell(i, 43).value or "").strip()
    if not T:
        continue
    debt = sum(debt_ref.get(real_d[t]["ref"], 0.0) for t in T); tot = sum(real_d[t]["amount"] or 0 for t in T)
    st = "оплачен" if debt <= 1 else ("долг, оплачено частично" if debt < tot - 1 else "не оплачен")
    if mark == "Оплачено" and debt > 1: cmp_ = "В ОТЧЁТЕ «Оплачено», В 1С ДОЛГ"
    elif mark != "Оплачено" and debt <= 1: cmp_ = "в отчёте «" + (mark or "пусто") + "», в 1С оплачено"
    else: cmp_ = "совпадает"
    for k, v in enumerate((round(debt, 2), st, cmp_)):
        c = ws.cell(i, dc + k, v); c.font = F
        if k == 0: c.number_format = "#,##0"
    ws.cell(i, dc + 1).fill = RED if debt > 1 else GRN
    ws.cell(i, dc + 2).fill = RED if cmp_.startswith("В ОТЧЁТЕ") else (YEL if cmp_.startswith("в отчёте") else GRN)
for t in sorted(real_d):
    d = real_d[t]; debt = debt_ref.get(d["ref"], 0.0)
    docs_list.append({"org": d["org"], "cust": d["cp"], "doc": t, "date": d["date"][:10], "amount": d["amount"], "debt": round(debt, 2), "paid_mark": "", "rows": "", "mgrs": ""})
json.dump(docs_list, open("debt_docs.json", "w", encoding="utf-8"), ensure_ascii=False)
ws.row_dimensions[1].height = 45
out = "2026-Единый отчет МСК (с проверкой 1С).xlsx"
wb.save(out); print("saved")
wb2 = openpyxl.load_workbook(out)
w2 = wb2["ЕО_2026"]
print(w2.cell(2, 49).value[:60], [(s.title, len(s._pivots)) for s in wb2.worksheets], w2.cell(2, 63).value, w2.cell(1914, 1).value)
