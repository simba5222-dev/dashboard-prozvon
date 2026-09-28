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

from app import analyzer, call_context, hints
from app.collector import (SynergyClient, contact_orders_around, find_contact,
                           load_stages, order_names)
from app.config import Settings
from app import prozvon
from app.crm_write import CrmWriter, lead_comment, lead_summary, order_customs
from app.db import save_call, save_inbound_check, save_screen, save_transcript
from app.stats import local_parts

logger = logging.getLogger(__name__)


def digits(phone: str) -> str:
    """Десять значащих цифр номера.

    В CRM номер записан по-разному — «+79213146028», «931 224-85-35», — а ВАТС
    отдаёт «79213846380». Сравнивать можно только хвост.
    """
    return re.sub(r"\D", "", str(phone or ""))[-10:]


def sales_number(conn: sqlite3.Connection, number: str) -> bool:
    """Позвонили ли **на прямой номер** менеджера отдела продаж.

    **Отбор идёт по номеру, а не по имени.** Менеджеры приходят и уходят,
    учётки в ВАТС переиспользуют — `stajer1` на деле Воронков, — а номер
    остаётся в компании и не меняется. Один номер бывает у нескольких
    человек: это всё равно номер отдела продаж.

    Принимается **один** номер — `diversion`, тот, который набрал клиент.
    Раньше сюда передавали ещё и `telnum` (прямой номер сотрудника, которому
    ВАТС перевела звонок), и хватало совпадения любого из двух. На этом всё и
    сломалось: клиент звонит на рекламный номер «Авито СПБ», ВАТС переводит
    на Толстова, `diversion` говорит «рекламный» — а `telnum` говорит
    «Толстов», и его «да» перевешивало. Звонок с рекламы разбирался как
    личный, и по нему заводилась заявка «Пойманная с прослушки» — при том что
    менеджер этот лид в ту же минуту брал в работу. Так вышло 8 заявок из 18.

    `telnum` остался там, где он и уместен, — в `answered_by`: он отвечает на
    вопрос «кто взял трубку», а не «наш ли это сценарий».
    """
    digits_only = digits(number)
    if not digits_only:
        return False
    row = conn.execute(
        "SELECT 1 FROM managers WHERE dept = 'продажи' AND phone = ? LIMIT 1", (digits_only,)
    ).fetchone()
    return row is not None


def group_number(conn: sqlite3.Connection, number: str) -> bool:
    """Это прямой номер менеджера прозвона?

    Отбор такой же, как у отдела продаж, и по той же причине: учётки в ВАТС
    переиспользуют, а номер за человеком закреплён.
    """
    digits_only = digits(number)
    if not digits_only:
        return False
    row = conn.execute(
        "SELECT 1 FROM managers WHERE dept = ? AND phone = ? LIMIT 1",
        ("прозвон", digits_only),
    ).fetchone()
    return row is not None


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


