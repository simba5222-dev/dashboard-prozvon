"""Подсчёты по звонкам. Ничего не ходит в сеть — только чтение из SQLite.

Дневные итоги намеренно не материализуются в отдельную таблицу: строк мало,
запрос дешёвый, а лишняя таблица — это ещё одно место, где данные разъезжаются.
"""

from __future__ import annotations

import json

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import CARD_FIELDS


def local_now(offset_hours: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=offset_hours)


def local_parts(started_at: str, offset_hours: int) -> tuple[str, int]:
    """Из времени ВАТС (UTC) получить местную дату и час."""
    raw = started_at.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(timezone.utc) + timedelta(hours=offset_hours)
    return local.strftime("%Y-%m-%d"), local.hour


@dataclass
class ManagerDay:
    """Итог дня по одному менеджеру."""

    vats_login: str
    display_name: str
    plan: int
    total: int = 0            # все исходящие
    talked: int = 0           # дольше порога — «поговорил»
    short: int = 0            # короче порога — «не дозвонился»
    talk_seconds: int = 0
    by_hour: dict[int, int] = field(default_factory=dict)
    checked: int = 0          # по скольким состоявшимся карточка проверена
    filled: dict[str, int] = field(default_factory=dict)
    is_demo: bool = False

    @property
    def plan_percent(self) -> int:
        return round(self.total / self.plan * 100) if self.plan else 0

    @property
    def talk_minutes(self) -> int:
        return round(self.talk_seconds / 60)

    @property
    def avg_talk_sec(self) -> int:
        return round(self.talk_seconds / self.talked) if self.talked else 0

    @property
    def discipline_percent(self) -> int:
        """Доля состоявшихся звонков, по которым карточка заполнена полностью."""
        if not self.checked:
            return 0
        full = min(self.filled.values()) if self.filled else 0
        return round(full / self.checked * 100)


def managers(conn: sqlite3.Connection, dept: str = "прозвон") -> list[sqlite3.Row]:
    """Сотрудники отдела. По умолчанию — прозвон: его считает весь дашборд."""
    return conn.execute(
        "SELECT * FROM managers WHERE active = 1 AND dept = ? ORDER BY display_name",
        (dept,),
    ).fetchall()


def day_summary(
    conn: sqlite3.Connection,
    day: str,
    *,
    default_plan: int,
    threshold_sec: int,
) -> list[ManagerDay]:
    """Итоги за один день по всем активным менеджерам."""
    result: list[ManagerDay] = []
    for m in managers(conn):
        md = ManagerDay(
            vats_login=m["vats_login"],
            display_name=m["display_name"],
            plan=m["plan_calls"] or default_plan,
            is_demo=bool(m["is_demo"]),
        )
        rows = conn.execute(
            """
            SELECT local_hour, duration_sec FROM calls
            WHERE local_date = ? AND vats_login = ? AND direction = 'out'
            """,
            (day, m["vats_login"]),
        ).fetchall()
        for r in rows:
            md.total += 1
            md.by_hour[r["local_hour"]] = md.by_hour.get(r["local_hour"], 0) + 1
            if r["duration_sec"] >= threshold_sec:
                md.talked += 1
                md.talk_seconds += r["duration_sec"]
            else:
                md.short += 1

        checks = conn.execute(
            """
            SELECT c.* FROM card_checks c
            JOIN calls k ON k.uid = c.call_uid
            WHERE k.local_date = ? AND k.vats_login = ? AND k.direction = 'out'
              AND k.duration_sec >= ?
            """,
            (day, m["vats_login"], threshold_sec),
        ).fetchall()
        md.checked = len(checks)
        for column, _ in CARD_FIELDS:
            md.filled[column] = sum(1 for c in checks if c[column])
        result.append(md)
    return result


