"""Разбор разговоров отдела поиска техники.

Задача у этого разбора одна и узкая. Поисковик звонит исполнителю из нашей
базы по одному типу техники, а в разговоре всплывает, что у того есть ещё
что-то: «экскаватора нет, но самосвал дам». Дальше это либо попадает в
карточку поставщика, либо теряется — и через месяц мы ищем самосвал у того,
у кого он уже есть.

Поэтому здесь: из разговора достаём только то, что поставщик назвал **своим**,
сверяем с карточками транспорта по его номеру и ставим отметку — всё занесено
или чего не хватает.

**Чего этот разбор не делает.** Он не оценивает, хорошо ли менеджер вёл
разговор, и не ставит оценок. Отметка «не занесено» — это не обвинение:
менеджер мог занести технику позже, чем прошёл разбор. Поэтому сверка
пересчитывается при каждом повторном запуске, и вчерашние отметки обновляются
по сегодняшнему состоянию карточек.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import analyzer, voice
from app.config import Settings
from app.db import phone10, save_search_check, save_transcript
from app.prozvon import transcribe_full

logger = logging.getLogger(__name__)

# Вердикты. Хранятся строкой, потому что по ним считается статистика и
# фильтруется лента; расшифровка — в `VERDICT_LABEL`.
ALL_SAVED = "all_saved"
MISSING = "missing"
NO_CARDS = "no_cards"
UNCLEAR = "unclear"
ASK_MORE = "ask_more"

VERDICT_LABEL = {
    ALL_SAVED: "всё занесено",
    MISSING: "не занесено в карточку",
    NO_CARDS: "поставщика нет в базе",
    UNCLEAR: "техника не прозвучала",
    ASK_MORE: "сказал, что есть ещё — спросить какая",
}

OFFER_PROMPT = """Ты разбираешь телефонный разговор сотрудника компании по аренде
спецтехники с владельцем техники (исполнителем, поставщиком). Сотрудник ищет
технику под заявку и обзванивает базу.

Нужно понять:

1. О каком типе техники сотрудник спрашивал.
2. Какая техника есть У СОБЕСЕДНИКА — то есть какую технику он называет своей,
   предлагает, говорит что она свободна, занята, в работе или на объекте.
3. Представил ли сотрудник компанию собеседнику (назвал её имя, чем занимается,
   что звонит от компании-арендодателя спецтехники) — это не приветствие и не
   «алло», а именно представление компании.
4. Спросил ли сотрудник, какая ЕЩЁ техника есть у собеседника вообще, помимо
   того конкретного типа, по которому звонил — «а что у вас ещё есть», «какой
   у вас парк техники».
5. Какую цену назвал собеседник за свою технику (рубли за смену, час, рейс или
   сутки).
6. Занята ли техника собеседника; если занята — сказал ли он, когда освободится,
   и спросил ли об этом сотрудник.
7. Кто собеседник. «исполнитель» — владелец техники, у которого сотрудник ищет
   технику под заявку. «заказчик» — клиент, которому компания даёт технику:
   сотрудник перезванивает ему по его заявке, называет цену, говорит, что техники
   нет или что все машины заняты, просит «обращайтесь», «если не найдёте — звоните».
   «неясно» — автоответчик, обрыв, не поговорили.

Правила, от которых зависит всё:
- Если собеседник — заказчик или неясно, пункты 2, 5 и 6 оставь пустыми: «занято»,
  «нет техники», «не положим» говорит сам сотрудник от лица своей компании, это не
  техника собеседника.
- Цена — только та, что называет СОБЕСЕДНИК за свою технику. Цену, которую
  предлагает или называет сам сотрудник, не берём. Сумму приводи числом в рублях
  («двадцать пять тысяч» — 25000); если единица не названа — «неизвестно».
- «Занята» — собеседник сказал, что техника в работе, на объекте, занята. В
  `free_from` — когда освободится, его словами («в пятницу», «через два дня»),
  пусто, если не сказал. `manager_asked` — true только если сотрудник сам спросил,
  когда техника освободится. «Свободна» — собеседник сказал, что техника
  свободна и можно брать.
- Берём только технику собеседника. Технику, которую перечисляет сам сотрудник,
  спрашивая о ней, — не берём.
- Если собеседник говорит, что такой техники у него НЕТ, не бывает, продал,
  больше не занимается — такую технику НЕ включаем.
