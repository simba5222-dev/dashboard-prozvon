"""Что записать в CRM по разобранному звонку менеджера «тёплого прозвона».

Здесь только расчёт: из результата разбора и текущих значений полей получается список
записей `plan_*`. Сама отправка в Synergy — отдельный шаг и пока не включена.

Правила (решения владельца 19.11.2026):
- пишем только то, что прозвучало: нет даты — поле «Следующий контакт» не трогаем;
- чужое не затираем: ручную правку менеджера не перезаписываем (помним, что писали сами,
  таблица `coach_crm_writes`);
- «Объекты» и «Новая потребность» дописываем строкой с датой, старое не стираем;
- «Звонить реже» только повышаем до «Раз в месяц» / «Не звонить», обратно в «Обычно» не возвращаем.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

# контакт
F_NEXT = "custom-30779"        # Следующий контакт (дата)
F_OBJECTS = "custom-30780"     # Объекты Клиента (текст)
F_FREQ = "custom-30781"        # Звонить реже (список)
F_DECIDER = "custom-30782"     # Кто решает? (текст)
F_NEED = "custom-30783"        # Новая потребность (текст)
# звонок
F_REVIEW = "custom-30784"      # Разбор Звонка (текст)
F_SCORE = "custom-30785"       # Оценка звонка ТП (число)
F_OWNER = "custom-30787"       # Нужен РОП (Да/Нет)
F_OUTCOME = "custom-30788"     # Итог разговора (список)

OUTCOME = {"договорились": "Договорились", "перезвон назначен": "Перезвон", "отказ": "Отказ",
           "не решено": "Не решено", "не по делу": "Не по делу"}
FREQ = {"раз в месяц": "Раз в месяц", "не звонить": "Не звонить"}
FREQ_RANK = {"Обычно": 0, "Раз в месяц": 1, "Не звонить": 2}

TABLE = """CREATE TABLE IF NOT EXISTS coach_crm_writes(
    target TEXT, target_id TEXT, field TEXT, value TEXT, call_uid TEXT, created_at TEXT,
    PRIMARY KEY(target, target_id, field))"""


def score(skills: dict[str, Any]) -> float | None:
    vals = [v for v in (skills or {}).values() if v in (0, 1, 2)]
    return round(sum(vals) / (2 * len(vals)) * 10, 1) if vals else None


def review_text(a: dict[str, Any], link: str) -> str:
    lines = [str(a.get("summary") or "").strip()]
    for g in a.get("good") or []:
        lines.append(f"+ {g['what']} («{g['quote']}»)")
    for b in a.get("bad") or []:
        lines.append(f"- {b['what']} («{b['quote']}»). Лучше: {b.get('better', '')}")
    if a.get("attention"):
        lines.append(f"! {a['attention']}")
    lines.append(link)
    return "\n".join(x for x in lines if x)[:2000]


def _line(day: str, text: str) -> str:
    d = date.fromisoformat(day)
    return f"{d:%d.%m} — {text.strip()}"


def _append(current: str, new_line: str) -> str | None:
    """Дописать строку, если такой ещё нет. None — нечего писать."""
    key = re.sub(r"^\d\d\.\d\d — ", "", new_line).lower()
    if any(key in ln.lower() for ln in (current or "").splitlines()):
        return None
    return ((current or "").rstrip() + "\n" + new_line).strip()


def plan_call(a: dict[str, Any], link: str) -> dict[str, Any]:
    out: dict[str, Any] = {F_REVIEW: review_text(a, link)}
    sc = score(a.get("skills") or {})
    if sc is not None:
        out[F_SCORE] = sc
    if a.get("outcome") in OUTCOME:
        out[F_OUTCOME] = OUTCOME[a["outcome"]]
    out[F_OWNER] = "Да" if a.get("needs_owner") else "Нет"
    return out


def plan_contact(a: dict[str, Any], day: str, current: dict[str, Any], mine: dict[str, str]) -> dict[str, tuple[Any, str]]:
    """Вернуть {поле: (значение, пояснение)}. Чего нет в ответе — писать не нужно.

    current — значения полей контакта сейчас; mine — что в них писали мы в прошлый раз.
    """
    out: dict[str, tuple[Any, str]] = {}
    today = date.fromisoformat(day)

    nd = a.get("next_date") or ""
    if nd:
        cur = str(current.get(F_NEXT) or "")[:10]
        if nd < str(today):
            pass   # дата в прошлом относительно звонка — модель ошиблась, не пишем
        elif not cur or cur < str(today) or cur == mine.get(F_NEXT):
            if cur != nd:
                out[F_NEXT] = (nd, "новая дата" if cur else "поле было пустое")
        else:
            out[F_NEXT] = (None, f"не трогаю: уже стоит {cur}, поставил не ИИ")

    objs = [o for o in a.get("objects") or [] if isinstance(o, dict)]
    if objs:
        text = current.get(F_OBJECTS) or ""
        for o in objs:
            line = _line(day, "; ".join(x for x in (o.get("where"), o.get("work"), o.get("when") and f"старт: {o['when']}") if x))
            new = _append(text, line)
            if new:
                text = new
        if text != (current.get(F_OBJECTS) or ""):
            out[F_OBJECTS] = (text, "дописано к существующему" if current.get(F_OBJECTS) else "поле было пустое")

    if a.get("new_need") and a.get("need_text"):
        new = _append(current.get(F_NEED) or "", _line(day, a["need_text"]))
        if new:
            out[F_NEED] = (new, "дописано к существующему" if current.get(F_NEED) else "поле было пустое")

    dm = str(a.get("decision_maker") or "").strip()
    if dm:
        cur = str(current.get(F_DECIDER) or "").strip()
        if not cur:
            out[F_DECIDER] = (dm, "поле было пустое")
        elif cur.lower() != dm.lower() and cur != mine.get(F_DECIDER):
            out[F_DECIDER] = (None, f"не трогаю: стоит «{cur}», ИИ услышал «{dm}»")

    pref = FREQ.get(str(a.get("contact_pref") or "").lower())
    if pref:
        cur = current.get(F_FREQ)
        cur = cur[0] if isinstance(cur, list) and cur else cur
        cur = {"Не Звонить": "Не звонить"}.get(cur, cur) or "Обычно"
        if FREQ_RANK.get(pref, 0) > FREQ_RANK.get(cur, 0):
            out[F_FREQ] = (pref, f"было «{cur}»")
    return out
