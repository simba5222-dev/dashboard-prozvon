import json, sys, collections
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

R = json.load(open(sys.argv[1], encoding="utf-8"))
OUT = sys.argv[2]
wb = Workbook()
HF = Font(name="Arial", bold=True, color="FFFFFF"); HP = PatternFill("solid", fgColor="305496"); F = Font(name="Arial", size=10)


def sheet(title, head, data, widths):
    ws = wb.create_sheet(title)
    ws.append(head)
    for c in ws[1]:
        c.font, c.fill, c.alignment = HF, HP, Alignment(wrap_text=True, vertical="center")
    for d in data:
        ws.append(d)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = F
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


iss = R["issues"]
kinds = collections.Counter((x["kind"], x["side"]) for x in iss)
by_mgr = collections.defaultdict(collections.Counter)
for x in iss:
    by_mgr[x["mgr"]][x["kind"]] += 1
for x in R["doc_diffs"]:
    for m in x["mgrs"]:
        by_mgr[m]["сумма по документу"] += 1

ws = wb.active
ws.title = "Сводка"
ws.append(["Сверка «Единого отчёта МСК» (лист ЕО_2026) с 1С на 18.11.2026"]); ws["A1"].font = Font(name="Arial", bold=True, size=13)
ws.append([f"Строк в отчёте проверено: {R['n_rows']}"])
ws.append([f"Документов сверено по сумме: реализации — {R['doc_ok'].get('реал',0)} совпали из {R['doc_ok'].get('реал',0)+sum(1 for x in R['doc_diffs'] if x['side']=='реал')}, поступления — {R['doc_ok'].get('пост',0)} совпали из {R['doc_ok'].get('пост',0)+sum(1 for x in R['doc_diffs'] if x['side']=='пост')}"])
ws.append([])
ws.append(["Тип замечания", "Реализация", "Поступление", "Обе стороны"])
for c in ws[5]:
    c.font, c.fill = HF, HP
names = {"нет документа": "Не указан номер документа", "нет в 1С": "Документ не найден в 1С", "не проведён": "Документ не проведён", "контрагент": "ИНН в отчёте ≠ контрагент документа", "арифметика": "Часы × ставка ≠ сумма в отчёте", "наработка": "Часы реализации ≠ часы поступления"}
for k, n in names.items():
    ws.append([n, kinds.get((k, "реал"), 0), kinds.get((k, "пост"), 0), kinds.get((k, "обе"), 0)])
ws.append(["Сумма по документу ≠ сумма строк отчёта", sum(1 for x in R["doc_diffs"] if x["side"] == "реал"), sum(1 for x in R["doc_diffs"] if x["side"] == "пост"), 0])
ws.append([])
ws.append(["По менеджерам (файлам)"]); ws[ws.max_row][0].font = Font(name="Arial", bold=True)
ws.append(["Менеджер", "Всего замечаний"])
for c in ws[ws.max_row]:
    c.font, c.fill = HF, HP
for m, c in sorted(by_mgr.items(), key=lambda kv: -sum(kv[1].values())):
    ws.append([m, sum(c.values())])
ws.column_dimensions["A"].width = 52
for col in "BCD":
    ws.column_dimensions[col].width = 16

# Документы
dd = sorted(R["doc_diffs"], key=lambda x: -abs(x["diff"]))
sheet("Суммы по документам", ["Сторона", "Документ 1С", "Организация", "Контрагент в 1С", "Дата", "Сумма строк отчёта", "Сумма документа 1С", "Разница", "Что не так (часы по ставкам)", "Строки в ЕО_2026", "Менеджеры", "Часть строк без часов"],
      [["реализация" if x["side"] == "реал" else "поступление", x["doc"], x["org"], x["cp"], x["date"], round(x["rep"], 2), round(x["c1"], 2), round(x["diff"], 2), x["notes"], ", ".join(map(str, x["rows"])), ", ".join(x["mgrs"]), "да" if x["partial"] else ""] for x in dd],
      [12, 14, 20, 30, 11, 15, 15, 12, 60, 24, 24, 10])

side = {"реал": "реализация", "пост": "поступление", "обе": "обе"}
for key, title in (("нет документа", "Нет номера документа"), ("контрагент", "ИНН не совпадает"), ("арифметика", "Арифметика отчёта"), ("наработка", "Часы реал. и пост."), ("не проведён", "Не проведены"), ("нет в 1С", "Нет в 1С")):
    rows = [x for x in iss if x["kind"] == key]
    if not rows:
        continue
    sheet(title, ["Менеджер", "Строка в ЕО_2026", "Заказчик", "Исполнитель", "Начало работ", "Техника", "Сторона", "Документ", "Замечание", "В отчёте", "В 1С", "Разница"],
          [[x["mgr"], x["row"], x["cust"], x["perf"], (x["start"].strftime("%d.%m.%Y") if hasattr(x["start"], "strftime") else x["start"]), x["tech"], side[x["side"]], x["doc"], x["text"], x["rep"], x["c1"], x["diff"]] for x in rows],
          [20, 10, 30, 30, 12, 24, 12, 16, 44, 28, 28, 10])

if R.get("debt"):
    debt = sorted(R["debt"], key=lambda x: -x["net"])
    sheet("Дебиторка", ["Заказчик (как в отчёте)", "Контрагент в 1С", "ИНН", "Наша организация", "Долг заказчика (Дт), ₽", "Авансы/переплата (Кт), ₽", "Итог: долг (+) / переплата (−), ₽"],
          [[x["cust"], x["name"], x["inn"], x["org"], round(x["dr"], 2), round(x["cr"], 2), round(x["net"], 2)] for x in debt], [32, 32, 14, 22, 18, 18, 22])
if R.get("debt_docs"):
    sheet("Долги по документам", ["Организация", "Заказчик", "Документ реализации", "Дата", "Сумма документа", "Долг по документу", "В отчёте отмечено «Оплачено»", "Строки ЕО_2026", "Менеджеры"],
          [[x["org"], x["cust"], x["doc"], x["date"], x["amount"], x["debt"], x["paid_mark"], x["rows"], x["mgrs"]] for x in R["debt_docs"]], [20, 32, 16, 11, 15, 15, 18, 22, 24])
wb.save(OUT)
print("saved", OUT)