- «Занят», «в работе», «на объекте», «освободится в пятницу» — это техника
  ЕСТЬ, включаем.
- Названия приводим строго к списку ниже, слово в слово. Если сказанное не
  ложится ни на один пункт списка — пропускаем, выдумывать нельзя.
- На каждый тип дай короткую цитату из разговора — ту, по которой ты решил.
  Без цитаты пункт не нужен.
- Отдельно отметь, если собеседник сказал, что у него есть ЕЩЁ техника, но не
  назвал какая: «у нас много разной техники», «не только погрузчики». Это не
  тип, это повод переспросить.
- На пункты 3 и 4 тоже нужна цитата. Без неё считай, что не было.

Список типов техники компании:
{types}

Разговор (Р — сотрудник, К — собеседник; роли могли определиться неточно):
{transcript}

Верни JSON:
{{"asked": "<тип из списка, о котором спрашивал сотрудник, или пусто>",
  "has": [{{"type": "<тип из списка>", "quote": "<цитата до 120 знаков>"}}],
  "more_unnamed": {{"yes": true/false, "quote": "<цитата или пусто>"}},
  "presented": {{"yes": true/false, "quote": "<цитата или пусто>"}},
  "clarified": {{"yes": true/false, "quote": "<цитата или пусто>"}},
  "prices": [{{"type": "<тип из списка>", "amount": <число рублей>,
               "unit": "смена|час|рейс|сутки|неизвестно", "quote": "<цитата>"}}],
  "busy": [{{"type": "<тип из списка>", "state": "занята|свободна",
             "free_from": "<когда освободится или пусто>",
             "manager_asked": true/false, "quote": "<цитата>"}}],
  "counterpart": "исполнитель|заказчик|неясно",
  "sure": 0-100}}"""

PRICE_UNITS = ("смена", "час", "рейс", "сутки", "неизвестно")
BUSY_STATES = ("занята", "свободна")
NO_DATE_WORDS = frozenset({"неизвестно", "не известно", "нет", "-", "—", "не сказал",
                           "не сказали", "не знаю", "не названа", "не указано", "пусто"})


def _amount(value: Any) -> float | None:
    """Сумма из ответа модели. Не число или не больше нуля — не цена."""
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return number if number > 0 else None


def vocabulary(conn: sqlite3.Connection) -> list[str]:
    """Названия типов техники — из справочника карточек, а не из головы.

    Справочник в CRM живой: типы добавляют. Поэтому список берётся из
    синхронизированных карточек, и новый тип появляется здесь сам.
    """
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT type_name FROM transport_cards "
        "WHERE type_name <> '' ORDER BY type_name")]


def supplier_cards(conn: sqlite3.Connection, number: str) -> list[dict[str, Any]]:
    """Карточки транспорта этого поставщика — по десяти цифрам номера."""
    digits = phone10(number)
    if not digits:
        return []
    return [dict(row) for row in conn.execute(
        "SELECT id, name, type_name, status, contact_name, contact_id "
        "FROM transport_cards "
        "WHERE phone10 = ? ORDER BY type_name", (digits,))]


def number_profile(conn: sqlite3.Connection, number: str,
                   with_orders: set[str] | None = None) -> str:
    """Что мы знаем про номер: техника в базе и был ли он заказчиком.

    Нужна просеву входящих. Владелец 23.09.2026 предложил исключать из
    прослушки контакты-исполнителей — на данных это оказалось опасно: из 67
    пойманных заявок 16 пришли с номеров, у которых есть карточки техники,
    и в них люди просят технику у нас. Поэтому признак идёт подсказкой, а не
    запретом: разбор видит факт и решает по разговору.
    """
    cards = supplier_cards(conn, number)
    if not cards:
        return ""
    types = sorted({card["type_name"] for card in cards if card["type_name"]})
    who = cards[0]["contact_name"] or "без имени"
    part = (f"номер есть в разделе «Транспорт»: {who}, карточек {len(cards)}"
            + (f" ({', '.join(types[:5])})" if types else ""))
    contact_id = str(cards[0].get("contact_id") or "")
    if with_orders is not None and contact_id:
        part += ("; у этого контакта есть заявки — он бывал и заказчиком"
                 if contact_id in with_orders else "; заявок у контакта нет")
    return part


def contacts_with_orders(path: Path) -> set[str]:
    """Кто из контактов хоть раз был заказчиком. Собирает classify_contacts.py."""
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _counterpart_text(transcript: str) -> str:
    """Только реплики собеседника — по подписи стороны в расшифровке.

    Пусто, если подписей нет: тогда проверять нечем и цитату ищем по всему тексту.
    """
    parts = []
    for line in transcript.splitlines():
        head, _, body = line.partition(":")
        if head.strip().casefold() in ("клиент", "client", "к", "собеседник"):
            parts.append(body.strip())
    return "\n".join(parts)


def heard_types(transcript: str, allowed: list[str], *, api_key: str, model: str,
                timeout_sec: float = 60.0, engine: str = "openai",
                folder: str = "", require_supplier: bool = False) -> dict[str, Any]:
    """Что собеседник назвал своим. Только из справочника, только с цитатой.

    Голова — через общий диспетчер `_спросить_голову`, как у потока 3.
    Переведено на Яндекс 07.10.2026: расшифровка здесь и так уже идёт через
    общий сервис (на Яндексе с 29.09.2026), а этот последний кусок зависел
    от OpenAI отдельно и падал молча при его недоступности.
    """
    if not transcript.strip() or not allowed:
        return {"asked": "", "has": [], "sure": 0, "presented": "", "clarified": "",
                "prices": [], "busy": []}
    if require_supplier and len(_counterpart_text(transcript).strip()) < 15:
        # Собеседник почти не говорил (автоответчик, обрыв, «алло»): модель не нужна.
        # Отсев до обращения к платной модели — экономия, 16.11.2026.
        return {"asked": "", "has": [], "sure": 0, "more_unnamed": "", "presented": "",
                "clarified": "", "prices": [], "busy": [], "counterpart": "неясно"}
    types = "\n".join(f"- {name}" for name in allowed)
    if engine == "claude":
        # Правила и справочник — одним неизменным блоком с кэшем, разговор — отдельно.
        static = OFFER_PROMPT.format(types=types, transcript="(разговор — в сообщении пользователя)")
        static += ("\n\nОтвет — ТОЛЬКО JSON, одной строкой, без пояснений. Пустые поля, "
                   "пустые списки и поле sure не пиши: каждый лишний токен стоит денег.")
        data = analyzer.спросить_claude_с_кэшем(
            static, "Разговор (Р — сотрудник, К — собеседник; роли могли определиться "
                    "неточно):\n" + transcript[:6000],
            api_key=api_key, model=model, kind="техника поставщика", max_tokens=700,
            timeout_sec=timeout_sec)
    else:
        prompt = OFFER_PROMPT.format(types=types, transcript=transcript[:6000])
        data = analyzer._спросить_голову(prompt, engine=engine, api_key=api_key, model=model,
                                          folder=folder, timeout_sec=timeout_sec,
                                          kind="техника поставщика")
    if data is None:
        return {"asked": "", "has": [], "sure": 0, "presented": "", "clarified": "",
                "prices": [], "busy": []}

    counterpart = str(data.get("counterpart") or "").strip().casefold()
    if require_supplier and counterpart != "исполнитель":
        # Новый канал (менеджеры продаж): их исходящие бывают и исполнителям, и
        # заказчикам, которым менеджер перезванивает по заявке. «Занято» и «нет
        # техники» там говорит сам менеджер — приписывать это собеседнику и
        # заводить его в базу как поставщика нельзя (пробный прогон 15.11.2026).
        return {"asked": "", "has": [], "sure": data.get("sure"), "more_unnamed": "",
                "presented": "", "clarified": "", "prices": [], "busy": [],
                "counterpart": counterpart or "неясно"}

    # Что сказал именно собеседник: тип, цена и «занято» — его слова, а не менеджера.
    side = _counterpart_text(transcript) if require_supplier else ""

    def said_by_counterpart(quote_text: str) -> bool:
        return not side or analyzer.quote_found(quote_text, side)

    known = {name.casefold(): name for name in allowed}
    has: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in data.get("has") or []:
        if not isinstance(item, dict):
            continue
        name = known.get(str(item.get("type") or "").strip().casefold())
        quote = str(item.get("quote") or "").strip()[:120]
        # Тип не из справочника или без цитаты — выбрасываем. По этим отметкам
        # человек будет править карточки, и придуманный тип дороже пропущенного.
        if not name or name in seen or not quote:
            continue
        if not analyzer.quote_found(quote, transcript):
            logger.info("цитата не найдена в разговоре, пропускаю: %s", quote[:60])
            continue
        if not said_by_counterpart(quote):
            logger.info("цитата из реплики менеджера, не собеседника, пропускаю: %s", quote[:60])
            continue
        seen.add(name)
        has.append({"type": name, "quote": quote})
    asked = known.get(str(data.get("asked") or "").strip().casefold(), "")
    more = data.get("more_unnamed") or {}
    quote = str(more.get("quote") or "").strip()[:120]
    unnamed = bool(more.get("yes")) and bool(quote) and analyzer.quote_found(quote, transcript)

    def flag_quote(key: str) -> str:
        # «Да» засчитываем только с цитатой, которая вправду есть в тексте —
        # как и у остальных пунктов: придуманное «да» дороже пропущенного.
        item = data.get(key) or {}
        text = str(item.get("quote") or "").strip()[:120]
        ok = bool(item.get("yes")) and bool(text) and analyzer.quote_found(text, transcript)
        return text if ok else ""

    # Цена и занятость — по тем же правилам, что и тип: из справочника, с цитатой,
    # которая вправду есть в разговоре. Цена без числа или без цитаты не нужна:
    # по ней будут судить о ставках поставщиков.
    prices: list[dict[str, Any]] = []
    for item in data.get("prices") or []:
        if not isinstance(item, dict):
            continue
        name = known.get(str(item.get("type") or "").strip().casefold())
        quote_p = str(item.get("quote") or "").strip()[:120]
        amount = _amount(item.get("amount"))
        if not name or not quote_p or amount is None:
            continue
        if not analyzer.quote_found(quote_p, transcript) or not said_by_counterpart(quote_p):
            continue
        unit = str(item.get("unit") or "").strip().casefold()
        prices.append({"type": name, "amount": amount,
                       "unit": unit if unit in PRICE_UNITS else "неизвестно",
                       "quote": quote_p})
    busy: list[dict[str, Any]] = []
    for item in data.get("busy") or []:
        if not isinstance(item, dict):
            continue
        name = known.get(str(item.get("type") or "").strip().casefold())
        quote_b = str(item.get("quote") or "").strip()[:120]
        state = str(item.get("state") or "").strip().casefold()
        if not name or not quote_b or state not in BUSY_STATES:
            continue
        if not analyzer.quote_found(quote_b, transcript) or not said_by_counterpart(quote_b):
            continue
        free_from = str(item.get("free_from") or "").strip()[:80]
        # Модель любит писать слово вместо пустоты: «неизвестно» — не дата.
        if free_from.casefold() in NO_DATE_WORDS:
            free_from = ""
        busy.append({"type": name, "state": state,
                     "free_from": free_from,
                     "manager_asked": bool(item.get("manager_asked")),
                     "quote": quote_b})

    return {"asked": asked, "has": has, "sure": data.get("sure"),
            "more_unnamed": quote if unnamed else "",
            "presented": flag_quote("presented"), "clarified": flag_quote("clarified"),
            "prices": prices, "busy": busy, "counterpart": counterpart}


def compare(heard: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Сверить услышанное с карточками. Возвращает вердикт и списки."""
    known = {str(card["type_name"]) for card in cards if card.get("type_name")}
    offered = [item["type"] for item in heard.get("has") or []]
    missing = [name for name in offered if name not in known]
    if not cards:
        verdict = NO_CARDS
    elif not offered:
        # «У нас много разной техники» — типов не назвал, но техника есть.
        # Для менеджера это работа: перезвонить и переписать карточку.
        verdict = ASK_MORE if heard.get("more_unnamed") else UNCLEAR
    elif missing:
        verdict = MISSING
    else:
        verdict = ALL_SAVED
    return {"verdict": verdict, "offered": offered, "known": sorted(known),
            "missing": missing, "more_unnamed": heard.get("more_unnamed") or "",
            "presented": heard.get("presented") or "",
            "clarified": heard.get("clarified") or ""}