def calls_of_day(
    conn: sqlite3.Connection, day: str, vats_login: str
) -> list[dict[str, Any]]:
    """Звонки менеджера за день вместе с тем, что проверено по карточке."""
    rows = conn.execute(
        """
        SELECT k.*, c.contact_found, c.need_filled,
               c.objects_filled, c.inn_filled, c.task_created,
               c.orders_count, c.deals_count,
               t.call_uid IS NOT NULL AS has_transcript
        FROM calls k
        LEFT JOIN card_checks c ON c.call_uid = k.uid
        LEFT JOIN transcripts t ON t.call_uid = k.uid
        WHERE k.local_date = ? AND k.vats_login = ?
        ORDER BY k.started_at DESC
        """,
        (day, vats_login),
    ).fetchall()
    return [dict(r) for r in rows]


# Фильтры отчёта: имя в адресе → условие SQL. Значение «1» означает «да»,
# «0» — «нет». Пусто — колонка не фильтруется.
REPORT_FLAG_FILTERS: dict[str, str] = {
    "need": "c.need_filled",
    "objects": "c.objects_filled",
    "inn": "c.inn_filled",
    "task": "c.task_created",
    "contact": "c.contact_found",
    "company": "(c.company_name IS NOT NULL AND c.company_name <> '')",
    "orders": "EXISTS (SELECT 1 FROM call_orders o WHERE o.call_uid = k.uid)",
    "transcript": "EXISTS (SELECT 1 FROM transcripts t WHERE t.call_uid = k.uid)",
    # Упущенное в CRM видно только из разбора: его складывает разборщик в
    # analysis_json. Отдельной колонки под это нет — фильтруем по содержимому.
    "missed": (
        "EXISTS (SELECT 1 FROM transcripts t WHERE t.call_uid = k.uid"
        " AND t.analysis_json LIKE '%\"missed\": [{%')"
    ),
}


