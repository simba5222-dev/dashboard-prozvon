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

from app import knowledge

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
    # Из учебника: какая техника обсуждалась, каких обязательных вопросов
    # менеджер не задал и какую сопутствующую технику не предложил.
    "equipment": "",
    "questions_missed": [],
    "upsell_missed": [],
}

PROMPT = """Ты разбираешь запись исходящего звонка менеджера «тёплого прозвона».
Наша компания — «{own_company}». Менеджер обзванивает клиентов, которые
обращались раньше, выясняет потребность и заводит заявку. Упоминание нашего
названия в разговоре — это про нас, а не про конкурента.

{domain}

ЧЕК-ЛИСТ ПО ЭТОМУ РАЗГОВОРУ (если техника угадана по словам клиента):
{checklist}

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
  "quality_notes": "коротко, за что такая оценка",
  "equipment": "какая техника обсуждалась, нашими словами: автовышка, гусеничный экскаватор, пухто; пусто, если речи о технике не было",
  "questions_missed": ["обязательные вопросы из чек-листа, которые менеджер не задал, хотя техника уже была названа"],
  "upsell_missed": ["какую сопутствующую технику стоило предложить и не предложил"]
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
- "questions_missed" и "upsell_missed" заполняй, только когда техника в разговоре
  названа. Клиент сказал «ничего не нужно» — списки пустые, придираться не за что;
- пиши по-русски, коротко, без вводных слов.
"""


EMPTY_VERDICT: dict[str, Any] = {
    "outcome": "",
    "client_position": "",
    "manager_actions": [],
    "gaps": [],
    "recommendations": [],
    "recoverable": None,
    "no_calls": False,
}

ORDER_PROMPT = """Ты разбираешь, почему заявка на аренду техники не дошла до сделки.
Наша компания — «{own_company}». Заявку завёл менеджер тёплого прозвона, дальше
её ведёт ответственный: он созванивается с клиентом, считает стоимость,
согласовывает технику и сроки.

{domain}

ЗАЯВКА:
{order}

РАЗГОВОРЫ С КЛИЕНТОМ ПОСЛЕ СОЗДАНИЯ ЗАЯВКИ (в порядке времени):
{calls}

Расшифровки автоматические, с телефонной линии, местами рвутся. Додумывать
нельзя: пиши только то, что видно из разговоров и полей заявки. Если разговоров
нет вовсе — так и скажи, поставь "no_calls": true и не выдумывай причин.

Верни JSON:
{{
  "outcome": "почему заявка не стала сделкой — 1-2 предложения, по существу",
  "client_position": "что говорил клиент: цена, сроки, нашёл другого, передумал, не берёт трубку",
  "manager_actions": ["что ответственный действительно сделал: перезвонил, посчитал, предложил замену"],
  "gaps": ["чего он не сделал, хотя разговор этого требовал"],
  "recommendations": ["что сделать сейчас, чтобы вернуть клиента — не больше трёх"],
  "recoverable": true/false — есть ли смысл возвращаться к этому клиенту
}}

Правила:
- «клиент не взял трубку» и «менеджер не перезвонил» — разные вещи, не путай:
  первое видно по недозвонам, второе — по отсутствию звонков вообще;
- не повторяй в "gaps" то, что уже написал в "outcome";
- пиши по-русски, коротко, без вводных слов.
"""


def order_summary(order: dict[str, Any]) -> str:
    """Заявка в виде, понятном модели."""
    lines = [
        f"- заявка №{order.get('number') or order.get('order_id')}: {order.get('name')}",
        f"- создана: {(order.get('created_at') or '')[:16].replace('T', ' ')}",
        f"- ответственный: {order.get('responsible') or 'не назначен'}",
        f"- стадия сейчас: {order.get('stage_name') or 'неизвестна'}",
    ]
    if order.get("amount"):
        lines.append(f"- сумма: {order['amount']}")
    for key in ("description", "note", "address", "client_need"):
        if order.get(key):
            lines.append(f"- {key}: {order[key]}")
    return "\n".join(lines)