def check_call(conn: sqlite3.Connection, settings: Settings, uid: str, path: Path,
               *, text: str | None = None, require_supplier: bool = False) -> dict[str, Any]:
    """Расшифровать разговор поисковика и сверить технику с карточками.

    Расшифровка сохраняется до разбора: если разбор упадёт, текст останется.
    Так же устроен разбор прозвона — расходиться им нельзя.
    """
    now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731
    row = conn.execute(
        "SELECT uid, client_phone, vats_login, local_date, duration_sec "
        "FROM calls WHERE uid = ?", (uid,)).fetchone()
    if row is None:
        return {"uid": uid, "note": "звонка нет в базе"}
    call = dict(row)

    if not text:
        # Распознаём тем же сервисом, что и всё остальное. До 29.09.2026 здесь
        # стоял отдельный путь через whisper-1: местная модель на коротких
        # разговорах подборщика рассыпалась, и ради качества звук уходил в
        # OpenAI — единственное место, где он покидал наши серверы. Теперь
        # сервис ходит в Яндекс, который на названиях техники точнее обоих,
        # и отдельный путь стал лишним. Настройка `search_voice_model`
        # оставлена: если понадобится вернуть whisper-1, она всё ещё работает.
        if settings.search_voice_model and settings.analysis_configured:
            turns = voice.transcribe(path, api_key=settings.openai_api_key,
                                     model=settings.search_voice_model)
        else:
            turns = (transcribe_full(path, settings.asr_url,
                                     settings.asr_timeout_sec).get("dialog") or [])
        text = analyzer.dialog_text(turns)
        save_transcript(conn, call_uid=uid, text=text, analysis_json=None,
                        created_at=now(), is_demo=0)
        conn.commit()
    if not (text or "").strip():
        return {"uid": uid, "transcribed": True, "checked": False,
                "note": "в записи нет речи"}
    # Канал менеджеров продаж (require_supplier) отличает исполнителя от заказчика — Яндекс Lite
    # на этом ошибается (пробный прогон 15.11.2026), поэтому там Claude, если он включён.
    claude_engine = (require_supplier and settings.search_sales_engine == "claude"
                     and bool(settings.anthropic_api_key))
    engine = "claude" if claude_engine else settings.search_engine
    yandex_engine = engine == "yandex"
    if claude_engine:
        pass
    elif yandex_engine:
        if not settings.yandex_stt_configured:
            return {"uid": uid, "transcribed": True, "checked": False,
                    "note": "ключ Яндекса не задан"}
    elif not settings.analysis_configured:
        return {"uid": uid, "transcribed": True, "checked": False,
                "note": "ключ OpenAI не задан"}

    if claude_engine:
        key, model_name = settings.anthropic_api_key or "", settings.anthropic_model
    elif yandex_engine:
        key, model_name = settings.yandex_api_key or "", settings.yandex_model
    else:
        key, model_name = settings.openai_api_key or "", settings.analysis_model
    heard = heard_types(
        text, vocabulary(conn),
        api_key=key, model=model_name,
        engine=engine, folder=settings.yandex_folder or "",
        require_supplier=require_supplier,
    )
    cards = supplier_cards(conn, call.get("client_phone"))
    result = compare(heard, cards)

    save_transcript(conn, call_uid=uid, text=text,
                    analysis_json=json.dumps({"search": heard}, ensure_ascii=False),
                    created_at=now(), is_demo=0)
    save_search_check(
        conn,
        call_uid=uid,
        local_date=call.get("local_date") or "",
        vats_login=call.get("vats_login") or "",
        phone10=phone10(call.get("client_phone")),
        contact_name=(cards[0]["contact_name"] if cards else ""),
        asked_type=heard.get("asked") or "",
        offered=json.dumps(heard.get("has") or [], ensure_ascii=False),
        known=json.dumps(result["known"], ensure_ascii=False),
        missing=json.dumps(result["missing"], ensure_ascii=False),
        verdict=result["verdict"],
        checked_at=now(),
        is_demo=0,
        more_unnamed=result.get("more_unnamed") or "",
        presented=result.get("presented") or "",
        clarified=result.get("clarified") or "",
        prices_json=json.dumps(heard.get("prices") or [], ensure_ascii=False),
        busy_json=json.dumps(heard.get("busy") or [], ensure_ascii=False),
    )
    conn.commit()
    if result["missing"]:
        logger.info("разговор %s: не занесено — %s", uid, ", ".join(result["missing"]))
    return {"uid": uid, "transcribed": True, "checked": True, "heard": heard, **result}
