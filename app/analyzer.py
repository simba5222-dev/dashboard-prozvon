"""Разбор расшифровки разговора: что сказал клиент и чего нет в карточке.

Это не пересказ ради пересказа. Владельцу нужны два ответа:

1. **Что упущено.** Клиент назвал технику, сроки, объект или обещание
   перезвонить, а в CRM этого нет — значит, сведения умрут вместе с записью.
2. **Что делать.** Короткие рекомендации по этому клиенту и по тому, как
   менеджер вёл разговор.

Поэтому модели дают не только расшифровку, но и то, что менеджер внёс в
карточку: без этого «упущено» не отличить от «записано».

Расшифровка приходит с телефонного звука и местами рвётся. Модель об этом
предупреждена и обязана опираться на дословную цитату: пункт без цитаты из
разговора — выдумка, а по этим строкам судят о работе людей.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Поля карточки, на языке которых говорим и с моделью, и с руководителем.
MISSED_FIELDS = (
    "потребность",
    "объекты",
    "компания",
    "ИНН",
    "задача",
    "контактное лицо",
    "сроки",
    "конкурент",
)

EMPTY_ANALYSIS: dict[str, Any] = {
    "summary": "",
    "client_need": "",
    "need_stated": False,
    "next_step": "",
    "missed": [],
    "recommendations": [],
    "call_quality": None,
    "quality_notes": "",
    "unusable": False,
}

PROMPT = """Ты разбираешь запись исходящего звонка менеджера «тёплого прозвона».
Наша компания — «{own_company}», сдаёт в аренду строительную и грузоподъёмную
технику: экскаваторы, автокраны, автовышки, манипуляторы, самосвалы. Менеджер
обзванивает клиентов, которые обращались раньше, выясняет потребность и заводит
заявку. Упоминание нашего названия в разговоре — это про нас, а не про конкурента.

Расшифровка автоматическая, с телефонной линии: слова бывают искажены, реплики
местами накладываются. Додумывать нельзя. Если разговор не разобрать или он
пустой (не дозвонился, попал не туда, сразу отказ) — поставь "unusable": true
и оставь остальные поля пустыми.

РАСШИФРОВКА (operator — менеджер, client — клиент):
{transcript}

ЧТО МЕНЕДЖЕР ВНЁС В CRM ПОСЛЕ ЭТОГО РАЗГОВОРА:
{card}

Верни JSON:
{{
  "summary": "1-2 предложения: о чём говорили и чем кончилось",
  "client_need": "какая техника нужна клиенту, на какой срок и объект — словами клиента, или пустая строка",
  "need_stated": true/false — прозвучала ли в разговоре хоть какая-то потребность,
  "next_step": "о чём договорились: перезвонить, прислать расчёт, ничего",
  "missed": [
    {{"field": "одно из: {fields}",
      "value": "что именно прозвучало и не попало в карточку",
      "quote": "дословная цитата из расшифровки, подтверждающая это"}}
  ],
  "recommendations": ["что сделать менеджеру по этому клиенту и что исправить в ведении разговора"],
  "call_quality": 1-5 — насколько разговор доведён до результата,
  "quality_notes": "коротко, за что такая оценка"
}}

Что такое "missed". Это сведения, которые **прозвучали в разговоре** и которых
**нет в карточке**. Только так. Примеры годных пунктов:
- клиент сказал «летом брали у вас экскаватор-погрузчик», а в карточке пусто;
- клиент назвал объект «Шушары, на неделю», а поле объектов не заполнено;
- клиент просил перезвонить 22-го, а задачи нет;
- в карточке записано «автовышка», а клиент говорил про самосвал.

Пунктов быть не должно, если сведения в разговоре не прозвучали. «Клиент не
назвал потребность», «менеджер не спросил ИНН», цитата «не прозвучало» — это
не "missed", а повод для "recommendations". Пустой список — нормальный ответ.

Перед каждым пунктом сверься со списком «ЧТО МЕНЕДЖЕР ВНЁС В CRM» выше. Если
сведения там уже есть — пункта быть не должно: компания записана — не пиши про
компанию, задача поставлена — не пиши про перезвон.

Правила:
- "quote" — дословный кусок расшифровки, слово в слово. Переписывать и
  приглаживать нельзя: цитату проверяют по тексту, и непохожий пункт выкинут;
- "recommendations" — не больше трёх, каждая по делу и выполнима: «перезвонить
  22-го, клиент просил», «спросить ИНН», а не «улучшить качество работы»;