def calls_summary(calls: list[dict[str, Any]], limit: int = 14000) -> str:
    """Звонки с клиентом: кто, когда, чем кончился, о чём говорили."""
    blocks = []
    for call in calls:
        who = call.get("vats_login") or "неизвестно"
        side = "менеджер звонил" if call.get("direction") == "out" else "клиент звонил сам"
        head = (f"[{(call.get('started_at') or '')[:16].replace('T', ' ')}] {side}, "
                f"{who}, {call.get('duration_sec', 0)} с")
        if (call.get("duration_sec") or 0) < 15:
            blocks.append(f"{head} — не поговорили")
            continue
        text = (call.get("transcript_text") or "").strip()
        blocks.append(f"{head}\n{text}" if text else f"{head} — записи нет")
    joined = "\n\n".join(blocks)
    return joined[:limit] if joined else "разговоров с клиентом после создания заявки не найдено"


def analyze_order(
    order: dict[str, Any], calls: list[dict[str, Any]], *, api_key: str, model: str,
    own_company: str = "Техно-Ресурс", timeout_sec: float = 180.0,
) -> dict[str, Any]:
    """Разобрать судьбу заявки по звонкам после её создания."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=timeout_sec)
    prompt = ORDER_PROMPT.format(
        own_company=own_company,
        domain=knowledge.DOMAIN,
        order=order_summary(order),
        calls=calls_summary(calls),
    )
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(response.choices[0].message.content or "{}")
    except ValueError:
        return {**EMPTY_VERDICT, "no_calls": not calls}
    return normalize_verdict(data)


def normalize_verdict(data: dict[str, Any]) -> dict[str, Any]:
    out = {**EMPTY_VERDICT, **{k: v for k, v in data.items() if k in EMPTY_VERDICT}}
    for key in ("manager_actions", "gaps", "recommendations"):
        value = out[key]
        if isinstance(value, str):
            value = [value]
        out[key] = [str(item).strip() for item in (value or []) if str(item).strip()][:3]
    for key in ("outcome", "client_position"):
        out[key] = str(out[key] or "").strip()
    if out["recoverable"] is not None:
        out["recoverable"] = bool(out["recoverable"])
    out["no_calls"] = bool(out["no_calls"])
    return out


EMPTY_SCREEN: dict[str, Any] = {
    "is_request": False,
    "equipment": "",
    "request": "",
    "quote": "",
    "about_existing": False,
    # caller — просит позвонивший, our_manager — технику ищет наш менеджер.
    "asked_by": "",
    "manager_side": "",
    "confidence": 0,
}

SCREEN_PROMPT = """Ты просматриваешь начало входящего звонка менеджеру компании
«{own_company}» — она сдаёт технику в аренду. Вопрос ровно один: **просит ли
технику позвонивший**, то есть спрашивает наличие, цену или сроки аренды.

**Кто спрашивает — решающее.** В расшифровке две стороны, «A» и «B», и кто из
них наш менеджер, заранее неизвестно: запись двухканальная, но каналы у
входящих разложены по-разному. Определи это сам по содержанию:

- наш менеджер отвечает на звонок, говорит от лица компании, называет цены и
  наличие, обещает посмотреть и перезвонить;
- позвонивший просит технику, называет объект и сроки, спрашивает стоимость.

Заявка бывает только тогда, когда технику просит **позвонивший**. Наш менеджер
сам постоянно ищет технику у подрядчиков: если про машину спрашивает он —
это наш поиск, а не заказ.

{domain}

Запросом НЕ считается:
- разговор по уже идущей аренде: где машина, когда приедет, почему опаздывает;
- документы, счета, оплата, акты;
- звонок не по адресу, ошибка номером, личный разговор;
- предложение услуг нам (продавцы, реклама);
- наш менеджер ищет технику у поставщика.

ОТКРЫТЫЕ ЗАЯВКИ ЭТОГО КЛИЕНТА (по ним он может звонить, и это не новый запрос):
{active}

РАСШИФРОВКА НАЧАЛА РАЗГОВОРА (распознана начерно, слова искажены):
{transcript}

