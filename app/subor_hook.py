"""Поток 4+: по вебхуку Synergy (этап заявки сменился на «Нужен Подбор»,
id 5646) — ищем в телеграм-группах «СПБ техника» за 2 недели, кто писал о
подходящей технике, и постим готовый список менеджеру в группу «Подбор
техники» (её id ниже).

Вебхук Synergy настроен в самом кабинете (Автоматизации → Сценарии), шлёт
POST JSON при изменении заявки -- условие "заявка изменена" общее, не только
про этот этап, поэтому здесь сами проверяем текущий этап через API, а не
верим телу запроса вслепую.

Сессия Telegram общая с contacts-recovery/adminlog -- клиентский API,
подключённый 06.10.2026, полный доступ дан владельцем явно.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
STATE_FILE = HERE / "scripts" / "sibiryakov-import" / "subor_hook_state.json"
SESSION_PATH = "/home/claude/telegram/tools/session"
TG_STATE = "/home/claude/telegram/tools/state.json"

STAGE_ID = "5646"
GROUP_ID = 4348457074
LOOKBACK_DAYS = 7  # сужено по слову владельца 21.10.2026 (было 14)
SYNERGY_ORDER_URL = "https://app.synergycrm.ru/orders/"
STOPWORDS = {"техника", "аренда", "спб", "и", "в", "на", "с", "до", "от", "из", "для", "по"}

PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s\-\(]?\d{3}[\s\-\)]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)")

# Решение владельца 21.10.2026: вместо широкого набора слов-маркеров --
# только прямые фразы "свободен/освободился/готов к работе" и их обычные
# падежи/роды. Жёстче, чем раньше, но то, что нужно -- точность важнее охвата.
OFFER_HINTS = (
    "свободен", "свободна", "свободно", "свободны",
    "освободился", "освободилась", "освободились",
    "готов к работе", "готова к работе", "готовы к работе",
)


def classify_message(text: str) -> str:
    low = text.lower()
    return "offer" if any(h in low for h in OFFER_HINTS) else "neutral"


def extract_equipment_phrase(text: str, keywords: list[str]) -> str:
    """Строка объявления, где встретилось искомое слово техники -- то, что
    нужно показать менеджеру как «тип транспорта», без остального текста."""
    for raw_line in text.split("\n"):
        line = raw_line.strip(" \t•-–—🔹🔸📍📅🚜🚩💸💬📞🔥✍️.,")
        if line and any(kw in line.lower() for kw in keywords):
            line = PHONE_RE.sub("", line).strip(" .,")
            if line:
                return line[:80]
    return ""


# Геокодирование -- API Геокодера Яндекс Карт, ключ получен 21.10.2026
# отдельно от ключа SpeechKit (это разные продукты, один ключ для другого не
# годится). Бесплатный лимит 1000 запросов/день, этого достаточно.
#
# 21.10.2026: сначала пробовали бесплатный Nominatim (OpenStreetMap) без
# ключа -- словил 429 (бан по частоте) уже на тестах из пары заявок, и хуже
# разбирал короткие ориентиры без адресного формата ("Петровский стадион").
# Группы "СПБ техника" почти все про Петербург/Ленобласть, поэтому сначала
# ищем в привязке к региону (rspn=1), без неё совпадают случайные
# одноимённые места по всей стране. Если в привязке не нашлось (объявление
# explicitно про другой регион, как Краснодарский край) -- пробуем без границ.
SPB_LO_CENTER = "30.3,59.5"  # lon,lat
SPB_LO_SPAN = "6,6"
RADIUS_KM = 200


# Объявления построены построчно (техника / дата / место / цена / телефон),
# но порядок строк не фиксирован. Geocoder не берёт весь текст одним комком --
# строки-боковой шум (ндс, смена, телефон) сбивают поиск. Решаем по строкам:
# вычищаем заведомо не-адресные, остальные пробуем по одной, предпочитая те,
# где есть явный адресный маркер.
LOCATION_MARKERS = (
    "м.", "метро", "обл", "район", "р-н", "г.", "город", "ул.", "улица",
    "пр-кт", "проспект", "ш.", "шоссе", "дер.", "деревня", "пос.", "посёлок",
    "село", "область", "край",
)
NOT_LOCATION_WORDS = (
    "ндс", "нал", "безнал", "смена", "смены", "суток", "сутки", "оплата",
    "вахтовка", "питание", "жилье", "жильё", "водитель", "рф", "бн", "лс",
    "срочно", "сейчас", "завтра", "сегодня", "неделя", "недели", "час", "часов",
)


def extract_location_candidates(text: str) -> list[str]:
    lines = [ln.strip(" \t•-–—🔹🔸📍📅🚜🚩💸💬📞🔥✍️.,") for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    scored = []
    for ln in lines:
        low = ln.lower()
        if PHONE_RE.search(ln):
            continue
        if any(w in low for w in NOT_LOCATION_WORDS):
            continue
        if len(ln) < 3 or len(ln) > 50:
            continue
        score = 1 if any(m in low for m in LOCATION_MARKERS) else 0
        scored.append((score, ln))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [ln for _, ln in scored]


async def _geocode_once(query: str, http, api_key: str, bounded: bool) -> tuple[float, float] | None:
    params = {"apikey": api_key, "format": "json", "geocode": query, "results": 1, "lang": "ru_RU"}
    if bounded:
        params.update({"ll": SPB_LO_CENTER, "spn": SPB_LO_SPAN, "rspn": 1})
    r = await http.get("https://geocode-maps.yandex.ru/1.x", params=params)
    if r.status_code != 200:
        logger.warning("геокодер Яндекса ответил %s по %r: %s", r.status_code, query[:60], r.text[:200])
        return None
    data = r.json()
    members = (((data.get("response") or {}).get("GeoObjectCollection") or {}).get("featureMember") or [])
    if not members:
        return None
    pos = members[0]["GeoObject"]["Point"]["pos"]  # "lon lat"
    lon, lat = (float(x) for x in pos.split())
    return lat, lon


async def geocode(query: str, http, api_key: str) -> tuple[float, float] | None:
    """Полный российский адрес (улица, посёлок, район, область) иногда не
    берётся целиком -- слишком специфичный хвост (садовое товарищество,
    номер участка) сбивает поиск. Пробуем целиком, потом отбрасываем от
    начала по одному куску через запятую (самое специфичное -- в начале
    адреса), пока не найдётся совпадение хотя бы по общей части (район/
    область)."""
    if not query:
        return None
    parts = [p.strip() for p in query.split(",") if p.strip()]
    candidates = [query] + [", ".join(parts[i:]) for i in range(1, len(parts))]
    for candidate in dict.fromkeys(candidates):  # без повторов, порядок сохранён
        try:
            point = await _geocode_once(candidate, http, api_key, bounded=True)
            if point is None:
                point = await _geocode_once(candidate, http, api_key, bounded=False)
            if point:
                return point
        except Exception as e:
            logger.warning("геокодер не ответил по %r: %s", candidate[:60], e)
    return None


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    from math import asin, cos, radians, sin, sqrt

    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


async def filter_by_distance(hits: list[dict], order_address: str, api_key: str) -> tuple[list[dict], str]:
    """Возвращает (отфильтрованные хиты, пояснение). Если адрес заявки не
    геокодировался -- фильтр не применяем вовсе, честно говорим об этом,
    а не тихо показываем всё как будто проверили."""
    import httpx

    async with httpx.AsyncClient(timeout=10) as http:
        order_point = await geocode(order_address, http, api_key)
        if not order_point:
            return hits, "адрес заявки не удалось определить — фильтр по расстоянию не применён"

        kept = []
        unknown = 0
        for h in hits:
            point = None
            for candidate in extract_location_candidates(h["text"])[:3]:
                point = await geocode(candidate, http, api_key)
                if point:
                    break
            if point is None:
                unknown += 1
                h["distance_km"] = None
                kept.append(h)  # не можем проверить -- не отбрасываем вслепую
                continue
            dist = haversine_km(order_point, point)
            h["distance_km"] = round(dist)
            if dist <= RADIUS_KM:
                kept.append(h)
        note = f"радиус {RADIUS_KM} км от объекта заявки"
        if unknown:
            note += f", у {unknown} сообщений место не определилось (оставлены как есть)"
        return kept, note

logger = logging.getLogger("subor_hook")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_order_id(payload: dict) -> str | None:
    """Форма тела вебхука у Synergy не документирована -- проверяем несколько
    правдоподобных мест, а не одну."""
    for path in (
        ("id",), ("order_id",), ("order", "id"), ("data", "id"),
        ("data", "attributes", "id"), ("object", "id"),
    ):
        node = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node:
            return str(node)
    return None


def keywords_from_type(type_text: str) -> list[str]:
    words = re.findall(r"[а-яёa-z]{4,}", type_text.lower())
    return [w for w in words if w not in STOPWORDS][:3] or [type_text.lower().strip()]


async def search_groups(keywords: list[str]) -> list[dict]:
    from datetime import datetime, timedelta, timezone

    from telethon import TelegramClient
    from telethon.tl.functions.messages import GetDialogFiltersRequest

    tg_state = json.loads(Path(TG_STATE).read_text(encoding="utf-8"))
    client = TelegramClient(SESSION_PATH, tg_state["api_id"], tg_state["api_hash"])
    await client.connect()
    try:
        result = await client(GetDialogFiltersRequest())
        filters = result.filters if hasattr(result, "filters") else result
        folder = None
        for f in filters:
            title = getattr(f, "title", None)
            name = title.text if hasattr(title, "text") else (title or "")
            if "техник" in name.lower():
                folder = f
                break
        if folder is None:
            return []

        since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        hits = []
        for peer in folder.include_peers:
            try:
                entity = await client.get_entity(peer)
            except Exception:
                continue
            async for msg in client.iter_messages(entity, limit=600):
                if msg.date < since:
                    break
                raw_text = msg.text or ""
                text = raw_text.lower()
                if not any(kw in text for kw in keywords):
                    continue
                if classify_message(raw_text) != "offer":
                    continue  # нет прямого "свободен/освободился/готов к работе"
                phone_m = PHONE_RE.search(raw_text)
                sender = await msg.get_sender()
                name = (getattr(sender, "first_name", "") or "")
                if getattr(sender, "last_name", None):
                    name += " " + sender.last_name
                username = getattr(sender, "username", None)
                hits.append({
                    "group": getattr(entity, "title", str(entity.id)),
                    "date": msg.date.isoformat(),
                    "sender": name.strip() or username or str(getattr(sender, "id", "?")),
                    "username": username,
                    "phone": phone_m.group(0) if phone_m else None,
                    "equipment": extract_equipment_phrase(raw_text, keywords),
                    "text": raw_text[:400],
                    "link": f"https://t.me/{username}" if username else None,
                })
        return dedupe_hits(hits)
    finally:
        await client.disconnect()


def dedupe_hits(hits: list[dict]) -> list[dict]:
    """Один и тот же номер (а чаще всего и тот же текст) расходится сразу по
    нескольким группам -- оставляем самое свежее сообщение, остальные
    вхождения просто считаем."""
    def normalize_phone(p: str | None) -> str | None:
        if not p:
            return None
        digits = re.sub(r"\D", "", p)
        return digits[-10:] if len(digits) >= 10 else None

    hits_sorted = sorted(hits, key=lambda h: h["date"], reverse=True)
    best_by_key: dict[str, dict] = {}
    for h in hits_sorted:
        key = normalize_phone(h.get("phone")) or (f"@{h['username']}" if h.get("username") else None) or h["text"][:80]
        if key not in best_by_key:
            h["seen_in_groups"] = {h["group"]}
            best_by_key[key] = h
        else:
            best_by_key[key]["seen_in_groups"].add(h["group"])
    return list(best_by_key.values())


def _phone_key(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else None


async def post_digest(order_id: str, order_number, type_text: str, address: str, hits: list[dict],
                       geo_note: str = "", db_hits: list[dict] | None = None) -> None:
    from telethon import TelegramClient

    tg_state = json.loads(Path(TG_STATE).read_text(encoding="utf-8"))
    client = TelegramClient(SESSION_PATH, tg_state["api_id"], tg_state["api_hash"])
    await client.connect()
    try:
        if geo_note:
            logger.info("заявка %s: %s", order_id, geo_note)
        divider = "➖➖➖➖➖➖➖➖➖➖"
        header = (
            f"**📋 Заявка №{order_number or order_id} — нужен подбор**\n"
            f"Техника: {type_text}\n"
            f"Объект: {address or '—'}\n"
            f"{SYNERGY_ORDER_URL}{order_id}\n"
            f"{divider}\n"
            f"**📞 Кому звонить**\n\n"
        )
        # Решение владельца 21.10.2026: только имя, телефон, тип техники --
        # без группы/даты/расстояния/цитаты сообщения.
        already = {_phone_key(h.get("phone")) for h in hits} - {None}
        tg_lines = []
        for h in sorted(hits, key=lambda h: h["date"], reverse=True)[:15]:
            contact = h.get("phone") or (f"@{h['username']}" if h["username"] else "телефон не указан")
            equipment = h.get("equipment") or type_text
            tg_lines.append(f"• {h['sender']}, {contact}, {equipment}")

        db_lines = []
        for d in (db_hits or []):
            if _phone_key(d.get("phone")) in already:
                continue  # этот же поставщик уже есть среди телеграм-находок
            p = d.get("phone") or ""
            # phone10 хранится как 10 цифр без +7 (app/db.py:phone10) -- для
            # показа человеку собираем в привычный вид, как в app/stats.py:1004.
            phone_pretty = f"+7 {p[:3]} {p[3:6]}-{p[6:8]}-{p[8:]}" if len(p) == 10 else p
            db_lines.append(f"• {d['sender']}, {phone_pretty}, {d['equipment']} (своб. {d['release_date']})")

        parts = []
        if tg_lines:
            parts.append("\n".join(tg_lines))
        else:
            parts.append(f"За последние {LOOKBACK_DAYS} дней в группах «СПБ техника» подходящих предложений не нашлось.")
        if db_lines:
            parts.append(f"**Из своей базы (освобождаются в пределах {RELEASE_WINDOW_DAYS} дней):**\n" + "\n".join(db_lines))
        body = "\n\n".join(parts)
        await client.send_message(GROUP_ID, header + body, parse_mode="md")
    finally:
        await client.disconnect()


RELEASE_WINDOW_DAYS = 3  # решение владельца 21.10.2026: +/- 3 дня от сегодня


def find_db_matches(type_id: str, keywords: list[str]) -> list[dict]:
    """Собственная база техники компании (transport_cards, синхронизирована
    из Synergy отдельным скриптом sync_transports.py) -- второй источник,
    рядом с телеграм-группами. Берём карточки с заполненной «Датой
    освобождения» в пределах +/- 3 дней от сегодня, совпадающие по типу
    техники: сперва точно по id (та же связь transport-type, что у заявки),
    а если у заявки типа-связи нет (только текст в custom-18621) -- по
    словам-ключам, как и при поиске в телеграме."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from app.config import get_settings
    from app.db import connect

    settings = get_settings()
    conn = connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, type_id, type_name, phone10, contact_name, release_date "
            "FROM transport_cards WHERE release_date != ''"
        ).fetchall()
    finally:
        conn.close()

    today = datetime.now(timezone.utc).date()
    low = today - timedelta(days=RELEASE_WINDOW_DAYS)
    high = today + timedelta(days=RELEASE_WINDOW_DAYS)

    matches = []
    for r in rows:
        if type_id:
            if r["type_id"] != type_id:
                continue
        else:
            type_low = (r["type_name"] or "").lower()
            if not any(kw in type_low for kw in keywords):
                continue
        try:
            d = datetime.fromisoformat(r["release_date"]).date()
        except ValueError:
            continue
        if not (low <= d <= high):
            continue
        if not r["phone10"]:
            continue
        matches.append({
            "sender": r["contact_name"] or r["name"] or "без имени",
            "phone": r["phone10"],
            "equipment": r["type_name"] or r["name"] or "",
            "release_date": d.isoformat(),
        })

    # Один поставщик нередко заводит несколько карточек на один и тот же тип
    # техники (разные машины или дубль при правке) -- один телефон, одна
    # строка в итоге, как и с телеграм-находками.
    by_phone: dict[str, dict] = {}
    for m in matches:
        key = _phone_key(m["phone"]) or m["phone"]
        if key not in by_phone:
            by_phone[key] = m
    return list(by_phone.values())


