"""Отчёт «кто сколько добавил техники» — один день или произвольный период.

Общий код для таймера (`scripts/transport_daily_report.py`, каждое утро за
вчера) и для команды `/период` в группе «Транспорт СРМ» (`report_bot.py`,
по запросу на произвольные даты). Источник — локальная копия
`transport_cards` (`sync_transports.py`), поля `created_at`/`responsible_name`
подтянуты оттуда же 03.11.2026.
"""

from __future__ import annotations

import sqlite3
from datetime import date


def build_report(conn: sqlite3.Connection, date_from: str, date_to: str) -> str:
    """date_from/date_to — ISO (ГГГГ-ММ-ГГ), включительно. Один день — одинаковые значения."""
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(responsible_name, ''), 'без ответственного'),
               COUNT(*)
        FROM transport_cards
        WHERE substr(created_at, 1, 10) BETWEEN ? AND ?
          AND COALESCE(status, '') <> 'Добавлено ИИ'
        GROUP BY 1
        ORDER BY 2 DESC
        """,
        (date_from, date_to),
    ).fetchall()

    total = sum(n for _, n in rows)

    if date_from == date_to:
        header = f"Транспорт СРМ, {date.fromisoformat(date_from):%d.%m.%Y}"
    else:
        header = (f"Транспорт СРМ, {date.fromisoformat(date_from):%d.%m.%Y} — "
                  f"{date.fromisoformat(date_to):%d.%m.%Y}")

    if not rows:
        return f"{header}: новых карточек техники не было."

    lines = [f"{header} — добавлено карточек техники: {total}", ""]
    for name, count in rows:
        lines.append(f"{name}: {count}")
    return "\n".join(lines)


def added_cards(conn: sqlite3.Connection, date_from: str, date_to: str,
                responsible_name: str | None = None) -> list[sqlite3.Row]:
    """Карточки техники, заведённые в Synergy за период — для личной страницы
    поисковика (`/search/manager/...`, добавлено 03.11.2026). Не путать с
    `search_by_type`/`search_feed` в app/stats.py — те берут ленту ПРАВОК
    (`activities`, и там шире: редактирование уже существующей карточки
    тоже считается), а здесь именно факт появления новой карточки."""
    # Карточки со статусом «Добавлено ИИ» создаёт робот под токеном владельца —
    # они не работа менеджера и в «кто сколько добавил» не входят (15.11.2026).
    where = ["substr(created_at, 1, 10) BETWEEN ? AND ?", "COALESCE(status, '') <> 'Добавлено ИИ'"]
    params: list[str] = [date_from, date_to]
    if responsible_name:
        where.append("responsible_name = ?")
        params.append(responsible_name)
    return conn.execute(
        f"""
        SELECT id, name, type_name, contact_name, responsible_name, created_at
        FROM transport_cards
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC
        """,
        params,
    ).fetchall()


def parse_ru_date(token: str, *, default_year: int) -> str | None:
    """«01.10», «01.10.2026», «2026-10-01» -> ISO. None, если не разобралось."""
    from datetime import datetime

    token = token.strip()
    for fmt, needs_year in (("%d.%m.%Y", False), ("%d.%m.%y", False),
                            ("%Y-%m-%d", False), ("%d.%m", True)):
        try:
            dt = datetime.strptime(token, fmt)
        except ValueError:
            continue
        if needs_year:
            dt = dt.replace(year=default_year)
        return dt.date().isoformat()
    return None