Верни JSON:
{{
  "is_request": true/false — просит ли технику позвонивший,
  "equipment": "какая техника, нашими словами; пусто, если не названа",
  "request": "суть просьбы одной строкой: что, куда, когда",
  "quote": "дословный кусок расшифровки, где это слышно",
  "about_existing": true/false — разговор об уже открытой заявке из списка выше,
  "manager_side": "A" или "B" — какая сторона наш менеджер; пусто, если не понять,
  "asked_by": "caller" — просит позвонивший, "our_manager" — технику ищет наш
      менеджер, пусто — про технику речи не было,
  "confidence": 0-100 — насколько уверен
}}

Расшифровка черновая: если разобрать нельзя — "is_request": false и
"confidence" пониже. Выдумывать запрос нельзя, цитата обязана быть из текста.
"""


def screen_call(
    transcript: str, active_orders: str, *, api_key: str, model: str,
    own_company: str = "Техно-Ресурс", timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Быстрый просев: есть ли в разговоре запрос на технику.

    Работает по черновой расшифровке начала разговора — полное распознавание
    всего потока входящих не помещается в сутки, а запрос звучит в первую
    минуту. Дальше в полный разбор уходят только те, где просев нашёл запрос.
    """
    from openai import OpenAI

    if not transcript.strip():
        return {**EMPTY_SCREEN}
    client = OpenAI(api_key=api_key, timeout=timeout_sec)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": SCREEN_PROMPT.format(
            own_company=own_company, domain=knowledge.DOMAIN, transcript=transcript[:6000],
            active=active_orders or "открытых заявок нет",
        )}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(response.choices[0].message.content or "{}")
    except ValueError:
        return {**EMPTY_SCREEN}
    out = {**EMPTY_SCREEN, **{k: v for k, v in data.items() if k in EMPTY_SCREEN}}
    out["is_request"] = bool(out["is_request"])
    out["about_existing"] = bool(out["about_existing"])
    try:
        out["confidence"] = max(0, min(100, int(out["confidence"])))
    except (TypeError, ValueError):
        out["confidence"] = 0
    for key in ("equipment", "request", "quote", "asked_by", "manager_side"):
        out[key] = str(out[key] or "").strip()
    # Технику ищет и наш менеджер — у подрядчиков, под чужую заявку. Такой
    # разговор заказом не является, как бы уверенно модель ни отвечала.
    # И если она не поняла, кто из сторон менеджер, заявку заводить нельзя:
    # роли по номеру канала у входящих определить невозможно.
    if out["asked_by"] != "caller" or not out["manager_side"]:
        out["is_request"] = False
    # Цитата обязана быть из расшифровки — то же правило, что и в разборе.
    if out["is_request"] and out["quote"] and not quote_found(out["quote"], transcript, 0.6):
        logger.info("просев: цитата не найдена в записи, запрос снят")
        out["is_request"] = False
    return out


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
    # Чек-лист подбираем по словам самого разговора и по потребности из
    # карточки: весь учебник в запрос не влезет, да и не нужен — вопросы к
    # автовышке ничего не говорят о разборе заявки на самосвал.
    hints = f"{transcript}\n{call.get('need_value') or ''}"
    questions = knowledge.questions_for(hints)
    upsell = knowledge.upsell_for(hints)
    checklist = "\n".join(f"- {q}" for q in questions) or "- техника в разговоре не названа"
    if upsell:
        checklist += "\n\nЧто к этой технике обычно предлагают:\n" + "\n".join(
            f"- {item}" for item in upsell)
    prompt = PROMPT.format(
        transcript=transcript,
        card=card_summary(call),
        fields=", ".join(MISSED_FIELDS),
        own_company=own_company,
        domain=knowledge.DOMAIN,
        checklist=checklist,
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

    for key in ("recommendations", "questions_missed", "upsell_missed"):
        value = out[key]
        if isinstance(value, str):
            value = [value]
        out[key] = [str(item).strip() for item in (value or []) if str(item).strip()][:3]

    quality = out["call_quality"]
    try:
        out["call_quality"] = min(5, max(1, int(quality))) if quality is not None else None
    except (TypeError, ValueError):
        out["call_quality"] = None

    out["need_stated"] = bool(out["need_stated"])
    out["unusable"] = bool(out["unusable"])
    for key in ("summary", "client_need", "next_step", "quality_notes", "equipment"):
        out[key] = str(out[key] or "").strip()
    return out