- пиши по-русски, коротко, без вводных слов.
"""


def card_summary(call: dict[str, Any]) -> str:
    """Что менеджер внёс в CRM — в виде, понятном модели."""
    lines = [
        f"- потребность в карточке: {call.get('need_value') or 'не заполнена'}",
        f"- компания клиента: {call.get('company_name') or 'не указана'}",
        f"- объекты: {'заполнены' if call.get('objects_filled') else 'не заполнены'}",
        f"- ИНН: {'заполнен' if call.get('inn_filled') else 'не заполнен'}",
    ]
    tasks = call.get("tasks") or []
    if tasks:
        for task in tasks:
            due = (task.get("due_date") or "")[:10]
            lines.append(f"- задача: «{task.get('name')}»" + (f", срок {due}" if due else ""))
    else:
        lines.append("- задач после звонка не поставлено")
    orders = call.get("orders") or []
    for order in orders:
        lines.append(f"- заявка: «{order.get('name')}», стадия {order.get('stage_name')}")
    if not orders:
        lines.append("- заявок по звонку не заведено")
    return "\n".join(lines)


def dialog_text(turns: list[dict[str, Any]], limit: int = 12000) -> str:
    """Реплики в виде «роль: текст». Длинные разговоры обрезаем с конца."""
    lines = [f"{t.get('role', 'speaker')}: {(t.get('text') or '').strip()}" for t in turns]
    text = "\n".join(line for line in lines if line.split(": ", 1)[-1])
    return text[:limit]


def analyze(
    transcript: str, call: dict[str, Any], *, api_key: str, model: str,
    own_company: str = "Техно-Ресурс", timeout_sec: float = 120.0,
) -> dict[str, Any]:
    """Разобрать один разговор. Возвращает словарь по образцу EMPTY_ANALYSIS."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=timeout_sec)
    prompt = PROMPT.format(
        transcript=transcript,
        card=card_summary(call),
        fields=", ".join(MISSED_FIELDS),
        own_company=own_company,
    )
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("разбор вернул не JSON: %s", raw[:200])
        return {**EMPTY_ANALYSIS, "unusable": True}
    return normalize(data, transcript, call)


def words(text: str) -> list[str]:
    """Значащие слова строки — для сверки цитаты с расшифровкой."""
    cleaned = "".join(char if char.isalnum() else " " for char in text.lower())
    return [word for word in cleaned.split() if len(word) > 3]


def quote_found(quote: str, transcript: str, ratio: float = 0.7) -> bool:
    """Есть ли цитата в расшифровке.

    Дословного совпадения не требуем: модель склеивает реплики и правит
    окончания. Но большая часть слов должна найтись в тексте — иначе это не
    цитата, а пересказ или выдумка, и пункту в отчёте не место.
    """
    needed = words(quote)
    if not needed:
        return False
    haystack = set(words(transcript))
    hits = sum(1 for word in needed if word in haystack)
    return hits / len(needed) >= ratio


def already_in_card(field: str, call: dict[str, Any]) -> bool:
    """Заполнено ли это поле в карточке.

    Модель иногда помечает «упущенным» то, что менеджер записал — например
    компанию, которая тут же стоит в карточке. Проверять это по тексту не надо,
    у нас есть сама карточка. Потребность — исключение: в карточке может стоять
    одно, а в разговоре звучать другое, и как раз это нужно показать.
    """
    filled = {
        "компания": bool((call.get("company_name") or "").strip()),
        "инн": bool(call.get("inn_filled")),
        "объекты": bool(call.get("objects_filled")),
        "задача": bool(call.get("tasks")),
    }
    return filled.get(field.strip().lower(), False)


def normalize(
    data: dict[str, Any], transcript: str = "", call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Привести ответ модели к ожидаемому виду.

    Модель иногда отдаёт строку вместо списка, пункт без цитаты или цитату,
    которой в разговоре не было. Такие пункты выбрасываем: по этим строкам
    судят о работе людей, и непроверяемое обвинение хуже, чем его отсутствие.
    """
    out = {**EMPTY_ANALYSIS, **{k: v for k, v in data.items() if k in EMPTY_ANALYSIS}}

    missed = []
    for item in out["missed"] if isinstance(out["missed"], list) else []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not value or not quote:
            continue
        if transcript and not quote_found(quote, transcript):
            logger.info("пункт без подтверждения в записи выброшен: %s", value[:60])
            continue
        field = str(item.get("field") or "прочее").strip()
        if call and already_in_card(field, call):
            logger.info("пункт про заполненное поле «%s» выброшен", field)
            continue
        missed.append({"field": field, "value": value, "quote": quote})
    out["missed"] = missed

    recommendations = out["recommendations"]
    if isinstance(recommendations, str):
        recommendations = [recommendations]
    out["recommendations"] = [
        str(r).strip() for r in (recommendations or []) if str(r).strip()
    ][:3]

    quality = out["call_quality"]
    try:
        out["call_quality"] = min(5, max(1, int(quality))) if quality is not None else None
    except (TypeError, ValueError):
        out["call_quality"] = None

    out["need_stated"] = bool(out["need_stated"])
    out["unusable"] = bool(out["unusable"])
    for key in ("summary", "client_need", "next_step", "quality_notes"):
        out[key] = str(out[key] or "").strip()
    return out
