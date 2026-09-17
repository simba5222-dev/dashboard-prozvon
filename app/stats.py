"""Подсчёты по звонкам. Ничего не ходит в сеть — только чтение из SQLite.

Дневные итоги намеренно не материализуются в отдельную таблицу: строк мало,
запрос дешёвый, а лишняя таблица — это ещё одно место, где данные разъезжаются.
"""

from __future__ import annotations

import json
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


def managers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM managers WHERE active = 1 ORDER BY display_name"
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
    where = ["k.local_date BETWEEN ? AND ?", "k.direction = 'out'", "k.duration_sec >= ?"]
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
        f"SELECT * FROM call_orders WHERE call_uid IN ({marks}) ORDER BY created_at",
        uids,
    ):
        orders.setdefault(row["call_uid"], []).append(dict(row))
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


def period_summary(
    conn: sqlite3.Connection,
    days: int,
    *,
    default_plan: int,
    threshold_sec: int,
    today: str,
) -> list[dict[str, Any]]:
    """Динамика по дням: сколько звонков и дозвонов в каждый из последних дней."""
    end = date.fromisoformat(today)
    out: list[dict[str, Any]] = []
    for i in range(days - 1, -1, -1):
        day = (end - timedelta(days=i)).isoformat()
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN duration_sec >= ? THEN 1 ELSE 0 END) AS talked
            FROM calls WHERE local_date = ? AND direction = 'out'
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