async def process_order(client, order_id: str, geocoder_key: str | None = None) -> str:
    """Возвращает короткий код результата для логов."""
    try:
        # "stage,transport-type" оба нужны явно в include -- иначе Synergy
        # отдаёт relationships.X.data пустым, даже если связь реально есть
        # (задокументированный нюанс, см. skill «синержи»).
        data = client.get(f"orders/{order_id}", **{"include": "stage,transport-type"}).get("data")
    except Exception as e:
        logger.warning("заявка %s: не удалось прочитать (%s)", order_id, e)
        return "read_error"
    if not data:
        return "not_found"

    rel = data.get("relationships", {})
    stage = (rel.get("stage") or {}).get("data") or {}
    if str(stage.get("id")) != STAGE_ID:
        return "not_target_stage"

    attrs = data.get("attributes", {})
    customs = attrs.get("customs") or {}
    type_list = customs.get("custom-18621") or []
    type_text = ", ".join(type_list) if isinstance(type_list, list) else str(type_list)
    tt = (rel.get("transport-type") or {}).get("data")
    type_id = str(tt["id"]) if tt and tt.get("id") else ""
    if not type_text.strip() and type_id:
        # не у всех заявок тип в custom-18621 -- часть ведётся штатной связью
        # transport-type (справочник transport-types), а не кастом-полем.
        try:
            tt_data = client.get(f"transport-types/{type_id}").get("data") or {}
            type_text = (tt_data.get("attributes") or {}).get("name") or ""
        except Exception as e:
            logger.warning("заявка %s: не удалось прочитать transport-type %s (%s)", order_id, type_id, e)
    if not type_text.strip():
        return "no_type"
    address = customs.get("custom-255") or ""
    order_number = attrs.get("number")

    keywords = keywords_from_type(type_text)
    hits = await search_groups(keywords)
    geo_note = ""
    if address.strip() and geocoder_key:
        hits, geo_note = await filter_by_distance(hits, address, geocoder_key)
    elif address.strip():
        geo_note = "ключ геокодера не настроен — фильтр по расстоянию не применён"

    db_hits = find_db_matches(type_id, keywords)

    await post_digest(order_id, order_number, type_text, address, hits, geo_note, db_hits)
    return f"posted({len(hits)} hits, {len(db_hits)} из базы)"


