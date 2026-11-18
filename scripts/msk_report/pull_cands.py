"""ТОЛЬКО ЧТЕНИЕ. Документы 1С по номерам из отчёта: реализации и поступления + остатки по расчётам -> ~/msk_cands.json"""
import sys, os, json, time, threading
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.expanduser("~/scan_registry"))
import onec
need = json.load(open(os.path.expanduser("~/msk_need.json")))
LAT = str.maketrans("СА", "CA")   # у СК в 1С первая буква может быть латинской C
od = onec.OData()
orgs = {o["Ref_Key"]: o["Description"] for o in od.get("Catalog_Организации", {"$top": 50, "$select": "Ref_Key,Description"})["value"]}
cps, lock = {}, threading.Lock()
def cp(ref):
    with lock:
        if ref in cps: return cps[ref]
    v = od.get("Catalog_Контрагенты", {"$filter": f"Ref_Key eq guid'{ref}'", "$top": 1, "$select": "Ref_Key,Description,ИНН"})["value"]
    r = {"name": v[0]["Description"], "inn": v[0]["ИНН"]} if v else {"name": "", "inn": ""}
    with lock: cps[ref] = r
    return r
def one(ent, number):
    found = []
    for num in dict.fromkeys([number, number.replace("СК", "CК", 1)]):
        rows = None
        for att in range(3):
            try:
                rows = od.get(ent, {"$top": 30, "$filter": f"Number eq '{num}' and DeletionMark eq false"})["value"]
                break
            except Exception:
                time.sleep(1)
        for r in rows or []:
            lines = []
            for tab in ("Услуги", "Товары"):
                try:
                    lines += [{"t": tab, "text": l.get("Содержание") or "", "qty": l.get("Количество"), "price": l.get("Цена"), "sum": l.get("Сумма"), "vat_rate": l.get("СтавкаНДС"), "vat": l.get("СуммаНДС")}
                              for l in od.get(f"{ent}(guid'{r['Ref_Key']}')/{tab}", {"$top": 200})["value"]]
                except Exception:
                    pass
            c = cp(r["Контрагент_Key"])
            found.append({"ref": r["Ref_Key"], "number": r["Number"], "date": r["Date"], "posted": r["Posted"], "amount": r["СуммаДокумента"], "org": orgs.get(r["Организация_Key"], ""),
                          "comment": r.get("Комментарий") or "", "in_no": r.get("НомерВходящегоДокумента") or "", "cp": c["name"], "inn": c["inn"], "lines": lines})
    return {"want": number, "cands": found, "missing": not found}
res = {}
for key, ent in (("real", "Document_РеализацияТоваровУслуг"), ("post", "Document_ПоступлениеТоваровУслуг")):
    with ThreadPoolExecutor(4) as ex:
        res[key] = list(ex.map(lambda n: one(ent, n), need[key]))
    print(key, len(res[key]), "не найдено", sum(1 for x in res[key] if x.get("missing")), flush=True)
json.dump(res, open(os.path.expanduser("~/msk_cands.json"), "w"), ensure_ascii=False)
print("DOCS_DONE", flush=True)
print("DONE", flush=True)
