"""Сверка «Единого отчёта МСК» (лист ЕО_2026) с реализациями и поступлениями 1С + дебиторка. Только чтение."""
import json, re, sys, collections, datetime
import openpyxl

REP, DOCS, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
TR = str.maketrans("CcAaBbEeKkMmHhOoPpTtXx", "СсАаВвЕеКкМмНнОоРрТтХх")
RX = re.compile(r"([A-Za-zА-Яа-я]{3,5})\s*-\s*(\d{4,7})(?:\s*\((\d+(?:[.,]\d+)?)\))?")


def toks(v):
    return [(a.translate(TR).upper() + "-" + n.zfill(6), (float(h.replace(",", ".")) if h else None)) for a, n, h in RX.findall(str(v or ""))]


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def close(a, b, tol=1.0):
    return a is not None and b is not None and abs(a - b) <= tol


def inn_of(v):
    return re.sub(r"\D", "", str(v or ""))


wb = openpyxl.load_workbook(REP, read_only=True, data_only=True)
raw = list(wb["ЕО_2026"].iter_rows(values_only=True))
rows = []
for i, r in enumerate(raw[1:], start=2):
    r = list(r[:57]) + [None] * (57 - len(r[:57]))
    if r[0] and r[0] != "Итог" and (r[6] or r[43]):
        rows.append((i, r))
D = json.load(open(DOCS, encoding="utf-8"))
real, post = {}, {}
cand = {"реал": {x["want"]: x for x in D["real"]}, "пост": {x["want"]: x for x in D["post"]}}
resolved = {}


def pick(side, number, r):
    """Номера в 1С повторяются по годам: выбираем документ с ИНН контрагента как в отчёте и датой ближе к работам."""
    c = cand[side].get(number, {}).get("cands") or []
    if not c:
        return None
    inn = inn_of(r[7] if side == "реал" else r[20])
    ref_d = r[3] if isinstance(r[3], datetime.datetime) else (r[2] if isinstance(r[2], datetime.datetime) else None)
    best, bs = None, -1e9
    for d in c:
        sc = 0.0
        if inn and inn_of(d["inn"]) == inn:
            sc += 1000
        if ref_d:
            dd = (datetime.datetime.fromisoformat(d["date"][:10]) - ref_d).days
            sc -= (abs(dd) if dd >= -10 else 3 * abs(dd))
        if d["posted"]:
            sc += 5
        if sc > bs:
            best, bs = d, sc
    return best


def doc_key(d):
    return f"{d['number'].replace('CК', 'СК')} от {d['date'][8:10]}.{d['date'][5:7]}.{d['date'][:4]}"
bal = D.get("bal", [])

for i, r in rows:
    for side, cell, src in (("реал", r[43], real), ("пост", r[44], post)):
        lst = []
        for t, h in toks(cell):
            d = pick(side, t, r)
            if d is None:
                lst.append((t, h))
            else:
                k = doc_key(d)
                src[k] = d
                lst.append((k, h))
        resolved[(i, side)] = lst
issues = []   # (источник, строка, заказчик, исполнитель, док, тип, что не так, в отчёте, в 1С, разница)


def add(r, i, kind, side, doc, text, rep, one, diff=None):
    issues.append({"mgr": str(r[0]).replace("ЗП МСК ", "").replace(" 2026.xlsx", ""), "row": i, "cust": r[6], "perf": r[19], "doc": doc, "side": side, "kind": kind, "text": text, "rep": rep, "c1": one, "diff": diff,
                   "start": r[2], "tech": r[5]})


# ---------- построчная сверка ----------
used = collections.defaultdict(set)      # (док, номер строки) -> занято строками отчёта
cover = collections.defaultdict(float)   # док -> сумма строк отчёта, привязанных к нему
cover_rows = collections.defaultdict(list)
stats = collections.Counter()
agg = collections.defaultdict(lambda: collections.defaultdict(float)); agg_sum = collections.defaultdict(float); agg_rows = collections.defaultdict(list); agg_skip = set()


