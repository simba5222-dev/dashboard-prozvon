"""Входящий звонок из ВАТС: весь путь от записи до заявки.

Раньше звонки приезжали из CRM опросом раз в десять минут. Это оказалось и
медленно, и неточно: у части звонков CRM отдавала нулевую длительность, и
разговор на три минуты выглядел как сброшенный — за 17.09 таких набралось 42,
175 минут разговоров в слепой зоне.

Теперь источник — сама ВАТС. Российский сервер получает от неё вебхук в
секунду окончания разговора, скачивает запись (из Амстердама её не забрать,
МегаФон не пускает зарубежные адреса) и отдаёт сюда. Здесь звонок проходит
тот же путь, что и раньше, только сразу:

    свой ли менеджер → просев начала записи → полный разбор → заявка в CRM

Порядок именно такой из-за денег: распознать целиком все входящие за день —
это часы работы процессора, а первая минута отвечает на вопрос «просил ли
клиент технику» почти всегда.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app import analyzer, hints
from app.collector import SynergyClient, contact_orders_around, find_contact, load_stages
from app.config import Settings
from app.crm_write import CrmWriter, lead_comment, lead_summary, order_customs
from app.db import save_call, save_screen, save_transcript
from app.stats import local_parts

logger = logging.getLogger(__name__)


def digits(phone: str) -> str:
    """Десять значащих цифр номера.

    В CRM номер записан по-разному — «+79213146028», «931 224-85-35», — а ВАТС
    отдаёт «79213846380». Сравнивать можно только хвост.
    """
    return re.sub(r"\D", "", str(phone or ""))[-10:]


def sales_number(conn: sqlite3.Connection, *numbers: str) -> bool:
    """Звонили ли на номер отдела продаж.

    **Отбор идёт по номеру, а не по имени.** Менеджеры приходят и уходят,
    учётки в ВАТС переиспользуют — `stajer1` на деле Воронков, — а номер
    остаётся в компании и не меняется. Один номер бывает у нескольких
    человек: это всё равно номер отдела продаж.
    """
    for raw in numbers:
        number = digits(raw)
        if not number:
            continue
        row = conn.execute(
            "SELECT 1 FROM managers WHERE dept = 'продажи' AND phone = ? LIMIT 1", (number,)
        ).fetchone()
        if row is not None:
            return True
    return False


def answered_by(conn: sqlite3.Connection, call: dict[str, Any]) -> sqlite3.Row | None:
    """Кто взял трубку — чтобы поставить его соисполнителем заявки.

    Сначала по номеру: у большинства менеджеров он свой, и этого достаточно.
    Если номер общий на нескольких человек, спрашиваем, кто ответил, — ВАТС
    называет имя. На логин не опираемся: учётки переиспользуют, `pleskach_artyom`
    сегодня Быков, а `shorokhova_margarita` — Кудрин.

    Не опознали — заявка всё равно заводится, просто без соисполнителя: потерять
    заказчика хуже, чем оставить поле пустым.
    """
    number = digits(call.get("diversion")) or digits(call.get("telnum"))
    owners = conn.execute(
        "SELECT * FROM managers WHERE dept = 'продажи' AND phone = ?", (number,)
    ).fetchall()
    if len(owners) == 1:
        return owners[0]

    # Номер общий на нескольких человек — спрашиваем, кто ответил. В вебхуке
    # ВАТС есть только учётка (имя приходит лишь в выгрузке истории), поэтому
    # сперва по ней. Учётки переиспользуют, и соответствие мы обновляем при
    # каждой сверке, — но ошибиться тут не страшно: от этого зависит только
    # соисполнитель заявки, а не то, заводить ли её.
    user = str(call.get("user") or "")
    if user:
        for row in owners:
            if row["vats_user"] == user:
                return row
        row = conn.execute(
            "SELECT * FROM managers WHERE vats_user = ? LIMIT 1", (user,)
        ).fetchone()
        if row is not None:
            return row

    key = " ".join(str(call.get("user_name") or "").replace("ё", "е").lower().split()[:2])
    if key:
        for row in owners or conn.execute("SELECT * FROM managers").fetchall():
            display = " ".join(str(row["display_name"]).replace("ё", "е").lower().split()[:2])
            if display == key:
                return row
    return None


def head_bytes(data: bytes, seconds: int, duration_sec: int) -> bytes:
    """Начало записи — тем же способом, что и в пакетном просеве.

    Режем по длине файла: ВАТС отдаёт mp3 с постоянным битрейтом, поэтому
    байты и секунды пропорциональны. Декодер переживает обрезанный кадр.
    """
    if duration_sec <= seconds or duration_sec <= 0:
        return data
    return data[: max(int(len(data) * seconds / duration_sec), 16384)]


# Менеджер ищет технику: обзванивает подрядчиков, те не берут трубку, потом
# перезванивают. Такой перезвон звучит как заказ — «экскаватор-погрузчик нужен
# на завтра» — и отличить его по одной расшифровке невозможно: 45 секунд, ни
# цены, ни объекта. Зато он виден в истории звонков.
#
# Порог выверен на размеченном наборе: два и более коротких недозвона за
# полчаса до входящего не встретились ни у одной из 25 настоящих заявок и
# поймали три чужих звонка. Более широкие условия («мы вообще звонили этому
# номеру») не годятся: менеджеры перезванивают и заказчикам — так отсеклась бы
# каждая четвёртая настоящая заявка.
CALLBACK_WINDOW_MIN = 30
CALLBACK_MIN_TRIES = 2
CALLBACK_MAX_SEC = 15


def parse_time(raw: str) -> datetime | None:
    """Разобрать время звонка из любого нашего источника.

    ВАТС отдаёт UTC без пометки часового пояса, CRM — с московским смещением.
    Без приведения к одному виду сравнение времён врёт, и правила, завязанные
    на «незадолго до звонка», молча перестают работать.
    """
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def callback_to_our_search(conn: sqlite3.Connection, phone: str, started_at: str) -> int:
    """Сколько раз мы безуспешно звонили на этот номер перед входящим."""
    number = digits(phone)
    if not number:
        return 0
    call_time = parse_time(started_at)
    if call_time is None:
        return 0
    rows = conn.execute(
        """
        SELECT started_at FROM calls
        WHERE direction = 'out' AND duration_sec <= ?
          AND REPLACE(REPLACE(REPLACE(client_phone, '+', ''), ' ', ''), '-', '') LIKE ?
        """,
        (CALLBACK_MAX_SEC, f"%{number}"),
    ).fetchall()
    # Время сравниваем разобранным, а не строками: из ВАТС оно приходит в UTC,
    # из CRM — с московским смещением, и лексикографическое сравнение врёт.
    window = timedelta(minutes=CALLBACK_WINDOW_MIN)
    tries = 0
    for row in rows:
        when = parse_time(row["started_at"])
        if when is None:
            continue
        if timedelta(0) <= call_time - when <= window:
            tries += 1
    return tries


def transcribe(settings: Settings, name: str, audio: bytes) -> str:
    """Распознать с делением на стороны и обезличить их.

    Роль по номеру канала у входящих ненадёжна: в одной записи «оператор» —
    наш менеджер, в другой — позвонивший. Кто есть кто, решает разбор.
    """
    response = httpx.post(
        f"{settings.asr_url.rstrip('/')}/transcribe",
        files={"file": (name, audio, "audio/mpeg")},
        data={"mode": "split"}, timeout=settings.asr_timeout_sec,
    )
    response.raise_for_status()
    text = analyzer.dialog_text(response.json().get("dialog") or [])
    return text.replace("operator:", "сторона A:").replace("client:", "сторона B:")


def analyze_like_production(settings: Settings, transcript: str) -> dict[str, Any]:
    """Разбор той же ручкой, что обслуживает звонки на общие номера."""
    response = httpx.post(
        f"{settings.asr_url.rstrip('/')}/analyze",
        json={"transcript": transcript},
        headers={"X-Analysis-Token": settings.asr_analysis_token or ""},
        timeout=180.0,
    )
    response.raise_for_status()
    return response.json()


def process(conn: sqlite3.Connection, settings: Settings, call: dict[str, Any],
            audio: bytes) -> dict[str, Any]:
    """Провести звонок по всему пути. Возвращает, чем дело кончилось."""
    uid = str(call["uid"])
    if not sales_number(conn, str(call.get("diversion") or ""), str(call.get("telnum") or "")):
        return {"uid": uid,
                "skipped": f"номер {call.get('diversion') or call.get('telnum')} "
                           "не из отдела продаж"}
    manager = answered_by(conn, call)

    duration = int(call.get("duration") or 0)
    if duration < settings.inbound_min_duration_sec:
        return {"uid": uid, "skipped": f"разговор {duration} с короче порога"}

    started = str(call.get("start") or "")
    started_dt = parse_time(started)
    if started_dt is not None:
        started = started_dt.isoformat()
    local_date, local_hour = local_parts(started, settings.timezone_offset_hours)
    save_call(
        conn, uid=uid, vats_login=manager["vats_login"] if manager else "неизвестно",
        client_phone=str(call.get("client") or ""), direction="in",
        status=str(call.get("status") or "success"), started_at=started,
        local_date=local_date, local_hour=local_hour,
        wait_sec=int(call.get("wait") or 0), duration_sec=duration,
        record_url=str(call.get("record") or ""),
        # in_group = 0: это личный звонок менеджеру, а не звонок группы
        # прозвона. В счётчиках прозвона ему не место.
        in_group=0, is_demo=0, fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    conn.commit()

    records = Path(settings.records_dir)
    records.mkdir(parents=True, exist_ok=True)
    path = records / f"{uid}.mp3"
    path.write_bytes(audio)

    tries = callback_to_our_search(conn, str(call.get("client") or ""), started)
    if tries >= CALLBACK_MIN_TRIES:
        # Это перезвон на наш собственный поиск техники. Распознавать незачем:
        # заявки тут нет по определению, кто бы что ни говорил в трубку.
        logger.info("звонок %s: перезвон на наш поиск (%s недозвона до этого)", uid, tries)
        save_screen(conn, call_uid=uid, head_text="",
                    verdict_json=json.dumps({"is_request": False,
                                             "verify_role": "our_search",
                                             "request": f"перезвон после {tries} наших недозвонов"},
                                            ensure_ascii=False),
                    is_request=0, created_at=datetime.now(timezone.utc).isoformat())
        conn.commit()
        return {"uid": uid, "skipped": f"перезвон на наш поиск ({tries} недозвона)"}

    head = transcribe(settings, f"{uid}.mp3", head_bytes(audio, 75, duration))
    verdict = analyzer.screen_call(
        head, "", api_key=settings.openai_api_key,
        model=settings.analysis_model, own_company=settings.own_company,
    )
    is_request = bool(verdict["is_request"] and not verdict["about_existing"])
    save_screen(conn, call_uid=uid, head_text=head,
                verdict_json=json.dumps(verdict, ensure_ascii=False),
                is_request=int(is_request),
                created_at=datetime.now(timezone.utc).isoformat())
    conn.commit()
    refresh_hint(conn, settings, str(call.get("client") or ""))
    if not is_request:
        return {"uid": uid, "manager": manager["display_name"] if manager else "—",
                "request": False}

    return create_lead(conn, settings, call, manager, verdict, path, local_date)


def refresh_hint(conn: sqlite3.Connection, settings: Settings, phone: str) -> None:
    """Обновить подсказку в карточке клиента после разговора.

    Пишем сразу после звонка, а не перед следующим: в момент входящего дорога
    каждая секунда, а карточку CRM показывает мгновенно — она должна застать
    подсказку уже готовой.
    """
    field = getattr(settings, "field_contact_hint", None)
    if not field:
        return
    try:
        client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
        contact = find_contact(client, phone)
        if contact is None:
            return
        text = hints.build(conn, phone, hints.orders_of(client, contact["id"], load_stages(client)))
        if text:
            CrmWriter(client, apply=True).set_contact_hint(contact["id"], field, text)
    except Exception:  # noqa: BLE001 — подсказка не должна мешать заявке
        logger.exception("подсказка по %s не обновилась", phone)


def create_lead(conn: sqlite3.Connection, settings: Settings, call: dict[str, Any],
                manager: sqlite3.Row | None, verdict: dict[str, Any], path: Path,
                local_date: str) -> dict[str, Any]:
    """Полный разбор и заявка в CRM — если её ещё нет."""
    uid = str(call["uid"])
    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
    contact = find_contact(client, str(call.get("client") or ""))
    if contact is None:
        return {"uid": uid, "request": True, "order": None,
                "note": "контакта с таким телефоном в CRM нет"}

    stages = load_stages(client)
    after, _active = contact_orders_around(client, contact["id"], str(call.get("start") or ""), stages)
    if after:
        return {"uid": uid, "request": True, "order": None,
                "note": f"заявка уже есть: {'; '.join(after[:2])}"}

    transcript = transcribe(settings, path.name, path.read_bytes())
    save_transcript(conn, call_uid=uid, text=transcript, analysis_json=None,
                    created_at=datetime.now(timezone.utc).isoformat(), is_demo=0)
    conn.commit()
    analysis = analyze_like_production(settings, transcript)

    row = {"uid": uid, "started_at": call.get("start"), "client_phone": call.get("client"),
           "display_name": (manager["display_name"] if manager
                            else str(call.get("user_name") or "неизвестно")),
           "vats_login": manager["vats_login"] if manager else "неизвестно"}
    stage_id = next((sid for sid, (name, _kind) in stages.items()
                     if name.strip().lower() == settings.crm_lead_stage.lower()), None)
    writer = CrmWriter(client, apply=True)
    order_id = writer.create_order(
        contact_id=contact["id"], name=settings.crm_lead_order_name, stage_id=stage_id,
        responsible_id=settings.crm_lead_responsible,
        customs=order_customs(analysis, transcript, lead_summary(row, verdict, analysis)),
        comment=lead_comment(row, verdict, analysis),
    )
    if order_id and manager is not None and manager["synergy_user"]:
        writer.add_performer(order_id, manager["synergy_user"])
    if order_id:
        conn.execute("UPDATE screens SET created_order_id = ? WHERE call_uid = ?", (order_id, uid))
        conn.commit()
    logger.info("звонок %s: заявка %s по контакту %s", uid, order_id, contact["id"])
    return {"uid": uid, "request": True, "order": order_id,
            "manager": manager["display_name"] if manager else "—"}