def report_rows(
    conn: sqlite3.Connection, since: str, until: str,
    vats_login: str | None = None, threshold_sec: int = 15,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Развёрнутая строка на каждый состоявшийся разговор.

    Дашборд отвечает «сколько и насколько дисциплинированно», а это — «что
    именно произошло»: с кем говорили, что записали в карточку, какую задачу
    поставил, завёл ли заявку, на кого её назначили и чем она кончилась.

    Недозвоны сюда не берём: рассказывать о них нечего, а таблицу они топят.
    """
    filters = filters or {}
    # in_group = 1 — звонки менеджеров прозвона. Чужие звонки в базе тоже есть,
    # их приносит разбор заявок, но в отчёте по прозвону им не место.
    where = ["k.local_date BETWEEN ? AND ?", "k.direction = 'out'",
             "k.in_group = 1", "k.duration_sec >= ?"]
    params: list[Any] = [since, until, threshold_sec]
    if vats_login:
        where.append("k.vats_login = ?")
        params.append(vats_login)

    for name, expression in REPORT_FLAG_FILTERS.items():
        value = filters.get(name)
        if value in (None, ""):
            continue
        # «Нет» должно ловить и NULL: непроверенная карточка — это не «да».
        where.append(expression if str(value) == "1" else f"NOT COALESCE({expression}, 0)")

    if filters.get("min_sec"):
        where.append("k.duration_sec >= ?")
        params.append(int(filters["min_sec"]))

    query = (filters.get("q") or "").strip()
    if query:
        # Условие с поиском добавляется последним, поэтому его четыре значения
        # спокойно идут в хвост списка параметров.
        where.append(
            "(c.contact_name LIKE ? OR c.company_name LIKE ? OR c.need_value LIKE ?"
            " OR k.client_phone LIKE ?)"
        )
        params.extend([f"%{query}%"] * 4)

    sql = f"""
        SELECT k.uid, k.started_at, k.local_date, k.local_hour, k.client_phone,
               k.duration_sec, k.vats_login, m.display_name,
               c.contact_id, c.contact_found, c.contact_name, c.company_name,
               c.need_value, c.need_filled, c.objects_filled, c.inn_filled,
               c.task_created, c.orders_count, c.deals_count, c.checked_at,
               t.text AS transcript_text, t.analysis_json
        FROM calls k
        LEFT JOIN managers m ON m.vats_login = k.vats_login
        LEFT JOIN card_checks c ON c.call_uid = k.uid
        LEFT JOIN transcripts t ON t.call_uid = k.uid
        WHERE {' AND '.join(where)}
        ORDER BY k.started_at DESC
    """
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []

    uids = [r["uid"] for r in rows]
    # Заявки и задачи забираем одним запросом на всю таблицу, а не по строке:
    # так отчёт за две недели не превращается в тысячу чтений.
    marks = ",".join("?" * len(uids))
    orders: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        f"""
        SELECT o.*, r.verdict_json FROM call_orders o
        LEFT JOIN order_reports r ON r.order_id = o.order_id
        WHERE o.call_uid IN ({marks}) ORDER BY o.created_at
        """,
        uids,
    ):
        order = dict(row)
        # Короткая причина проигрыша нужна прямо в строке отчёта: владелец
        # смотрит таблицу целиком и не должен проваливаться в каждую заявку,
        # чтобы понять, почему она не дошла до сделки.
        verdict = _parsed_analysis(row["verdict_json"])
        order["verdict"] = verdict
        order["reason"] = (verdict or {}).get("outcome", "")
        orders.setdefault(row["call_uid"], []).append(order)
    tasks: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        f"SELECT * FROM call_tasks WHERE call_uid IN ({marks}) ORDER BY created_at",
        uids,
    ):
        tasks.setdefault(row["call_uid"], []).append(dict(row))

    out = []
    for row in rows:
        item = dict(row)
        item["orders"] = orders.get(row["uid"], [])
        item["orders_made"] = len(item["orders"])
        item["responsibles"] = sorted({o["responsible"] for o in item["orders"] if o["responsible"]})
        item["tasks"] = tasks.get(row["uid"], [])
        item["checked"] = row["checked_at"] is not None
        # Звонки, проверенные до появления отчёта, знают галочки, но не
        # подробности. Пустая колонка у них означает «ещё не дособрано», а не
        # «менеджер не внёс» — путать эти два состояния нельзя.
        item["detailed"] = row["contact_name"] is not None
        item["analysis"] = _parsed_analysis(row["analysis_json"])
        item["missed"] = (item["analysis"] or {}).get("missed") or []
        out.append(item)
    return out


def _parsed_analysis(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def report_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Итоги под таблицей — по тем же строкам, что показаны."""
    orders = [o for r in rows for o in r["orders"]]
    return {
        "calls": len(rows),
        "contacts_found": sum(1 for r in rows if r["contact_found"]),
        "needs": sum(1 for r in rows if r["need_filled"]),
        "companies": sum(1 for r in rows if r["company_name"]),
        "no_details": sum(1 for r in rows if r["checked"] and not r["detailed"]),
        "inn": sum(1 for r in rows if r["inn_filled"]),
        "tasks": sum(1 for r in rows if r["task_created"]),
        "tasks_made": sum(len(r.get("tasks") or []) for r in rows),
        "analyzed": sum(1 for r in rows if r.get("analysis")),
        "with_missed": sum(1 for r in rows if r.get("missed")),
        "missed_items": sum(len(r.get("missed") or []) for r in rows),
        "orders": len(orders),
        "won": sum(1 for o in orders if o["stage_kind"] == "won"),
        "lost": sum(1 for o in orders if o["stage_kind"] == "lost"),
        "in_work": sum(1 for o in orders if o["stage_kind"] not in ("won", "lost")),
        "unchecked": sum(1 for r in rows if not r["checked"]),
    }


def call_detail(conn: sqlite3.Connection, uid: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT k.*, m.display_name,
               c.contact_id, c.contact_found, c.need_filled,
               c.objects_filled, c.inn_filled, c.task_created,
               c.orders_count, c.deals_count, c.checked_at,
               t.text AS transcript_text, t.analysis_json
        FROM calls k
        LEFT JOIN managers m ON m.vats_login = k.vats_login
        LEFT JOIN card_checks c ON c.call_uid = k.uid
        LEFT JOIN transcripts t ON t.call_uid = k.uid
        WHERE k.uid = ?
        """,
        (uid,),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["analysis"] = _parsed_analysis(data.get("analysis_json"))
    data["missed"] = (data["analysis"] or {}).get("missed") or []
    data["tasks"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM call_tasks WHERE call_uid = ? ORDER BY created_at", (uid,))
    ]
    data["orders"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM call_orders WHERE call_uid = ? ORDER BY created_at", (uid,))
    ]
    return data


def inbound_rows(
    conn: sqlite3.Connection, since: str, until: str,
    only_open: bool = True, manager: str | None = None, min_sec: int = 0,
    dept: str = "продажи",
) -> list[dict[str, Any]]:
    """Входящие звонки менеджерам и что из них вышло.

    `only_open` оставляет те, по которым заявки нет и никто не сказал «запроса
    не было» — это и есть список на проверку. Без него виден весь поток,
    включая те звонки, по которым менеджер всё оформил сам.
    """
    where = ["k.direction = 'in'", "k.local_date BETWEEN ? AND ?",
             "k.vats_login IN (SELECT vats_login FROM managers WHERE dept = ? AND active = 1)"]
    params: list[Any] = [since, until, dept]
    if min_sec:
        where.append("k.duration_sec >= ?")
        params.append(min_sec)
    if manager:
        where.append("k.vats_login = ?")
        params.append(manager)
    if only_open:
        where.append("c.call_uid IS NOT NULL AND c.orders_after = 0 AND c.dismissed = 0")

    rows = conn.execute(
        f"""
        SELECT k.uid, k.started_at, k.local_date, k.client_phone, k.duration_sec,
               k.vats_login, k.record_url, m.display_name,
               c.contact_id, c.contact_found, c.contact_name, c.company_name,
               c.orders_after, c.order_names, c.active_names, c.checked_at, c.dismissed,
               t.text IS NOT NULL AS has_transcript, t.analysis_json,
               s.verdict_json, s.is_request, s.approved, s.created_order_id,
               s.verdict AS human_verdict
        FROM calls k
        LEFT JOIN managers m ON m.vats_login = k.vats_login
        LEFT JOIN inbound_checks c ON c.call_uid = k.uid
        LEFT JOIN transcripts t ON t.call_uid = k.uid
        LEFT JOIN screens s ON s.call_uid = k.uid
        WHERE {' AND '.join(where)}
        ORDER BY k.started_at DESC
        """,
        params,
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["analysis"] = _parsed_analysis(row["analysis_json"])
        item["screen"] = _parsed_analysis(row["verdict_json"])
        item["checked"] = row["checked_at"] is not None
        out.append(item)
    return out


def inbound_totals(
    conn: sqlite3.Connection, since: str, until: str, min_sec: int,
    dept: str = "продажи",
) -> dict[str, int]:
    """Сколько входящих, сколько проверено, по скольким нет заявки."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS all_calls,
               SUM(c.call_uid IS NOT NULL) AS checked,
               SUM(c.orders_after > 0) AS with_order,
               SUM(c.call_uid IS NOT NULL AND c.orders_after = 0 AND c.dismissed = 0) AS open_calls,
               SUM(c.dismissed = 1) AS dismissed
        FROM calls k LEFT JOIN inbound_checks c ON c.call_uid = k.uid
        WHERE k.direction = 'in' AND k.local_date BETWEEN ? AND ? AND k.duration_sec >= ?
          AND k.vats_login IN (SELECT vats_login FROM managers WHERE dept = ? AND active = 1)
        """,
        (since, until, min_sec, dept),
    ).fetchone()
    return {key: (row[key] or 0) for key in row.keys()}


def order_detail(conn: sqlite3.Connection, order_id: str) -> dict[str, Any] | None:
    """Заявка со всем, что о ней известно: звонок-источник, разбор, разговоры.

    Заявку ведёт не тот, кто её завёл, поэтому сюда попадают и звонки чужих
    менеджеров — их приносит `analyze_orders.py`, отбирая по телефону клиента.
    """
    row = conn.execute(
        """
        SELECT o.*, k.local_date, k.vats_login, k.started_at AS call_started_at,
               k.client_phone, m.display_name,
               c.contact_id, c.contact_name, c.company_name, c.need_value
        FROM call_orders o
        JOIN calls k ON k.uid = o.call_uid
        LEFT JOIN managers m ON m.vats_login = k.vats_login
        LEFT JOIN card_checks c ON c.call_uid = o.call_uid
        WHERE o.order_id = ?
        ORDER BY o.created_at
        LIMIT 1
        """,
        (order_id,),
    ).fetchone()
    if row is None:
        return None
    order = dict(row)

    report = conn.execute(
        "SELECT * FROM order_reports WHERE order_id = ?", (order_id,)
    ).fetchone()
    order["report"] = dict(report) if report else None
    order["verdict"] = _parsed_analysis(report["verdict_json"]) if report else None

    phone_digits = "".join(ch for ch in (order.get("client_phone") or "") if ch.isdigit())[-10:]
    calls = []
    for call in conn.execute(
        """
        SELECT k.uid, k.started_at, k.direction, k.duration_sec, k.vats_login,
               k.in_group, k.client_phone, m.display_name,
               t.text IS NOT NULL AS has_transcript, t.analysis_json
        FROM calls k
        LEFT JOIN managers m ON m.vats_login = k.vats_login
        LEFT JOIN transcripts t ON t.call_uid = k.uid
        WHERE k.started_at >= ? ORDER BY k.started_at
        """,
        (order["created_at"] or order["call_started_at"],),
    ):
        digits = "".join(ch for ch in (call["client_phone"] or "") if ch.isdigit())
        if not phone_digits or digits[-10:] != phone_digits:
            continue
        item = dict(call)
        item["analysis"] = _parsed_analysis(call["analysis_json"])
        calls.append(item)
    order["calls"] = calls
    return order


def orders_of_period(
    conn: sqlite3.Connection, since: str, until: str, kind: str | None = None,
) -> list[dict[str, Any]]:
    """Заявки, заведённые по звонкам прозвона за период."""
    where = ["k.local_date BETWEEN ? AND ?", "k.in_group = 1"]
    params: list[Any] = [since, until]
    if kind == "lost":
        where.append("o.stage_kind = 'lost'")
    elif kind == "open":
        where.append("o.stage_kind NOT IN ('won', 'lost')")
    elif kind == "won":
        where.append("o.stage_kind = 'won'")
    rows = conn.execute(
        f"""
        SELECT o.*, k.local_date, c.contact_name, c.company_name,
               r.calls_count, r.verdict_json
        FROM call_orders o
        JOIN calls k ON k.uid = o.call_uid
        LEFT JOIN card_checks c ON c.call_uid = o.call_uid
        LEFT JOIN order_reports r ON r.order_id = o.order_id
        WHERE {' AND '.join(where)}
        GROUP BY o.order_id
        ORDER BY o.created_at DESC
        """,
        params,
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["verdict"] = _parsed_analysis(row["verdict_json"])
        out.append(item)
    return out


def period_summary(
    conn: sqlite3.Connection,
    days: int,
    *,
    default_plan: int,
    threshold_sec: int,
    today: str,
) -> list[dict[str, Any]]:
    """Динамика по дням: сколько звонков и дозвонов в каждый из последних дней.

    Считаем только `in_group = 1` — звонки прозвона. Без этого условия в
    строку попадала вся компания (полторы тысячи исходящих в день), а планом
    оставались 120 звонков одного человека: экран показывал 692% выполнения
    там, где план был провален вдвое.

    Набор звонков и план сходятся не идеально: звонки берём по природе
    (`in_group`), а план — по активным сотрудникам. Разойдутся они только
    если человек уволится посреди недели, и это меньшее зло: считать историю
    по сегодняшнему составу значит задним числом стирать чужую работу.
    """
    end = date.fromisoformat(today)
    out: list[dict[str, Any]] = []
    for i in range(days - 1, -1, -1):
        day = (end - timedelta(days=i)).isoformat()
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN duration_sec >= ? THEN 1 ELSE 0 END) AS talked
            FROM calls
            WHERE local_date = ? AND direction = 'out' AND in_group = 1
            """,
            (threshold_sec, day),
        ).fetchone()
        n_managers = len(managers(conn)) or 1
        out.append({
            "day": day,
            "total": row["total"] or 0,
            "talked": row["talked"] or 0,
            "plan": default_plan * n_managers,
        })
    return out


def search_day(conn: sqlite3.Connection, day: str, *, threshold_sec: int) -> list[dict[str, Any]]:
    """День менеджера по поиску техники: звонки и правки в карточках.

    Его работу не видно по заявкам — заявки заводят другие. Видно по двум
    вещам: сколько он звонил поставщикам и что после этого поменялось в
    карточках транспорта. Поэтому здесь и то, и другое.
    """
    out: list[dict[str, Any]] = []
    for person in managers(conn, dept="поиск"):
        calls = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN direction = 'out' THEN 1 ELSE 0 END) outgoing,
                      SUM(CASE WHEN direction = 'in' THEN 1 ELSE 0 END) incoming,
                      SUM(CASE WHEN duration_sec >= ? THEN 1 ELSE 0 END) talked
                 FROM calls WHERE local_date = ? AND vats_login = ?""",
            (threshold_sec, day, person["vats_login"]),
        ).fetchone()
        # Правки сценариев считаем отдельно: это работа робота, а не человека,
        # и засчитывать её сотруднику нельзя.
        acts = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN scenario = '' THEN 1 ELSE 0 END) by_hand,
                      COUNT(DISTINCT CASE WHEN scenario = '' AND entity_type = 'Transport'
                                          THEN entity_id END) cards
                 FROM activities WHERE local_date = ? AND vats_login = ?""",
            (day, person["vats_login"]),
        ).fetchone()
        out.append({
            "vats_login": person["vats_login"],
            "display_name": person["display_name"],
            "ext": person["ext"],
            "calls": dict(calls) if calls else {},
            "acts": dict(acts) if acts else {},
        })
    return out