def pick_line(doc, hours, rate, tot, key):
    best, score = None, 0
    for n, l in enumerate(doc["lines"]):
        if (key, n) in used[key]:
            continue
        q, p, s = num(l["qty"]), num(l["price"]), num(l["sum"])
        sc = (3 if close(q, hours, 0.01) else 0) + (3 if close(p, rate, 0.01) else 0) + (4 if close(s, tot, 1) else 0)
        if sc > score:
            best, score, bn = l, sc, n
    if best is not None and score >= 4:
        used[key].add((key, bn))
        return best, score
    return None, 0


for i, r in rows:
    cust_inn, perf_inn = inn_of(r[7]), inn_of(r[20])
    h_r, rate_r, s_r = num(r[8]), num(r[11]), num(r[12])
    h_p, rate_p, s_p = num(r[21]), num(r[24]), num(r[25])
    # арифметика самого отчёта
    if h_r is not None and rate_r is not None and s_r is not None and not close(h_r * rate_r, s_r, 1.01):
        add(r, i, "арифметика", "реал", "", "часы × ставка ≠ сумма в отчёте", f"{h_r:g} × {rate_r:g} = {h_r*rate_r:,.2f}", f"{s_r:,.2f}", round(h_r * rate_r - s_r, 2))
    if h_p is not None and rate_p is not None and s_p is not None and not close(h_p * rate_p, s_p, 1.01):
        add(r, i, "арифметика", "пост", "", "часы × ставка ≠ сумма в отчёте", f"{h_p:g} × {rate_p:g} = {h_p*rate_p:,.2f}", f"{s_p:,.2f}", round(h_p * rate_p - s_p, 2))
    if h_r is not None and h_p is not None and abs(h_r - h_p) > 0.01 and (r[19] not in (None, "", "-")):
        add(r, i, "наработка", "обе", "", "часы по реализации ≠ часы по поступлению", f"реал. {h_r:g}", f"пост. {h_p:g}", round(h_r - h_p, 2))
    for side, cell, hh, rr, ss, inn, label in (("реал", r[43], h_r, rate_r, s_r, cust_inn, "заказчик"), ("пост", r[44], h_p, rate_p, s_p, perf_inn, "исполнитель")):
        T = resolved[(i, side)]
        src = real if side == "реал" else post
        if not T:
            txt = str(cell).strip().replace("\n", " ") if cell not in (None, "") else "пусто"
            if side == "реал" or (r[19] not in (None, "", "-") and not str(r[19]).lower().startswith("без") and str(cell) not in ("СТ", "Дельта")):
                stats[f"нет номера документа ({side})"] += 1
                add(r, i, "нет документа", side, "", "в отчёте не указан номер документа 1С" if not isinstance(cell, datetime.datetime) else "вместо номера документа стоит дата", txt if not isinstance(cell, datetime.datetime) else cell.strftime("%d.%m.%Y"), "", None)
            continue
        found = [(t, h) for t, h in T if t in src and not src[t].get("missing")]
        for t, h in T:
            if t not in src or src[t].get("missing"):
                add(r, i, "нет в 1С", side, t, "документ из отчёта не найден в 1С", t, "", None)
        if not found:
            continue
        # реквизиты документа
        for t, h in found:
            d = src[t]
            if not d["posted"]:
                add(r, i, "не проведён", side, t, "документ в 1С не проведён", t, "не проведён", None)
            if inn and inn_of(d["inn"]) and inn != inn_of(d["inn"]):
                add(r, i, "контрагент", side, t, f"ИНН {label}а в отчёте не совпадает с контрагентом документа", f"{inn} ({r[6] if side=='реал' else r[19]})", f"{d['inn']} ({d['cp']})", None)
        # вклад строки отчёта в документ: часы по ставке
        for t, h in found:
            hrs = h if h is not None else (hh if len(found) == 1 else None)
            if hrs is None or rr is None:
                agg_skip.add((side, t))
                continue
            k = (side, t)
            agg[k][round(rr, 2)] += hrs
            agg_sum[k] += hrs * rr if len(found) > 1 else (ss if ss is not None else hrs * rr)
            agg_rows[k].append(i)

