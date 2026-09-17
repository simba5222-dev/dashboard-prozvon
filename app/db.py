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
    in_group      INTEGER NOT NULL DEFAULT 1,  -- 0 — чужой звонок, взят ради разбора заявки
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
    objects_filled    INTEGER,
    inn_filled        INTEGER,
    task_created      INTEGER,
    -- Частота заказов — не то, что менеджер вписывает, а факт из CRM:
    -- сколько у клиента заявок и сколько из них дошло до сделки.
    orders_count      INTEGER,
    deals_count       INTEGER,
    -- Для развёрнутого отчёта: не только «заполнено или нет», но и что именно.
    contact_name      TEXT,
    company_name      TEXT,
    need_value        TEXT,
    checked_at        TEXT NOT NULL,
    is_demo           INTEGER NOT NULL DEFAULT 0
);

-- Заявки, заведённые после звонка. Одна строка на заявку: по ним видно,
-- сколько менеджер создал, на кого их назначили и чем они кончились.
CREATE TABLE IF NOT EXISTS call_orders (
    order_id      TEXT NOT NULL,
    call_uid      TEXT NOT NULL REFERENCES calls (uid),
    name          TEXT,
    created_at    TEXT,
    responsible   TEXT,
    stage_name    TEXT,
    stage_kind    TEXT,          -- opened / won / lost / пусто для промежуточных
    amount        REAL,
    is_demo       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (order_id, call_uid)
);
CREATE INDEX IF NOT EXISTS idx_call_orders_call ON call_orders (call_uid);

-- Задачи, поставленные после звонка. Отдельной строкой, а не галочкой:
-- «задача есть» и «задача — перезвонить 17-го с готовым расчётом» — разные
-- сведения, и руководителю нужно второе.
CREATE TABLE IF NOT EXISTS call_tasks (
    task_id       TEXT NOT NULL,
    call_uid      TEXT NOT NULL REFERENCES calls (uid),
    name          TEXT,
    created_at    TEXT,
    due_date      TEXT,
    status        TEXT,          -- opened / completed / ...
    responsible   TEXT,
    completed_at  TEXT,
    is_demo       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, call_uid)
);
CREATE INDEX IF NOT EXISTS idx_call_tasks_call ON call_tasks (call_uid);

-- Разбор заявки: почему она не дошла до сделки. Считается по звонкам с
-- клиентом после её создания, поэтому живёт отдельно от самой заявки —
-- заявка приходит из CRM, а разбор делаем мы.
CREATE TABLE IF NOT EXISTS order_reports (
    order_id      TEXT PRIMARY KEY,
    contact_id    TEXT,
    calls_count   INTEGER NOT NULL DEFAULT 0,
    verdict_json  TEXT,
    created_at    TEXT NOT NULL
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
# Четыре пункта, которые менеджер обязан заполнить после разговора.
# Пятый — частота заказов — сюда не входит: его не вписывают руками,
# он считается по заявкам и сделкам клиента.
CARD_FIELDS = (
    ("need_filled", "потребность в технике"),
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
    # Писателей больше одного: сборщик по таймеру, разбор записей и разбор
    # заявок работают одновременно. Без ожидания второй писатель получает
    # «database is locked» и падает посреди работы — так уже терялся час
    # распознавания. Тридцати секунд хватает: длинных транзакций здесь нет.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# Колонки, появившиеся после первых установок. «CREATE TABLE IF NOT EXISTS»
# задним числом не работает: таблица уже есть, и новые колонки в ней сами не
# заведутся — дописываем их по одной. Порядок важен только для чтения глазами.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("card_checks", "contact_name", "TEXT"),
    ("card_checks", "company_name", "TEXT"),
    ("card_checks", "need_value", "TEXT"),
    # Разбирая заявку, мы забираем и звонки чужих менеджеров — тех, кому её
    # передали. В счётчиках прозвона им не место, поэтому они помечены нулём.
    ("calls", "in_group", "INTEGER NOT NULL DEFAULT 1"),
)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, kind in ADDED_COLUMNS:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
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
    row.setdefault("in_group", 1)
    cur = conn.execute(
        """
        INSERT INTO calls (uid, vats_login, client_phone, direction, status,
                           started_at, local_date, local_hour, wait_sec,
                           duration_sec, record_url, in_group, is_demo, fetched_at)
        VALUES (:uid, :vats_login, :client_phone, :direction, :status,
                :started_at, :local_date, :local_hour, :wait_sec,
                :duration_sec, :record_url, :in_group, :is_demo, :fetched_at)
        ON CONFLICT (uid) DO NOTHING
        """,
        row,
    )
    return cur.rowcount > 0


def save_card_check(conn: sqlite3.Connection, **row: Any) -> None:
    # Подробности для развёрнутого отчёта появились позже галочек, и не всякий
    # вызов их знает: демо-данные их не выдумывают, тесты проверяют дисциплину.
    for extra in ("contact_name", "company_name", "need_value"):
        row.setdefault(extra, None)
    conn.execute(
        """
        INSERT INTO card_checks (call_uid, contact_id, contact_found, need_filled,
                                 objects_filled, inn_filled, task_created,
                                 orders_count, deals_count, contact_name,
                                 company_name, need_value, checked_at, is_demo)
        VALUES (:call_uid, :contact_id, :contact_found, :need_filled,
                :objects_filled, :inn_filled, :task_created,
                :orders_count, :deals_count, :contact_name,
                :company_name, :need_value, :checked_at, :is_demo)
        ON CONFLICT (call_uid) DO UPDATE SET
            contact_id       = excluded.contact_id,
            contact_found    = excluded.contact_found,
            need_filled      = excluded.need_filled,
            objects_filled   = excluded.objects_filled,
            inn_filled       = excluded.inn_filled,
            task_created     = excluded.task_created,
            orders_count     = excluded.orders_count,
            deals_count      = excluded.deals_count,
            contact_name     = excluded.contact_name,
            company_name     = excluded.company_name,
            need_value       = excluded.need_value,
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


def save_call_order(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO call_orders (order_id, call_uid, name, created_at, responsible,
                                 stage_name, stage_kind, amount, is_demo)
        VALUES (:order_id, :call_uid, :name, :created_at, :responsible,
                :stage_name, :stage_kind, :amount, :is_demo)
        ON CONFLICT (order_id, call_uid) DO UPDATE SET
            responsible = excluded.responsible,
            stage_name  = excluded.stage_name,
            stage_kind  = excluded.stage_kind,
            amount      = excluded.amount
        """,
        row,
    )


def save_call_task(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO call_tasks (task_id, call_uid, name, created_at, due_date,
                                status, responsible, completed_at, is_demo)
        VALUES (:task_id, :call_uid, :name, :created_at, :due_date,
                :status, :responsible, :completed_at, :is_demo)
        ON CONFLICT (task_id, call_uid) DO UPDATE SET
            name         = excluded.name,
            due_date     = excluded.due_date,
            status       = excluded.status,
            responsible  = excluded.responsible,
            completed_at = excluded.completed_at
        """,
        row,
    )


def save_order_report(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO order_reports (order_id, contact_id, calls_count, verdict_json, created_at)
        VALUES (:order_id, :contact_id, :calls_count, :verdict_json, :created_at)
        ON CONFLICT (order_id) DO UPDATE SET
            contact_id   = excluded.contact_id,
            calls_count  = excluded.calls_count,
            verdict_json = excluded.verdict_json,
            created_at   = excluded.created_at
        """,
        row,
    )


def has_any_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM calls LIMIT 1").fetchone() is not None