def transcribe(settings: Settings, name: str, audio: bytes, seconds: float = 0.0) -> str:
    """Распознать с делением на стороны и обезличить их.

    Роль по номеру канала у входящих ненадёжна: в одной записи «оператор» —
    наш менеджер, в другой — позвонивший. Кто есть кто, решает разбор.
    """
    response = httpx.post(
        f"{settings.asr_url.rstrip('/')}/transcribe",
        files={"file": (name, audio, "audio/mpeg")},
        # Нужный отрезок вырезает сам сервис: резать файл по байтам нельзя,
        # на части записей такой кусок не декодируется.
        data={"mode": "split", "seconds": str(seconds)},
        timeout=settings.asr_timeout_sec,
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
    if str(call.get("type") or "in") == "out":
        return prozvon_call(conn, settings, call, audio)
    # Сценарий выбирается по тому, КУДА звонил клиент, и только по этому.
    # Рекламный номер — это сценарий «звонки на общие номера», им занимается
    # боевой сервер; искать там потерянную заявку бессмысленно и вредно.
    dialed = str(call.get("diversion") or "")
    if not sales_number(conn, dialed):
        return {"uid": uid,
                "skipped": f"звонили на {dialed or '—'}, это не прямой номер "
                           "менеджера отдела продаж"}
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
        in_group=0, diversion=digits(dialed) or None,
        is_demo=0, fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    conn.commit()

    # Строка проверки по CRM нужна, даже если дальше всё упадёт: пакетный
    # просев берёт звонки именно по ней. Без неё звонок, на котором сломалось
    # распознавание или кончились деньги у модели, не подхватит уже никто.
    save_inbound_check(
        conn, call_uid=uid, contact_id=None, contact_found=0, contact_name=None,
        company_name=None, orders_after=0, order_names="",
        checked_at=datetime.now(timezone.utc).isoformat(),
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

    head = transcribe(settings, f"{uid}.mp3", audio, seconds=75)
    # Повод звонка считается из метаданных и уходит в задание как факт.
    # Раньше модель выводила его из слов, и на перезвонах исполнителей
    # придумывала заявки, которых не было.
    контекст = call_context.describe(call_context.build(conn, {
        "uid": uid, "direction": "in",
        "client_phone": str(call.get("client") or ""),
        "vats_login": manager["vats_login"] if manager else "",
        "diversion": digits(dialed), "started_at": started,
    }))
    verdict = analyzer.screen_call(
        head, "", api_key=settings.openai_api_key,
        model=settings.analysis_model, own_company=settings.own_company,
        context=контекст,
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


def lead_block_reason(active: list[dict[str, str]]) -> str:
    """Почему звонок вообще не надо трогать. Пустая строка — работаем.

    Остался один случай: у клиента **открыта** заявка, заведённая до звонка.
    Решение владельца от 22.09.2026 — ничего не делаем. Такой звонок почти
    всегда «где моя техника», и менеджер про этого клиента не забудет: он с
    ним прямо сейчас работает.

    Раньше сюда же попадал случай «заявку завели вокруг звонка» и тоже давал
    отказ. Теперь он обрабатывается иначе: такую заявку мы **дописываем**, а
    не обходим стороной, — см. `create_lead`.

    Открытой считается не всякая незакрытая, а только та, которую трогали за
    `order_active_days`. Иначе брошенный «Новый» двухлетней давности съедал бы
    новый запрос от старого клиента, а это ровно тот заказ, про который
    менеджер и забывает: проконсультировал и не оформил.
    """
    if active:
        return f"у клиента открыта заявка: {order_names(active[:2])}"
    return ""


def _remember_orders(conn: sqlite3.Connection, uid: str,
                     after: list[dict[str, str]], active: list[dict[str, str]]) -> None:
    """Запомнить, какие заявки нашлись у контакта, — чтобы отказ был виден.

    Без этого «не завели, потому что у клиента уже есть заявка» нигде не
    остаётся следом, и отличить его от «просто ничего не нашли» нельзя ни на
    экране, ни потом при разборе.
    """
    conn.execute(
        """UPDATE inbound_checks
              SET orders_after = ?, order_names = ?, active_orders = ?, active_names = ?
            WHERE call_uid = ?""",
        (len(after), order_names(after), len(active), order_names(active), uid),
    )
    conn.commit()


def prozvon_call(conn: sqlite3.Connection, settings: Settings,
                 call: dict[str, Any], audio: bytes) -> dict[str, Any]:
    """Исходящий звонок менеджера прозвона — разбираем сразу после разговора.

    До 22.09.2026 эти разговоры разбирались пачкой и только руками, поэтому
    отчёт по менеджеру отставал на дни, а однажды простоял четыре. Теперь
    путь тот же, что у входящих: запись приезжает с боевого сервера в секунду
    окончания разговора, и разбор идёт сразу.

    Чужие исходящие сюда не попадают: боевой шлёт только звонки с номеров
    прозвона, а здесь это проверяется ещё раз — кто отдаёт запись, решать не
    ему.
    """
    uid = str(call["uid"])
    dialed = str(call.get("diversion") or "")
    if not group_number(conn, dialed):
        return {"uid": uid, "skipped": f"исходящий с {dialed or '—'}, "
                                       "это не номер менеджера прозвона"}
    duration = int(call.get("duration") or 0)
    if duration < settings.transcribe_min_duration_sec:
        return {"uid": uid, "skipped": f"разговор {duration} с короче порога"}

    manager = conn.execute(
        "SELECT * FROM managers WHERE dept = 'прозвон' AND phone = ?",
        (digits(dialed),),
    ).fetchone()
    started = str(call.get("start") or "")
    started_dt = parse_time(started)
    if started_dt is not None:
        started = started_dt.isoformat()
    local_date, local_hour = local_parts(started, settings.timezone_offset_hours)
    save_call(
        conn, uid=uid, vats_login=manager["vats_login"] if manager else "неизвестно",
        client_phone=str(call.get("client") or ""), direction="out",
        status=str(call.get("status") or "success"), started_at=started,
        local_date=local_date, local_hour=local_hour,
        wait_sec=int(call.get("wait") or 0), duration_sec=duration,
        record_url=str(call.get("record") or ""),
        # in_group = 1: это и есть звонок прозвона, ради которого всё затевалось.
        in_group=1, diversion=digits(dialed) or None,
        is_demo=0, fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    conn.commit()

    records = Path(settings.records_dir)
    records.mkdir(parents=True, exist_ok=True)
    path = records / f"{uid}.mp3"
    path.write_bytes(audio)
    return prozvon.analyze_call(conn, settings, uid, path)


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
    """Полный разбор и заявка в CRM.

    Свою заявку заводим **только если её нет**. Synergy на входящий звонок
    часто создаёт карточку сама — телефонная интеграция делает это в первую
    секунду разговора, с именем «-» или номером телефона. Из 55 наших заявок
    у 15 рядом стояла такая: два лида на одно обращение, и менеджер видел два.

    Поэтому: нашлась заявка вокруг звонка — **дописываем её**, а не создаём
    рядом. Метка `custom-30614` ставится в обоих случаях: по ней владелец
    находит нашу работу, что бы ни было написано в названии.
    """
    uid = str(call["uid"])
    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
    contact = find_contact(client, str(call.get("client") or ""))
    if contact is None:
        return {"uid": uid, "request": True, "order": None,
                "note": "контакта с таким телефоном в CRM нет"}

    stages = load_stages(client)
    after, active = contact_orders_around(
        client, contact["id"], str(call.get("start") or ""), stages,
        fresh_days=settings.order_active_days)
    reason = lead_block_reason(active)
    if reason:
        _remember_orders(conn, uid, after, active)
        logger.info("звонок %s: %s — не трогаем", uid, reason)
        return {"uid": uid, "request": True, "order": None, "note": reason}

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
    summary = lead_summary(row, verdict, analysis)
    comment = lead_comment(row, verdict, analysis)
    customs = order_customs(
        analysis, transcript, summary,
        caught=caught_mark(uid, call, settings.timezone_offset_hours))

    if after:
        # Заявку уже завели — её и дописываем. Ни стадию, ни ответственного,
        # ни название не трогаем: заявка чужая, ведёт её человек.
        target = after[0]["id"]
        writer.update_customs(target, customs)
        writer.post_comment(target, comment)
        if manager is not None and manager["synergy_user"]:
            writer.add_performer(target, manager["synergy_user"])
        writer.mark_call(uid, f"заявка {target} — «{after[0]['name']}»")
        _remember_orders(conn, uid, after, active)
        conn.execute("UPDATE screens SET created_order_id = ? WHERE call_uid = ?", (target, uid))
        conn.commit()
        logger.info("звонок %s: дописана существующая заявка %s (%s)",
                    uid, target, after[0]["name"])
        return {"uid": uid, "request": True, "order": target, "enriched": True,
                "manager": manager["display_name"] if manager else "—"}

    order_id = writer.create_order(
        contact_id=contact["id"], name=settings.crm_lead_order_name, stage_id=stage_id,
        responsible_id=settings.crm_lead_responsible,
        customs=customs, comment=comment,
    )
    if order_id and manager is not None and manager["synergy_user"]:
        writer.add_performer(order_id, manager["synergy_user"])
    if order_id:
        writer.mark_call(uid, f"заявка {order_id} — «{settings.crm_lead_order_name}»")
        conn.execute("UPDATE screens SET created_order_id = ? WHERE call_uid = ?", (order_id, uid))
        conn.commit()
    logger.info("звонок %s: заявка %s по контакту %s", uid, order_id, contact["id"])
    return {"uid": uid, "request": True, "order": order_id, "enriched": False,
            "manager": manager["display_name"] if manager else "—"}


def caught_mark(uid: str, call: dict[str, Any] | None = None,
                offset_hours: int = 3) -> str:
    """Что пишем в поле «Пойманная с прослушки» у заявки.

    Не просто «да». РОП открывает заявку, видит в активности несколько звонков
    и не понимает, из какого она выросла, — поэтому метка называет звонок
    по-человечески: когда, сколько длился, с какого номера. Идентификатор ВАТС
    оставляем в хвосте: людям он не нужен, а нам по нему искать.
    """
    parts = []
    started = str((call or {}).get("start") or "")
    when = parse_time(started)
    if when is not None:
        local = when + timedelta(hours=offset_hours)
        parts.append(local.strftime("%d.%m.%Y %H:%M"))
    duration = int((call or {}).get("duration") or 0)
    if duration:
        parts.append(f"{duration} с")
    phone = str((call or {}).get("client") or "").strip()
    if phone:
        parts.append(f"+{phone.lstrip('+')}")
    parts.append(f"звонок {uid}")
    return "да · " + " · ".join(parts)
