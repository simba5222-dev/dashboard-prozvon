"""Хранилище дашборда: SQLite, схема и доступ.

Отдельная СУБД здесь не нужна: данных сотни строк в день, пишет один процесс,
а файл легко скопировать и посмотреть руками. Схема создаётся при старте —
отдельного шага миграции пока нет, таблицы только добавляются.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;

-- Менеджеры. Логин в ВАТС и пользователь в Synergy — разные системы,
-- поэтому храним оба и связываем здесь.
CREATE TABLE IF NOT EXISTS managers (
    vats_login    TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    synergy_user  TEXT,
    plan_calls    INTEGER,          -- NULL → берём общий план из настроек
    active        INTEGER NOT NULL DEFAULT 1,
    is_demo       INTEGER NOT NULL DEFAULT 0
);

-- Звонки, как их отдала ВАТС. uid — её идентификатор, он же защита от
-- задвоения при повторном опросе.
CREATE TABLE IF NOT EXISTS calls (
    uid           TEXT PRIMARY KEY,
    vats_login    TEXT NOT NULL,
    client_phone  TEXT NOT NULL,
    direction     TEXT NOT NULL,     -- out / in
    status        TEXT,              -- success / missed / ...
    started_at    TEXT NOT NULL,     -- ISO 8601, UTC
    local_date    TEXT NOT NULL,     -- YYYY-MM-DD по местному времени
    local_hour    INTEGER NOT NULL,
    wait_sec      INTEGER NOT NULL DEFAULT 0,
    duration_sec  INTEGER NOT NULL DEFAULT 0,
    record_url    TEXT,
    is_demo       INTEGER NOT NULL DEFAULT 0,
    fetched_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_day ON calls (local_date, vats_login);

-- Что менеджер внёс в карточку после разговора.
-- NULL в поле означает «не проверяли», 0 — «проверили, не заполнено».
CREATE TABLE IF NOT EXISTS card_checks (
    call_uid          TEXT PRIMARY KEY REFERENCES calls (uid),
    contact_id        TEXT,
    contact_found     INTEGER NOT NULL DEFAULT 0,
    need_filled       INTEGER,
    frequency_filled  INTEGER,
    objects_filled    INTEGER,
    inn_filled        INTEGER,
    task_created      INTEGER,
    checked_at        TEXT NOT NULL,
    is_demo           INTEGER NOT NULL DEFAULT 0
);

-- Расшифровка и разбор. Заполняется отдельно и может отставать.
CREATE TABLE IF NOT EXISTS transcripts (
    call_uid      TEXT PRIMARY KEY REFERENCES calls (uid),
    text          TEXT,
    analysis_json TEXT,
    created_at    TEXT NOT NULL,
    is_demo       INTEGER NOT NULL DEFAULT 0
);
"""

# Пять пунктов, которые менеджер обязан заполнить после разговора.
CARD_FIELDS = (
    ("need_filled", "потребность в технике"),
    ("frequency_filled", "частота заказов"),
    ("objects_filled", "объекты"),
    ("inn_filled", "ИНН компании"),
    ("task_created", "задача поставлена"),
)


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_manager(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO managers (vats_login, display_name, synergy_user, plan_calls, active, is_demo)
        VALUES (:vats_login, :display_name, :synergy_user, :plan_calls, :active, :is_demo)
        ON CONFLICT (vats_login) DO UPDATE SET
            display_name = excluded.display_name,
            synergy_user = excluded.synergy_user,
            plan_calls   = excluded.plan_calls,
            active       = excluded.active
        """,
        {
            "vats_login": row["vats_login"],
            "display_name": row["display_name"],
            "synergy_user": row.get("synergy_user"),
            "plan_calls": row.get("plan_calls"),
            "active": int(row.get("active", 1)),
            "is_demo": int(row.get("is_demo", 0)),
        },
    )


def save_call(conn: sqlite3.Connection, **row: Any) -> bool:
    """Записать звонок. Возвращает True, если он новый.

    Повторный опрос ВАТС приносит те же звонки — на это и стоит primary key.
    """
    cur = conn.execute(
        """
        INSERT INTO calls (uid, vats_login, client_phone, direction, status,
                           started_at, local_date, local_hour, wait_sec,
                           duration_sec, record_url, is_demo, fetched_at)
        VALUES (:uid, :vats_login, :client_phone, :direction, :status,
                :started_at, :local_date, :local_hour, :wait_sec,
                :duration_sec, :record_url, :is_demo, :fetched_at)
        ON CONFLICT (uid) DO NOTHING
        """,
        row,
    )
    return cur.rowcount > 0


def save_card_check(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO card_checks (call_uid, contact_id, contact_found, need_filled,
                                 frequency_filled, objects_filled, inn_filled,
                                 task_created, checked_at, is_demo)
        VALUES (:call_uid, :contact_id, :contact_found, :need_filled,
                :frequency_filled, :objects_filled, :inn_filled,
                :task_created, :checked_at, :is_demo)
        ON CONFLICT (call_uid) DO UPDATE SET
            contact_id       = excluded.contact_id,
            contact_found    = excluded.contact_found,
            need_filled      = excluded.need_filled,
            frequency_filled = excluded.frequency_filled,
            objects_filled   = excluded.objects_filled,
            inn_filled       = excluded.inn_filled,
            task_created     = excluded.task_created,
            checked_at       = excluded.checked_at
        """,
        row,
    )


def save_transcript(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO transcripts (call_uid, text, analysis_json, created_at, is_demo)
        VALUES (:call_uid, :text, :analysis_json, :created_at, :is_demo)
        ON CONFLICT (call_uid) DO UPDATE SET
            text          = excluded.text,
            analysis_json = excluded.analysis_json,
            created_at    = excluded.created_at
        """,
        row,
    )


def has_any_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM calls LIMIT 1").fetchone() is not None