# ---------- по документам: сумма отчёта против суммы документа ----------
doc_diffs = []
doc_ok = collections.Counter()
for (side, t), rates in agg.items():
    d = (real if side == "реал" else post).get(t)
    one = collections.defaultdict(float); one_sum = 0.0
    for l in d["lines"]:
        one[round(num(l["price"]) or 0, 2)] += num(l["qty"]) or 0
        one_sum += num(l["sum"]) or 0
    notes = []
    for rate in sorted(set(rates) | set(one)):
        a_, b_ = rates.get(rate, 0.0), one.get(rate, 0.0)
        if abs(a_ - b_) > 0.01:
            notes.append(f"ставка {rate:g}: в отчёте {a_:g} ч, в 1С {b_:g} ч")
    sdiff = agg_sum[(side, t)] - one_sum
    if notes or abs(sdiff) > 1:
        doc_diffs.append({"side": side, "doc": t, "org": d["org"], "cp": d["cp"], "date": d["date"][:10], "rep": agg_sum[(side, t)], "c1": one_sum, "diff": sdiff, "notes": "; ".join(notes) or "часы и ставки совпали, расходится сумма",
                          "partial": (side, t) in agg_skip, "rows": sorted(set(agg_rows[(side, t)])), "mgrs": sorted({str(raw[j - 1][0]).replace("ЗП МСК ", "").replace(" 2026.xlsx", "") for j in agg_rows[(side, t)]})})
    else:
        doc_ok[side] += 1

# ---------- дебиторка ----------
# остаток 62.01/62.02 по контрагентам и документам расчётов
cust_inns = {}
for i, r in rows:
    if inn_of(r[7]):
        cust_inns.setdefault(inn_of(r[7]), r[6])
# нужен словарь ref документа -> номер: берём из real
ref2num = {}
debt_doc = {}
by_cp = collections.defaultdict(lambda: {"dr": 0.0, "cr": 0.0, "orgs": set(), "name": "", "inn": ""})
for b in bal:
    if not b["acc"].startswith("62"):
        continue
    k = inn_of(b["inn"]) or b["cp"]
    e = by_cp[(k, b["org"])]
    e["name"], e["inn"] = b["cp"], inn_of(b["inn"])
    e["dr"] += b["dr"] or 0
    e["cr"] += b["cr"] or 0

debt_rows = []
for (k, org), e in by_cp.items():
    if e["inn"] in cust_inns:
        debt_rows.append({"cust": cust_inns[e["inn"]], "name": e["name"], "inn": e["inn"], "org": org, "dr": e["dr"], "cr": e["cr"], "net": e["dr"] - e["cr"]})

json.dump({"issues": issues, "doc_diffs": doc_diffs, "doc_ok": dict(doc_ok), "debt": debt_rows, "docs_sel": {**{"реал|"+k: v for k, v in real.items()}, **{"пост|"+k: v for k, v in post.items()}}, "resolved": {f"{k[0]}|{k[1]}": v for k, v in resolved.items()}, "stats": dict(stats), "n_rows": len(rows)}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, default=str)
print("строк", len(rows), "замечаний", len(issues), "расхождений по документам", len(doc_diffs), "дебиторов", len(debt_rows))
print(collections.Counter((x["kind"], x["side"]) for x in issues).most_common())
print(dict(stats))

# ---------- по каждой строке отчёта: поля для новых столбцов ----------
dd_map = {(x["side"], x["doc"]): x for x in doc_diffs}
iss_by = collections.defaultdict(list)
for x in issues:
    iss_by[(x["row"], x["side"])].append(x)
net_by_inn = collections.defaultdict(float); net_by_inn_org = collections.defaultdict(float)
for x in debt_rows:
    net_by_inn[x["inn"]] += x["net"]; net_by_inn_org[(x["inn"], x["org"])] += x["net"]
