"""Сбор данных из Synergy CRM.

Звонки берём не из ВАТС, а из самой Synergy: она заводит запись о каждом
звонке, и в этой записи уже есть направление, длительность, время и — главное —
поле `custom-28722` вида «Ткачевин Михаил Эдгарович 726», то есть ФИО
сотрудника вместе с добавочным. Поэтому сопоставлять логины ВАТС с
пользователями CRM не требуется вовсе: звонок сам знает своего автора.

Побочная выгода: дашборд не зависит от доступности ВАТС. Она пускает только
российские адреса, а Synergy отвечает откуда угодно.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from app.config import Settings
from app.db import (
    save_call,
    save_call_order,
    save_call_task,
    save_card_check,
    save_inbound_check,
    upsert_manager,
)
from app.stats import local_parts

logger = logging.getLogger(__name__)

# Виды стадий, после которых заявка больше не живёт: сделка состоялась или
# провалена. Всё остальное — работа в процессе.
INACTIVE_STAGE_KINDS = ("won", "lost")

# Поле звонка, где Synergy хранит «Фамилия Имя Отчество добавочный».
CALL_AUTHOR_FIELD = "custom-28722"
# Поля телефона у контакта — записаны по-разному, приходится перебирать.
CONTACT_PHONE_FIELDS = ("general-phone", "mobile-phone", "work-phone", "other-phone")


class SynergyClient:
    """Тонкий клиент Synergy: только чтение и только то, что нужно дашборду.

    Лимит запросов у Synergy общий на аккаунт и делится с сервисом распознавания,
    который ходит туда же. Поэтому здесь свой тормоз и свои повторы: проверка
    одной карточки это шесть обращений, а карточек за день несколько десятков.
    """

    def __init__(
        self, *, base_url: str, token: str, timeout_sec: float = 30.0,
        min_interval_sec: float = 0.35, retries: int = 5,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout_sec
        self._min_interval = min_interval_sec
        self._retries = max(1, retries)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def _wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_at = now + self._min_interval

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        url = f"{self._base}/{path.lstrip('/')}"
        last: httpx.Response | None = None
        for attempt in range(1, self._retries + 1):
            self._wait()
            response = httpx.get(url, params=params, headers=self._headers, timeout=self._timeout)
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            last = response
            if attempt < self._retries:
                hinted = response.headers.get("Retry-After")
                pause = float(hinted) if hinted else min(1.5 * (2 ** (attempt - 1)), 12.0)
                logger.info("Synergy ответила 429, жду %.1f с (попытка %s)", pause, attempt)
                time.sleep(min(pause, 20.0))
        assert last is not None
        logger.warning("Synergy: 429 после %s попыток — %s", self._retries, path)
        last.raise_for_status()
        return {}

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Запись в Synergy. Тот же тормоз и те же повторы, что у чтения.

        Раньше повторы были только у чтения, и записи молча терялись при 429 —
        в сервисе распознавания это уже проходили.
        """
        url = f"{self._base}/{path.lstrip('/')}"
        headers = {**self._headers, "Content-Type": "application/vnd.api+json"}
        last: httpx.Response | None = None
        for attempt in range(1, self._retries + 1):
            self._wait()
            response = httpx.post(url, json=payload, headers=headers, timeout=self._timeout)
            if response.status_code != 429:
                response.raise_for_status()
                return response.json() if response.content else {}
            last = response
            if attempt < self._retries:
                hinted = response.headers.get("Retry-After")
                pause = float(hinted) if hinted else min(1.5 * (2 ** (attempt - 1)), 12.0)
                logger.info("Synergy ответила 429 на запись, жду %.1f с", pause)
                time.sleep(min(pause, 20.0))
        assert last is not None
        last.raise_for_status()
        return {}

    def patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Правка записи в Synergy. Тот же тормоз и повторы, что у остальных."""
        url = f"{self._base}/{path.lstrip('/')}"
        headers = {**self._headers, "Content-Type": "application/vnd.api+json"}
        last: httpx.Response | None = None
        for attempt in range(1, self._retries + 1):
            self._wait()
            response = httpx.patch(url, json=payload, headers=headers, timeout=self._timeout)
            if response.status_code != 429:
                response.raise_for_status()
                return response.json() if response.content else {}
            last = response
            if attempt < self._retries:
                time.sleep(min(1.5 * (2 ** (attempt - 1)), 12.0))
        assert last is not None
        last.raise_for_status()
        return {}

    def count(self, path: str) -> int | None:
        """Сколько записей в связи — без выгрузки самих записей."""
        try:
            data = self.get(path, per_page=1)
        except (httpx.HTTPError, ValueError):
            return None
        return (data.get("meta") or {}).get("record-count")


def normalize(text: str) -> str:
    """Для сравнения названий: регистр и «ё» в русских системах пишут как попало."""
    return (text or "").strip().lower().replace("ё", "е")


def load_stages(client: SynergyClient) -> dict[str, tuple[str, str]]:
    """Справочник стадий заявки: id → (название, вид).

    Вид заполнен только у трёх стадий — «Сделка» (won), «Новый» (opened) и
    «Сделка провалена» (lost). У остальных он пустой, и это не пробел в данных:
    промежуточные стадии вроде «Выставлен счёт» исходом не являются.
    """
    try:
        rows = client.get("order-stages", per_page=100).get("data") or []
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("справочник стадий не прочитался: %s", exc)
        return {}
    out = {}
    for row in rows:
        attrs = row.get("attributes") or {}
        out[str(row["id"])] = (str(attrs.get("name") or ""), str(attrs.get("kind") or ""))
    return out


def text_of(value: Any) -> str:
    """Значение поля карточки как строка. Списки Synergy отдаёт для «мультивыбора»."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def contact_company(client: SynergyClient, contact_id: str) -> str:
    """Название компании, привязанной к контакту. Пусто — компании нет."""
    try:
        rows = client.get(f"contacts/{contact_id}/companies", per_page=3).get("data") or []
    except (httpx.HTTPError, ValueError):
        return ""
    names = []
    for row in rows:
        attrs = row.get("attributes") or {}
        name = attrs.get("as-string") or attrs.get("name")
        if name:
            names.append(str(name).strip())
    return ", ".join(names)


