import sys, os, json
sys.path.insert(0, os.path.expanduser("~/scan_registry"))
import onec
od = onec.OData()
need = set(json.load(open(os.path.expanduser("~/msk_need.json")))["real"])
out, skip = {}, 0
flt = "Date ge datetime'2025-01-01T00:00:00' and DeletionMark eq false"
while True:
    rows = od.get("Document_РеализацияТоваровУслуг", {"$top": 500, "$skip": skip, "$orderby": "Date", "$filter": flt, "$select": "Ref_Key,Number,Date,СуммаДокумента"})["value"]
    for r in rows:
        n = r["Number"].replace("CК", "СК")
        out[r["Ref_Key"]] = {"number": n, "date": r["Date"][:10], "amount": r["СуммаДокумента"]}
    if len(rows) < 500: break
    skip += 500
print(len(out), flush=True)
json.dump(out, open(os.path.expanduser("~/real_refs.json"), "w"), ensure_ascii=False)
