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
from app.db import save_call, save_card_check, upsert_manager
from app.stats import local_parts

logger = logging.getLogger(__name__)

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


def sync_managers(conn: sqlite3.Connection, client: SynergyClient, group_name: str) -> list[str]:
    """Обновить список менеджеров из группы Synergy. Возвращает фамилии."""
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
            is_demo=0,
        )
        if not attrs.get("disabled"):
            surnames.append(surname_of(name))
    conn.commit()
    logger.info("группа «%s»: активных менеджеров %s", group_name, len(surnames))
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
            if attrs.get("direction") != "outgoing":
                continue
            author, _ = parse_author((attrs.get("customs") or {}).get(CALL_AUTHOR_FIELD))
            surname = surname_of(author)
            if surname not in known:
                continue
            seen += 1
            started = attrs.get("started-at") or attrs.get("created-at") or ""
            local_date, local_hour = local_parts(started, settings.timezone_offset_hours)
            if save_call(
                conn, uid=str(item["id"]), vats_login=surname,
                client_phone=str(attrs.get("dst-phone-number") or ""),
                direction="out", status=str(attrs.get("status") or ""),
                started_at=started, local_date=local_date, local_hour=local_hour,
                wait_sec=int(float(attrs.get("wait") or 0)),
                duration_sec=int(float(attrs.get("duration") or 0)),
                record_url=attrs.get("recording") or None, is_demo=0, fetched_at=now,
            ):
                new += 1
        if page % 20 == 0:
            conn.commit()
            logger.info("просмотрено страниц %s, звонков менеджеров %s", page, seen)
        if too_old:
            break
    conn.commit()
    logger.info("период %s…%s: найдено %s, новых %s", since, until, seen, new)
    return new, seen


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
        attrs = item["attributes"]
        if attrs.get("direction") != "outgoing":
            continue
        author, _ext = parse_author((attrs.get("customs") or {}).get(CALL_AUTHOR_FIELD))
        surname = surname_of(author)
        if surname not in known:
            continue
        seen += 1
        started = attrs.get("started-at") or attrs.get("created-at") or ""
        local_date, local_hour = local_parts(started, settings.timezone_offset_hours)
        if save_call(
            conn,
            uid=str(item["id"]),
            vats_login=surname,
            client_phone=str(attrs.get("dst-phone-number") or ""),
            direction="out",
            status=str(attrs.get("status") or ""),
            started_at=started,
            local_date=local_date,
            local_hour=local_hour,
            wait_sec=int(float(attrs.get("wait") or 0)),
            duration_sec=int(float(attrs.get("duration") or 0)),
            record_url=attrs.get("recording") or None,
            is_demo=0,
            fetched_at=now,
        ):
            new += 1
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


def has_task_after_call(client: SynergyClient, contact_id: str, after_iso: str, window_min: int) -> bool:
    """Поставлена ли задача по контакту вскоре после звонка."""
    try:
        rows = client.get("diaries", **{"filter[contact-id]": contact_id,
                                        "per_page": 20, "sort": "-created-at"}).get("data") or []
    except (httpx.HTTPError, ValueError):
        return False
    try:
        call_time = datetime.fromisoformat(after_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    limit = call_time + timedelta(minutes=window_min)
    for task in rows:
        created = (task.get("attributes") or {}).get("created-at")
        if not created:
            continue
        try:
            made = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if call_time <= made <= limit:
            return True
    return False


def check_pending_cards(
    conn: sqlite3.Connection, client: SynergyClient, settings: Settings, limit: int,
) -> int:
    """Проверить карточки по любым непроверенным звонкам, от свежих к старым.

    Нужно, чтобы история заполнялась сама: собрать звонки за две недели дёшево,
    а карточек там девять сотен, и проверка каждой — шесть обращений к Synergy.
    Таймер берёт порцию за раз и постепенно догоняет.
    """
    rows = conn.execute(
        """
        SELECT k.uid, k.client_phone, k.started_at, k.local_date FROM calls k
        LEFT JOIN card_checks c ON c.call_uid = k.uid
        WHERE k.direction = 'out' AND k.duration_sec >= ? AND c.call_uid IS NULL
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
    done = 0
    for day, _count in by_day.items():
        done += check_cards(conn, client, settings, day, limit=limit - done)
        if done >= limit:
            break
    logger.info("догнано карточек: %s", done)
    return done


def check_cards(
    conn: sqlite3.Connection, client: SynergyClient, settings: Settings, day: str, limit: int = 0,
) -> int:
    """Проверить карточки клиентов по состоявшимся звонкам за день."""
    rows = conn.execute(
        """
        SELECT k.uid, k.client_phone, k.started_at FROM calls k
        LEFT JOIN card_checks c ON c.call_uid = k.uid
        WHERE k.local_date = ? AND k.direction = 'out'
          AND k.duration_sec >= ? AND c.call_uid IS NULL
        ORDER BY k.started_at
        """,
        (day, settings.talk_threshold_sec),
    ).fetchall()
    if limit:
        rows = rows[:limit]

    now = datetime.now(timezone.utc).isoformat()
    done = 0
    for row in rows:
        contact = find_contact(client, row["client_phone"])
        if contact is None:
            save_card_check(
                conn, call_uid=row["uid"], contact_id=None, contact_found=0,
                need_filled=None, objects_filled=None, inn_filled=None, task_created=None,
                orders_count=None, deals_count=None, checked_at=now, is_demo=0,
            )
            done += 1
            continue

        contact_id = contact["id"]
        customs = (contact.get("attributes") or {}).get("customs") or {}

        def filled(field: str | None) -> int:
            if not field:
                return 0
            value = customs.get(field)
            if isinstance(value, list):
                return int(bool([v for v in value if str(v).strip()]))
            return int(bool(str(value).strip())) if value is not None else 0

        objects = max(filled(settings.field_objects), filled(settings.field_objects_extra))
        save_card_check(
            conn,
            call_uid=row["uid"],
            contact_id=contact_id,
            contact_found=1,
            need_filled=filled(settings.field_need),
            objects_filled=objects,
            inn_filled=filled(settings.field_inn),
            task_created=int(has_task_after_call(
                client, contact_id, row["started_at"], settings.card_window_min)),
            orders_count=client.count(f"contacts/{contact_id}/orders"),
            deals_count=client.count(f"contacts/{contact_id}/deals"),
            checked_at=now,
            is_demo=0,
        )
        done += 1
    conn.commit()
    logger.info("карточек проверено за %s: %s", day, done)
    return done