per_row = {}
for i, r in rows:
    pr = {}
    for side, cell, src in (("реал", r[43], real), ("пост", r[44], post)):
        T = resolved[(i, side)]
        probs = iss_by.get((i, side), [])
        if not T:
            if any(x["kind"] == "нет документа" for x in probs):
                pr[side] = {"doc": "нет номера документа", "cp": "", "inn": "", "c1": None, "rep": None, "sum": "", "org": "", "bad": True}
            else:
                pr[side] = {"doc": "не требуется", "cp": "", "inn": "", "c1": None, "rep": None, "sum": "", "org": "", "bad": False}
            continue
        ds = [src[t] for t, h in T if t in src and not src[t].get("missing")]
        miss = [t for t, h in T if t not in src or src[t].get("missing")]
        bad = bool(miss)
        st = []
        if miss:
            st.append("нет в 1С: " + ", ".join(miss))
        if any(not d["posted"] for d in ds):
            st.append("не проведён"); bad = True
        inn_bad = any(x["kind"] == "контрагент" for x in probs)
        sumtxt = ""
        c1 = sum(num(d["amount"]) or 0 for d in ds) if ds else None
        repdoc = None
        if len(T) == 1 and ds:
            repdoc = agg_sum.get((side, T[0][0]))
            x = dd_map.get((side, T[0][0]))
            if x:
                sumtxt = ("РАСХОЖДЕНИЕ: " + x["notes"]) if abs(x["diff"]) > 1 or "ставка" in x["notes"] else ""
                if abs(x["diff"]) > 1:
                    sumtxt = f"РАСХОЖДЕНИЕ: отчёт {x['rep']:,.0f} ₽ / 1С {x['c1']:,.0f} ₽ (разница {x['diff']:,.0f})"
                    bad = True
                else:
                    sumtxt = "сумма совпала, часы/ставки по строкам 1С разбиты иначе"
            else:
                sumtxt = "совпала"
        elif ds:
            sumtxt = "несколько документов в ячейке — по каждому см. лист «Суммы по документам»"
        if inn_bad:
            bad = True
        pr[side] = {"doc": "; ".join(st) or "найден", "cp": "; ".join(sorted({d["cp"] for d in ds})), "inn": ("НЕ совпадает" if inn_bad else ("совпадает" if ds else "")), "c1": c1, "rep": repdoc, "sum": sumtxt,
                    "org": "; ".join(sorted({d["org"] for d in ds})), "bad": bad}
    why = []
    if pr["реал"]["bad"]: why.append("реализация")
    if pr["пост"]["bad"]: why.append("поступление")
    hr = any(x["kind"] == "наработка" and x["side"] == "обе" for x in iss_by.get((i, "обе"), []))
    if hr: why.append("часы реал.≠пост.")
    ar = [x for s in ("реал", "пост") for x in iss_by.get((i, s), []) if x["kind"] == "арифметика"]
    if ar: why.append("арифметика")
    inn_c = inn_of(r[7])
    orgs = [o for o in pr["реал"]["org"].split("; ") if o]
    pr["hours_diff"] = "часы реал. ≠ пост." if hr else ""
    pr["debt_org"] = sum(net_by_inn_org.get((inn_c, o), 0) for o in orgs) if (inn_c and orgs) else None
    pr["debt_all"] = net_by_inn.get(inn_c) if inn_c in net_by_inn else (0 if inn_c in cust_inns else None)
    pr["verdict"] = "ПРОВЕРИТЬ: " + ", ".join(why) if why else "ок"
    per_row[i] = pr
json.dump({"issues": issues, "doc_diffs": doc_diffs, "doc_ok": dict(doc_ok), "debt": debt_rows, "docs_sel": {**{"реал|"+k: v for k, v in real.items()}, **{"пост|"+k: v for k, v in post.items()}}, "resolved": {f"{k[0]}|{k[1]}": v for k, v in resolved.items()}, "stats": dict(stats), "n_rows": len(rows), "per_row": per_row}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, default=str)
print("verdicts", collections.Counter(v["verdict"].split(":")[0] for v in per_row.values()))