def handle_webhook_sync(payload: dict) -> None:
    """Точка входа из FastAPI BackgroundTasks (синхронная обёртка)."""
    import asyncio
    import sys

    sys.path.insert(0, str(HERE))
    from app.collector import SynergyClient
    from app.config import get_settings

    order_id = extract_order_id(payload)
    if not order_id:
        logger.warning("вебхук подбора: не нашёл id заявки в теле: %s", json.dumps(payload)[:500])
        return

    state = load_state()
    if order_id in state:
        logger.info("заявка %s уже обработана (%s), пропуск", order_id, state[order_id])
        return

    settings = get_settings()
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        timeout_sec=30.0, min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries,
    )

    try:
        result = asyncio.run(process_order(client, order_id, settings.yandex_geocoder_key))
    except Exception as e:  # noqa: BLE001
        logger.exception("подбор по заявке %s упал: %s", order_id, e)
        result = f"error: {e}"

    # Запоминаем только настоящий успех. Все прочие причины (этап ещё не тот,
    # тип техники пока не заполнен, сбой чтения) обратимы -- заявку могут
    # доредактировать, и следующий вебхук должен попробовать снова, а не
    # молча пропускать уже «известный» id.
    if result.startswith("posted"):
        state[order_id] = {"result": result, "ts": time.time()}
        save_state(state)
    logger.info("заявка %s: %s", order_id, result)
