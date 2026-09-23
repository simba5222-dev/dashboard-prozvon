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

Нужно понять две вещи.

1. О каком типе техники сотрудник спрашивал.
2. Какая техника есть У СОБЕСЕДНИКА — то есть какую технику он называет своей,
   предлагает, говорит что она свободна, занята, в работе или на объекте.

Правила, от которых зависит всё:
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

Список типов техники компании:
{types}

Разговор (Р — сотрудник, К — собеседник; роли могли определиться неточно):
{transcript}

Верни JSON:
{{"asked": "<тип из списка, о котором спрашивал сотрудник, или пусто>",
  "has": [{{"type": "<тип из списка>", "quote": "<цитата до 120 знаков>"}}],
  "more_unnamed": {{"yes": true/false, "quote": "<цитата или пусто>"}},
  "sure": 0-100}}"""


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


def heard_types(transcript: str, allowed: list[str], *, api_key: str, model: str,
                timeout_sec: float = 60.0) -> dict[str, Any]:
    """Что собеседник назвал своим. Только из справочника, только с цитатой."""
    from openai import OpenAI

    if not transcript.strip() or not allowed:
        return {"asked": "", "has": [], "sure": 0}
    client = OpenAI(api_key=api_key, timeout=timeout_sec)
    analyzer.pace_calls()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": OFFER_PROMPT.format(
            types="\n".join(f"- {name}" for name in allowed),
            transcript=transcript[:6000],
        )}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    analyzer.log_usage(response, kind="техника поставщика", model=model)
    try:
        data = json.loads(response.choices[0].message.content or "{}")
    except ValueError:
        return {"asked": "", "has": [], "sure": 0}

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
        seen.add(name)
        has.append({"type": name, "quote": quote})
    asked = known.get(str(data.get("asked") or "").strip().casefold(), "")
    more = data.get("more_unnamed") or {}
    quote = str(more.get("quote") or "").strip()[:120]
    unnamed = bool(more.get("yes")) and bool(quote) and analyzer.quote_found(quote, transcript)
    return {"asked": asked, "has": has, "sure": data.get("sure"),
            "more_unnamed": quote if unnamed else ""}


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
            "missing": missing, "more_unnamed": heard.get("more_unnamed") or ""}


def check_call(conn: sqlite3.Connection, settings: Settings, uid: str, path: Path,
               *, text: str | None = None) -> dict[str, Any]:
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
        # Разговоры здесь короткие, и местная модель на них рассыпается.
        # Подробности и цена — в `app/voice.py` и в настройке.
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
    if not settings.analysis_configured:
        return {"uid": uid, "transcribed": True, "checked": False,
                "note": "ключ OpenAI не задан"}

    heard = heard_types(text, vocabulary(conn), api_key=settings.openai_api_key,
                        model=settings.analysis_model)
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
    )
    conn.commit()
    if result["missing"]:
        logger.info("разговор %s: не занесено — %s", uid, ", ".join(result["missing"]))
    return {"uid": uid, "transcribed": True, "checked": True, **result}