def orders_after_call(
    client: SynergyClient, contact_id: str, after_iso: str,
    window_hours: int, stages: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Заявки, заведённые по контакту вскоре после звонка.

    Связи `responsible` и `stage` в списке приходят пустыми — их надо просить
    через `include`, тогда сами объекты лежат в разделе `included`.
    """
    try:
        payload = client.get(f"contacts/{contact_id}/orders",
                             include="responsible,stage", sort="-created-at", per_page=50)
    except (httpx.HTTPError, ValueError):
        return []
    index = {(row["type"], str(row["id"])): row for row in payload.get("included") or []}

    try:
        call_time = datetime.fromisoformat(after_iso.replace("Z", "+00:00"))
    except ValueError:
        return []
    limit = call_time + timedelta(hours=window_hours)

    out: list[dict[str, Any]] = []
    for row in payload.get("data") or []:
        attrs = row.get("attributes") or {}
        created = attrs.get("created-at")
        if not created:
            continue
        try:
            made = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            continue
        if not (call_time <= made <= limit):
            continue

        rels = row.get("relationships") or {}

        def linked(rel_name: str) -> dict | None:
            ref = (rels.get(rel_name) or {}).get("data")
            if not ref:
                return None
            return index.get((ref["type"], str(ref["id"])))

        user = linked("responsible")
        responsible = ""
        if user:
            ua = user.get("attributes") or {}
            responsible = str(ua.get("as-string")
                              or " ".join(filter(None, [ua.get("last-name"), ua.get("first-name")]))).strip()

        stage_ref = ((rels.get("stage") or {}).get("data") or {})
        stage_name, stage_kind = stages.get(str(stage_ref.get("id") or ""), ("", ""))

        number = attrs.get("number")
        title = str(attrs.get("name") or "").strip()
        out.append({
            "order_id": str(row["id"]),
            "name": f"№{number} {title}".strip() if number else title,
            "created_at": str(created),
            "responsible": responsible,
            "stage_name": stage_name,
            "stage_kind": stage_kind,
            "amount": float(attrs.get("amount") or 0),
        })
    return out


def surname_of(full_name: str) -> str:
    """Фамилия из «Фамилия Имя Отчество» — по ней и сопоставляем."""
    return normalize((full_name or "").split(" ")[0])


def parse_author(raw: Any) -> tuple[str, str]:
    """Из «Ткачевин Михаил Эдгарович 726» получить имя и добавочный."""
    text = " ".join(raw) if isinstance(raw, list) else str(raw or "")
    text = text.strip()
    match = re.search(r"\s(\d{2,6})$", text)
    if match:
        return text[: match.start()].strip(), match.group(1)
    return text, ""


def sync_managers(
    conn: sqlite3.Connection, client: SynergyClient, group_name: str,
    dept: str = "прозвон",
) -> list[str]:
    """Обновить список сотрудников из группы Synergy. Возвращает фамилии.

    `dept` разделяет два списка: «прозвон» — те, чью дисциплину считает
    дашборд, «продажи» — те, кому клиенты звонят напрямую. Один и тот же
    человек может быть в обеих группах, тогда за ним остаётся последний
    записанный отдел — это осознанно: отчёты по нему всё равно разные.
    """
    groups = client.get("user-groups", per_page=100).get("data") or []
    target = next(
        (g for g in groups
         if normalize(str((g.get("attributes") or {}).get("name") or ""))
         == normalize(group_name)),
        None,
    )
    if target is None:
        logger.warning("группа «%s» в Synergy не найдена", group_name)
        return []

    members = client.get(f"user-groups/{target['id']}/users", per_page=100).get("data") or []
    surnames: list[str] = []
    for user in members:
        attrs = user.get("attributes") or {}
        name = attrs.get("as-string") or ""
        if not name:
            continue
        upsert_manager(
            conn,
            vats_login=surname_of(name),   # ключ — фамилия: по ней узнаём автора звонка
            display_name=name,
            synergy_user=user["id"],
            plan_calls=None,
            active=0 if attrs.get("disabled") else 1,
            dept=dept,
            is_demo=0,
        )
        if not attrs.get("disabled"):
            surnames.append(surname_of(name))
    conn.commit()
    logger.info("группа «%s» (%s): активных сотрудников %s", group_name, dept, len(surnames))
    return surnames


def iter_calls_for_day(client: SynergyClient, day: str, max_pages: int = 40) -> Iterable[dict]:
    """Звонки за местную дату. Идём от свежих и останавливаемся, перейдя границу."""
    for page in range(1, max_pages + 1):
        try:
            rows = client.get("telephony-calls", per_page=100, page=page,
                              sort="-created-at").get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Synergy: страница %s не прочиталась: %s", page, exc)
            return
        if not rows:
            return
        older = False
        for item in rows:
            created = (item["attributes"].get("created-at") or "")[:10]
            if created and created < day:
                older = True
                continue
            if created == day:
                yield item
        if older:
            return


def collect_range(
    conn: sqlite3.Connection, client: SynergyClient, settings: Settings,
    since: str, until: str, max_pages: int = 400,
) -> tuple[int, int]:
    """Собрать звонки за период одним проходом.

    По дню за раз пришлось бы каждый раз листать историю с начала: Synergy
    отдаёт звонки только от свежих к старым. Здесь идём один раз до нижней
    границы и раскладываем встреченное по дням.
    """
    known = {row["vats_login"] for row in conn.execute("SELECT vats_login FROM managers")}
    if not known:
        logger.warning("менеджеров в базе нет — сначала синхронизируйте группу")
        return 0, 0

    now = datetime.now(timezone.utc).isoformat()
    new = seen = 0
    for page in range(1, max_pages + 1):
        try:
            rows = client.get("telephony-calls", per_page=100, page=page,
                              sort="-created-at").get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Synergy: страница %s не прочиталась: %s", page, exc)
            break
        if not rows:
            break
        too_old = False
        for item in rows:
            attrs = item["attributes"]
            created = (attrs.get("created-at") or "")[:10]
            if created and created < since:
                too_old = True
                continue
            if created > until:
                continue
            is_new, in_group = store_call(conn, item, settings, known, now)
            if in_group:
                seen += 1
                new += int(is_new)
        conn.commit()
        if page % 20 == 0:
            logger.info("просмотрено страниц %s, звонков менеджеров %s", page, seen)
        if too_old:
            break
    conn.commit()
    logger.info("период %s…%s: найдено %s, новых %s", since, until, seen, new)
    return new, seen


def store_call(
    conn: sqlite3.Connection, item: dict[str, Any], settings: Settings,
    known: set[str], now: str,
) -> tuple[bool, bool]:
    """Сохранить звонок как он пришёл из Synergy.

    Возвращает (новый ли, звонок ли прозвона). Входящие и звонки чужих
    менеджеров сохраняем наравне с остальными: страницы всё равно пролистаны,
    а без них не найти заявку, о которой клиент попросил напрямую менеджера.
    В счётчики прозвона они не попадают — там условие `in_group = 1`.
    """
    attrs = item["attributes"]
    outgoing = attrs.get("direction") == "outgoing"
    author, _ = parse_author((attrs.get("customs") or {}).get(CALL_AUTHOR_FIELD))
    surname = surname_of(author)
    in_group = bool(surname in known and outgoing)
    started = attrs.get("started-at") or attrs.get("created-at") or ""
    local_date, local_hour = local_parts(started, settings.timezone_offset_hours)
    phone = (attrs.get("dst-phone-number") if outgoing else attrs.get("src-phone-number")) or ""
    is_new = save_call(
        conn,
        uid=str(item["id"]),
        vats_login=surname or "неизвестно",
        client_phone=str(phone),
        direction="out" if outgoing else "in",
        status=str(attrs.get("status") or ""),
        started_at=started,
        local_date=local_date,
        local_hour=local_hour,
        wait_sec=int(float(attrs.get("wait") or 0)),
        duration_sec=int(float(attrs.get("duration") or 0)),
        record_url=attrs.get("recording") or None,
        in_group=int(in_group),
        is_demo=0,
        fetched_at=now,
    )
    return is_new, in_group


def collect_calls(
    conn: sqlite3.Connection, client: SynergyClient, settings: Settings, day: str,
) -> tuple[int, int]:
    """Сохранить исходящие звонки менеджеров за день. Возвращает (новых, всего)."""
    known = {row["vats_login"] for row in conn.execute("SELECT vats_login FROM managers")}
    if not known:
        logger.warning("менеджеров в базе нет — сначала синхронизируйте группу")
        return 0, 0

    now = datetime.now(timezone.utc).isoformat()
    new = seen = 0
    for item in iter_calls_for_day(client, day):
        is_new, in_group = store_call(conn, item, settings, known, now)
        if in_group:
            seen += 1
            new += int(is_new)
    conn.commit()
    logger.info("звонки за %s: найдено %s, новых %s", day, seen, new)
    return new, seen


def find_contact(client: SynergyClient, phone: str) -> dict[str, Any] | None:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    variants = [digits, digits[-10:]] if len(digits) > 10 else [digits]
    for field in CONTACT_PHONE_FIELDS:
        for variant in variants:
            try:
                rows = client.get("contacts", **{f"filter[{field}]": variant, "per_page": 1}).get("data")
            except (httpx.HTTPError, ValueError):
                continue
            if rows:
                return rows[0]
    return None


def contact_phones(client: SynergyClient, contact_id: str) -> set[str]:
    """Все телефоны контакта — по ним ищем звонки любых менеджеров."""
    try:
        data = client.get(f"contacts/{contact_id}").get("data") or {}
    except (httpx.HTTPError, ValueError):
        return set()
    attrs = data.get("attributes") or {}
    out = set()
    for field in CONTACT_PHONE_FIELDS:
        value = attrs.get(field)
        for item in value if isinstance(value, list) else [value]:
            digits = re.sub(r"\D", "", str(item or ""))
            if len(digits) >= 10:
                out.add(digits[-10:])
    return out


def collect_calls_for_phones(
    conn: sqlite3.Connection, client: SynergyClient, settings: Settings,
    phones: set[str], since: str, until: str, max_pages: int = 400,
) -> tuple[int, str]:
    """Сохранить звонки с этими клиентами за период — чьи угодно, в обе стороны.

    Нужно для разбора заявки: её ведёт не тот, кто звонил в прозвоне, а тот,
    кому её передали, и его звонки в дашборде не собираются. Отбирать звонки
    по контакту Synergy не умеет (`filter[contact-id]` отвечает 400, а
    `filter[dst-phone-number]` — 500), поэтому листаем период и сверяем
    телефоны на своей стороне.

    Телефоны принимаем **все сразу**, одним проходом: компания делает около
    1300 звонков в день, до заявки недельной давности это тысяч десять записей.
    Листать их заново под каждую заявку — час работы и лишняя нагрузка на CRM.

    Возвращает (сколько сохранено, самая старая просмотренная дата). Вторая
    величина важнее первой: если листание не дошло до даты заявки, «звонков
    нет» означает «мы не смотрели», а не «менеджер не звонил». Перепутать эти
    два состояния нельзя — по ним судят о работе людей.

    Такие звонки помечаются `in_group = 0` и в счётчики прозвона не попадают.
    """
    if not phones:
        return 0, until
    now = datetime.now(timezone.utc).isoformat()
    known = {row["vats_login"] for row in conn.execute("SELECT vats_login FROM managers")}
    saved = 0
    reached = until
    for page in range(1, max_pages + 1):
        try:
            rows = client.get("telephony-calls", per_page=100, page=page,
                              sort="-created-at").get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("звонки клиента: страница %s не прочиталась: %s", page, exc)
            break
        if not rows:
            # Страницы кончились — значит, просмотрели всё, что есть в CRM.
            reached = since
            break
        too_old = False
        for item in rows:
            attrs = item["attributes"]
            created = (attrs.get("created-at") or "")[:10]
            if created:
                reached = min(reached, created)
            if created and created < since:
                too_old = True
                continue
            if created > until:
                continue
            incoming = attrs.get("direction") != "outgoing"
            client_phone = str(
                (attrs.get("src-phone-number") if incoming else attrs.get("dst-phone-number")) or ""
            )
            digits = re.sub(r"\D", "", client_phone)
            if len(digits) < 10 or digits[-10:] not in phones:
                continue
            author, _ = parse_author((attrs.get("customs") or {}).get(CALL_AUTHOR_FIELD))
            surname = surname_of(author)
            started = attrs.get("started-at") or attrs.get("created-at") or ""
            local_date, local_hour = local_parts(started, settings.timezone_offset_hours)
            if save_call(
                conn, uid=str(item["id"]), vats_login=surname or "неизвестно",
                client_phone=client_phone,
                direction="in" if incoming else "out",
                status=str(attrs.get("status") or ""),
                started_at=started, local_date=local_date, local_hour=local_hour,
                wait_sec=int(float(attrs.get("wait") or 0)),
                duration_sec=int(float(attrs.get("duration") or 0)),
                record_url=attrs.get("recording") or None,
                in_group=int(surname in known), is_demo=0, fetched_at=now,
            ):
                saved += 1
        if too_old:
            break
        # Фиксируем каждую страницу: пока транзакция открыта, другие процессы
        # (разбор записей, сборщик по таймеру) не могут писать в базу.
        conn.commit()
        if page % 25 == 0:
            logger.info("просмотрено страниц %s, дошли до %s", page, reached)
    conn.commit()
    logger.info("звонки клиентов за %s…%s: новых %s, просмотрено до %s",
                since, until, saved, reached)
    return saved, reached


def contact_orders_around(
    client: SynergyClient, contact_id: str, call_iso: str, stages: dict[str, tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Заявки контакта: заведённые после звонка и открытые на момент звонка.

    Заявка в Synergy привязана к контакту, поэтому проверять надо именно по
    нему, а не по времени: «после этого звонка по клиенту появилась заявка» —
    вот признак того, что менеджер её оформил. Верхнего окна нет: он мог
    завести её и через два дня.

    Открытые заявки возвращаем отдельно — это контекст. Клиент часто звонит
    по уже заведённой заявке, и такой разговор запросом не считается.
    """
    try:
        payload = client.get(f"contacts/{contact_id}/orders",
                             include="stage", sort="-created-at", per_page=50)
    except (httpx.HTTPError, ValueError):
        return [], []
    try:
        call_time = datetime.fromisoformat(call_iso.replace("Z", "+00:00"))
    except ValueError:
        return [], []

    after: list[str] = []
    active: list[str] = []
    for row in payload.get("data") or []:
        attrs = row.get("attributes") or {}
        name = str(attrs.get("name") or f"№{attrs.get('number') or row['id']}").strip()
        stage_ref = (((row.get("relationships") or {}).get("stage") or {}).get("data") or {})
        _stage_name, kind = stages.get(str(stage_ref.get("id") or ""), ("", ""))
        created = str(attrs.get("created-at") or "")
        try:
            made = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if made >= call_time:
            after.append(name)
        elif kind not in INACTIVE_STAGE_KINDS:
            active.append(name)
    return after, active


def check_inbound_calls(
    conn: sqlite3.Connection, client: SynergyClient, settings: Settings,
    limit: int = 40, recheck_hours: int = 0,
) -> int:
    """Проверить входящие звонки: завели ли по ним заявку.

    Клиент часто звонит менеджеру напрямую и просит технику. Если менеджер не
    оформил заявку, о просьбе не знает никто — это и есть потерянный заказ.
    Здесь дешёвая проверка по метаданным: нашёлся ли клиент в CRM и появилась
    ли заявка в окне после разговора.

    Проверяем не сразу: `inbound_wait_hours` даёт менеджеру время оформить
    заявку самому. Иначе список наполнится теми, кто как раз всё делает верно.
    """
    ready_before = (
        datetime.now(timezone.utc) - timedelta(hours=settings.inbound_wait_hours)
    ).isoformat()
    # Только отдел продаж: клиенты звонят напрямую им, и именно их заявки
    # теряются. Весь остальной входящий поток компании сюда не относится.
    rows = conn.execute(
        """
        SELECT k.uid, k.client_phone, k.started_at FROM calls k
        LEFT JOIN inbound_checks c ON c.call_uid = k.uid
        WHERE k.direction = 'in' AND k.duration_sec >= ?
          AND k.started_at <= ? AND c.call_uid IS NULL
          AND k.vats_login IN (SELECT vats_login FROM managers WHERE dept = ? AND active = 1)
        ORDER BY k.started_at DESC
        LIMIT ?
        """,
        (settings.inbound_min_duration_sec, ready_before, settings.sales_dept, limit),
    ).fetchall()
    if not rows:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    stages = load_stages(client)
    done = 0
    for row in rows:
        contact = find_contact(client, row["client_phone"])
        if contact is None:
            save_inbound_check(
                conn, call_uid=row["uid"], contact_id=None, contact_found=0,
                contact_name=None, company_name=None, orders_after=0,
                order_names="", checked_at=now,
            )
            conn.commit()
            done += 1
            continue
        contact_id = contact["id"]
        attrs = contact.get("attributes") or {}
        after, active = contact_orders_around(client, contact_id, row["started_at"], stages)
        save_inbound_check(
            conn, call_uid=row["uid"], contact_id=contact_id, contact_found=1,
            contact_name=str(attrs.get("as-string") or "").strip(),
            company_name=contact_company(client, contact_id),
            orders_after=len(after), order_names="; ".join(after),
            active_orders=len(active), active_names="; ".join(active[:5]),
            checked_at=now,
        )
        # Фиксируем каждую проверку: обход сотни звонков идёт минутами, и по
        # незакрытой транзакции снаружи не видно, сколько уже сделано.
        conn.commit()
        done += 1
        if done % 25 == 0:
            logger.info("входящих проверено %s из %s", done, len(rows))
    conn.commit()
    logger.info("входящих проверено: %s", done)
    return done


def manager_user_ids(conn: sqlite3.Connection) -> list[str]:
    """Идентификаторы менеджеров в Synergy — по ним отбираются задачи."""
    return [
        str(row["synergy_user"])
        for row in conn.execute(
            "SELECT synergy_user FROM managers WHERE active = 1 AND synergy_user IS NOT NULL"
        )
    ]


def refresh_tasks(
    conn: sqlite3.Connection, client: SynergyClient, settings: Settings,
    since: str, until: str,
) -> int:
    """Пересобрать задачи по уже проверенным звонкам за период.

    Отдельно от проверки карточек: та стоит шесть обращений на звонок, а здесь
    хватает одного списка задач на период. Нужно, чтобы починить историю —
    до 17.09.2026 задачи отбирались фильтром, которого у Synergy нет, и по всем
    звонкам подряд стояло «задача не поставлена».
    """
    rows = conn.execute(
        """
        SELECT k.uid, k.started_at, c.contact_id FROM calls k
        JOIN card_checks c ON c.call_uid = k.uid
        WHERE k.local_date BETWEEN ? AND ? AND k.direction = 'out'
          AND k.duration_sec >= ? AND c.contact_found = 1 AND c.contact_id IS NOT NULL
        ORDER BY k.started_at
        """,
        (since, until, settings.talk_threshold_sec),
    ).fetchall()
    if not rows:
        return 0
    tasks = load_tasks_index(client, manager_user_ids(conn), since=since)
    found = 0
    for row in rows:
        made = tasks_after_call(tasks, row["contact_id"], row["started_at"],
                                settings.card_window_min)
        conn.execute(
            "UPDATE card_checks SET task_created = ? WHERE call_uid = ?",
            (int(bool(made)), row["uid"]),
        )
        for task in made:
            save_call_task(conn, call_uid=row["uid"], is_demo=0, **task)
        found += len(made)
    conn.commit()
    logger.info("задачи за %s…%s: звонков %s, задач привязано %s",
                since, until, len(rows), found)
    return found


def load_tasks_index(
    client: SynergyClient, user_ids: Iterable[str], since: str, max_pages: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    """Задачи менеджеров с даты `since`, разложенные по контактам.

    Отбирать задачи по контакту нельзя: `filter[contact-id]` на `diaries`
    Synergy отвечает 400. Зато фильтр по автору работает, а задач у менеджера
    десятки в месяц — дешевле забрать их разом и разложить здесь. Заодно это
    одно обращение на период вместо одного на каждый звонок.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for user_id in user_ids:
        if not user_id:
            continue
        for page in range(1, max_pages + 1):
            try:
                # Связь с контактом приходит только по `include`: без него
                # у задачи есть ссылка на контакт, но нет его идентификатора.
                payload = client.get(
                    "diaries", include="contact,responsible", per_page=100, page=page,
                    sort="-created-at", **{"filter[user-id]": str(user_id)},
                )
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("задачи пользователя %s не прочитались: %s", user_id, exc)
                break
            rows = payload.get("data") or []
            if not rows:
                break
            names = {
                str(item["id"]): str((item.get("attributes") or {}).get("as-string") or "")
                for item in payload.get("included") or []
                if item.get("type") == "users"
            }
            too_old = False
            for row in rows:
                attrs = row.get("attributes") or {}
                created = str(attrs.get("created-at") or "")
                if created[:10] < since:
                    too_old = True
                    continue
                ref = ((row.get("relationships") or {}).get("contact") or {}).get("data")
                if not ref:
                    continue
                index.setdefault(str(ref["id"]), []).append({
                    "task_id": str(row["id"]),
                    "name": str(attrs.get("name") or ""),
                    "created_at": created,
                    "due_date": str(attrs.get("due-date") or ""),
                    "status": str(attrs.get("status") or ""),
                    "completed_at": str(attrs.get("completed-at") or ""),
                    "responsible": names.get(str(attrs.get("responsible-id") or ""), ""),
                })
            if too_old:
                break
    logger.info("задачи: контактов с задачами %s", len(index))
    return index


def tasks_after_call(
    index: dict[str, list[dict[str, Any]]], contact_id: str, after_iso: str, window_min: int,
) -> list[dict[str, Any]]:
    """Задачи по контакту, поставленные в окне после звонка."""
    try:
        call_time = datetime.fromisoformat(after_iso.replace("Z", "+00:00"))
    except ValueError:
        return []
    limit = call_time + timedelta(minutes=window_min)
    out = []
    for task in index.get(str(contact_id), []):
        try:
            made = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if call_time <= made <= limit:
            out.append(task)
    return sorted(out, key=lambda t: t["created_at"])


def check_pending_cards(
    conn: sqlite3.Connection, client: SynergyClient, settings: Settings, limit: int,
    refresh: bool = False,
) -> int:
    """Проверить карточки по любым непроверенным звонкам, от свежих к старым.

    Нужно, чтобы история заполнялась сама: собрать звонки за две недели дёшево,
    а карточек там девять сотен, и проверка каждой — шесть обращений к Synergy.
    Таймер берёт порцию за раз и постепенно догоняет.
    """
    condition = "c.call_uid IS NULL"
    if refresh:
        condition = "(c.call_uid IS NULL OR (c.contact_found = 1 AND c.contact_name IS NULL))"
    rows = conn.execute(
        f"""
        SELECT k.uid, k.client_phone, k.started_at, k.local_date FROM calls k
        LEFT JOIN card_checks c ON c.call_uid = k.uid
        WHERE k.direction = 'out' AND k.duration_sec >= ? AND {condition}
        ORDER BY k.started_at DESC
        LIMIT ?
        """,
        (settings.talk_threshold_sec, limit),
    ).fetchall()
    if not rows:
        return 0
    by_day: dict[str, int] = {}
    for row in rows:
        by_day[row["local_date"]] = by_day.get(row["local_date"], 0) + 1
    tasks = load_tasks_index(client, manager_user_ids(conn), since=min(by_day))
    done = 0
    for day, _count in by_day.items():
        done += check_cards(conn, client, settings, day, limit=limit - done,
                            refresh=refresh, tasks=tasks)
        if done >= limit:
            break
    logger.info("догнано карточек: %s", done)
    return done


def check_cards(
    conn: sqlite3.Connection, client: SynergyClient, settings: Settings, day: str,
    limit: int = 0, refresh: bool = False,
    tasks: dict[str, list[dict[str, Any]]] | None = None,
) -> int:
    """Проверить карточки клиентов по состоявшимся звонкам за день.

    Обычно берём только непроверённые звонки. С `refresh` захватываем и те,
    что проверялись до появления развёрнутого отчёта: у них в базе есть
    галочки, но нет ни имени клиента, ни компании, ни заявок.

    `tasks` — готовый указатель задач по контактам. Когда его не передали,
    строим на этот день сами: задачи отбираются по автору, а не по контакту,
    поэтому дешевле взять их одним запросом на период.
    """
    condition = "c.call_uid IS NULL"
    if refresh:
        condition = "(c.call_uid IS NULL OR (c.contact_found = 1 AND c.contact_name IS NULL))"
    rows = conn.execute(
        f"""
        SELECT k.uid, k.client_phone, k.started_at FROM calls k
        LEFT JOIN card_checks c ON c.call_uid = k.uid
        WHERE k.local_date = ? AND k.direction = 'out'
          AND k.duration_sec >= ? AND {condition}
        ORDER BY k.started_at
        """,
        (day, settings.talk_threshold_sec),
    ).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        logger.info("карточек проверено за %s: 0", day)
        return 0

    stages = load_stages(client)
    if tasks is None:
        tasks = load_tasks_index(client, manager_user_ids(conn), since=day)
    now = datetime.now(timezone.utc).isoformat()
    done = 0
    for row in rows:
        contact = find_contact(client, row["client_phone"])
        if contact is None:
            save_card_check(
                conn, call_uid=row["uid"], contact_id=None, contact_found=0,
                need_filled=None, objects_filled=None, inn_filled=None, task_created=None,
                orders_count=None, deals_count=None, contact_name=None,
                company_name=None, need_value=None, checked_at=now, is_demo=0,
            )
            done += 1
            continue

        contact_id = contact["id"]
        attrs = contact.get("attributes") or {}
        customs = attrs.get("customs") or {}

        def filled(field: str | None) -> int:
            if not field:
                return 0
            value = customs.get(field)
            if isinstance(value, list):
                return int(bool([v for v in value if str(v).strip()]))
            return int(bool(str(value).strip())) if value is not None else 0

        objects = max(filled(settings.field_objects), filled(settings.field_objects_extra))
        made_tasks = tasks_after_call(
            tasks, contact_id, row["started_at"], settings.card_window_min)
        save_card_check(
            conn,
            call_uid=row["uid"],
            contact_id=contact_id,
            contact_found=1,
            need_filled=filled(settings.field_need),
            objects_filled=objects,
            inn_filled=filled(settings.field_inn),
            task_created=int(bool(made_tasks)),
            orders_count=client.count(f"contacts/{contact_id}/orders"),
            deals_count=client.count(f"contacts/{contact_id}/deals"),
            # Пустая строка, а не NULL: NULL здесь означает «ещё не проверяли»,
            # и по нему отбираются строки на перепроверку. Клиент без имени
            # или без компании — это проверенный факт, а не пробел.
            contact_name=str(attrs.get("as-string") or "").strip(),
            company_name=contact_company(client, contact_id),
            need_value=text_of(customs.get(settings.field_need)),
            checked_at=now,
            is_demo=0,
        )
        for order in orders_after_call(client, contact_id, row["started_at"],
                                       settings.order_window_hours, stages):
            save_call_order(conn, call_uid=row["uid"], is_demo=0, **order)
        for task in made_tasks:
            save_call_task(conn, call_uid=row["uid"], is_demo=0, **task)
        done += 1
    conn.commit()
    logger.info("карточек проверено за %s: %s", day, done)
    return done