def search_feed(conn: sqlite3.Connection, day: str, vats_login: str | None = None,
                limit: int = 200, dept: str = "поиск") -> list[dict[str, Any]]:
    """Лента правок за день: что именно менял человек, без правок сценариев.

    По умолчанию — только отдел поиска техники. Без этого в ленту попадают
    все, чью работу мы собираем, и экран отдела перестаёт быть про отдел.
    """
    where = ["local_date = ?", "scenario = ''", "summary <> ''"]
    params: list[Any] = [day]
    if vats_login:
        where.append("vats_login = ?")
        params.append(vats_login)
    else:
        where.append("vats_login IN (SELECT vats_login FROM managers WHERE dept = ?)")
        params.append(dept)
    rows = conn.execute(
        f"""SELECT created_at, vats_login, entity_type, entity_id, entity_title,
                   action, summary
              FROM activities WHERE {' AND '.join(where)}
             ORDER BY created_at DESC LIMIT ?""",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def _norm(text: str) -> str:
    """Схлопнуть пробелы и неразрывные пробелы — Synergy щедра на них."""
    return re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip().lower()


def search_tasks(conn: sqlite3.Connection, days: int = 14) -> list[dict[str, Any]]:
    """Заявки в подборе и сколько по ним сделано — приблизительно.

    **Связь приблизительная, и это осознанный выбор владельца.** Точной в
    данных нет: карточка транспорта связана с поставщиком, а не с заявкой.
    Поэтому считаем по совпадению типа техники и времени: с момента передачи
    заявки в подбор смотрим, какие карточки этого типа трогал подборщик.

    Отсюда и границы применимости: если он ведёт две заявки на один тип
    техники разом, работа разделится между ними неверно — одни и те же
    карточки засчитаются обеим. Цифра годится как «сколько шевелений было по
    заявке», а не как отчёт по каждому звонку.
    """
    tasks = conn.execute(
        """SELECT order_id, entered_at, local_date, title, equipment
             FROM search_tasks WHERE local_date >= date('now', ?)
            ORDER BY entered_at DESC""",
        (f"-{days} day",),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for task in tasks:
        kinds = [_norm(k) for k in str(task["equipment"] or "").split(";") if _norm(k)]
        rows = conn.execute(
            """SELECT entity_id, entity_title, created_at, vats_login
                 FROM activities
                WHERE entity_type = 'Transport' AND scenario = ''
                  AND created_at >= ?
                  AND vats_login IN (SELECT vats_login FROM managers WHERE dept = 'поиск')""",
            (task["entered_at"],),
        ).fetchall()
        # Название карточки начинается с типа техники: «Автокран  -  -  -».
        # Этого хватает, чтобы отличить подбор крана от подбора самосвала.
        cards = {
            r["entity_id"] for r in rows
            if kinds and any(_norm(r["entity_title"]).startswith(k) for k in kinds)
        }
        out.append({
            "order_id": task["order_id"],
            "entered_at": task["entered_at"],
            "title": task["title"],
            "equipment": task["equipment"],
            "cards": len(cards),
            "touched": sum(
                1 for r in rows
                if kinds and any(_norm(r["entity_title"]).startswith(k) for k in kinds)
            ),
        })
    return out


def openai_spend(data_dir: str, day: str | None = None) -> dict[str, Any]:
    """Расход на модель за день: сколько и на что.

    Считаем сами, потому что OpenAI отдаёт расход только по админскому ключу,
    а рабочий его не видит. Один раз вопрос «почему кончился бюджет» пришлось
    выяснять раскопками по логам — больше не придётся.
    """
    from pathlib import Path

    path = Path(data_dir) / "openai_usage.jsonl"
    out: dict[str, Any] = {"day": day, "usd": 0.0, "calls": 0, "by_kind": {}}
    if not path.exists():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if day and not str(row.get("at", "")).startswith(day):
            continue
        kind = row.get("kind") or "прочее"
        slot = out["by_kind"].setdefault(kind, {"calls": 0, "usd": 0.0})
        slot["calls"] += 1
        slot["usd"] += float(row.get("usd") or 0.0)
        out["calls"] += 1
        out["usd"] += float(row.get("usd") or 0.0)
    out["usd"] = round(out["usd"], 2)
    for slot in out["by_kind"].values():
        slot["usd"] = round(slot["usd"], 3)
    return out


def openai_balance(data_dir: str, topup_usd: float, topup_at: str | None) -> dict[str, Any]:
    """Остаток на счёте модели: сколько положили минус наш расход.

    Баланс OpenAI отдаёт только админскому ключу, рабочий его не видит —
    один раз деньги кончились посреди рабочего дня, и конвейер встал молча.
    Поэтому считаем сами: владелец говорит сумму пополнения, мы вычитаем то,
    что записал наш учёт.

    Цифра приблизительная по одной причине: если кто-то тратит тот же ключ
    мимо нас, мы этого не увидим. Зато она есть и обновляется сама.
    """
    from pathlib import Path

    out: dict[str, Any] = {"topup": topup_usd, "since": topup_at, "spent": 0.0,
                           "left": topup_usd, "per_day": 0.0, "days_left": None}
    if not topup_usd or not topup_at:
        return out
    path = Path(data_dir) / "openai_usage.jsonl"
    if not path.exists():
        return out
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out

    days: set[str] = set()
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        day = str(row.get("at", ""))[:10]
        if not day or day < topup_at:
            continue
        days.add(day)
        out["spent"] += float(row.get("usd") or 0.0)

    out["spent"] = round(out["spent"], 2)
    out["left"] = round(topup_usd - out["spent"], 2)
    if days:
        out["per_day"] = round(out["spent"] / len(days), 2)
        if out["per_day"] > 0:
            out["days_left"] = int(out["left"] / out["per_day"])
    return out
