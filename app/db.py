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
    -- Отдел: «прозвон» — те, чью дисциплину считает дашборд; «продажи» — те,
    -- кому клиенты звонят напрямую. Списки разные, и смешивать их нельзя:
    -- у продаж нет плана по звонкам, а у прозвона нет входящего потока.
    dept          TEXT NOT NULL DEFAULT 'прозвон',
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
    diversion     TEXT,              -- на какой НАШ номер звонили: рекламный или прямой
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

-- Лента действий из Synergy: кто, что и когда изменил.
-- Нужна, чтобы видеть работу менеджера по поиску техники: он не заявки
-- заводит, а правит карточки транспорта — статус поставщика, комментарий,
-- дату звонка. По заявкам его работу не увидеть вовсе.
CREATE TABLE IF NOT EXISTS activities (
    id            TEXT PRIMARY KEY,   -- идентификатор события в Synergy
    created_at    TEXT NOT NULL,      -- ISO 8601, как отдала Synergy
    local_date    TEXT NOT NULL,      -- YYYY-MM-DD по местному времени
    synergy_user  TEXT,               -- кто сделал, идентификатор в Synergy
    vats_login    TEXT,               -- он же у нас, если опознан
    entity_type   TEXT,               -- Transport / Order / Contact / ...
    entity_id     TEXT,
    entity_title  TEXT,               -- «Экскаватор погрузчик - - -»
    action        TEXT,               -- update / create / create_telephony_call
    summary       TEXT,               -- «Статус: — → Не отвечает»
    changes_json  TEXT,               -- то же машинно, на случай разбора
    -- Имя сценария, если правку сделал робот, а не человек. Без этого
    -- работа автоматизаций засчитывалась бы сотруднику.
    scenario      TEXT,
    is_demo       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_activities_day ON activities (local_date, vats_login);

-- Заявки, отданные в подбор техники. Момент передачи берётся из ленты: его
-- делает менеджер отдела продаж, меняя этап на «Нужен Подбор». С этой
-- секунды и считается работа по подбору.
CREATE TABLE IF NOT EXISTS search_tasks (
    order_id    TEXT PRIMARY KEY,
    entered_at  TEXT NOT NULL,   -- когда отдали в подбор, ISO 8601
    local_date  TEXT NOT NULL,
    title       TEXT,            -- как называется заявка
    equipment   TEXT,            -- тип техники из карточки заявки
    is_demo     INTEGER NOT NULL DEFAULT 0
);

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

-- Входящие звонки менеджерам: есть ли по клиенту заявка после разговора.
-- Клиент часто звонит менеджеру напрямую, и если тот не завёл заявку, о
-- просьбе не знает никто. Эта таблица — след проверки: кого звали, нашёлся ли
-- клиент в CRM и появилась ли заявка в окне после звонка.
CREATE TABLE IF NOT EXISTS inbound_checks (
    call_uid      TEXT PRIMARY KEY REFERENCES calls (uid),
    contact_id    TEXT,
    contact_found INTEGER NOT NULL DEFAULT 0,
    contact_name  TEXT,
    company_name  TEXT,
    orders_after  INTEGER NOT NULL DEFAULT 0,   -- заявок у контакта после звонка
    order_names   TEXT,                          -- какие именно, через «;»
    active_orders INTEGER NOT NULL DEFAULT 0,   -- открытые заявки контакта на момент звонка
    active_names  TEXT,
    tasks_after   INTEGER,                       -- NULL — не проверяли
    checked_at    TEXT NOT NULL,
    -- Человек посмотрел и сказал «запроса не было». Такие строки из списка
    -- уходят, но не удаляются: по ним потом считается точность отбора.
    dismissed     INTEGER NOT NULL DEFAULT 0,
    dismissed_at  TEXT
);

-- Просев входящих: по началу разговора — был ли запрос на технику.
-- Черновая расшифровка хранится, но людям не показывается: она нужна только
-- для ответа «запрос или нет» и чтобы можно было перепроверить решение.
CREATE TABLE IF NOT EXISTS screens (
    call_uid     TEXT PRIMARY KEY REFERENCES calls (uid),
    head_text    TEXT,
    verdict_json TEXT,
    is_request   INTEGER NOT NULL DEFAULT 0,
    created_order_id TEXT,        -- заявка, которую мы завели по этому звонку
    -- Мягкий режим: находку подтверждает человек, и только после этого
    -- заводится заявка. Точность просева на 17.09.2026 — 69%, каждая третья
    -- заявка была бы мусорной, поэтому автомат отключён до 90%.
    approved     INTEGER NOT NULL DEFAULT 0,
    approved_at  TEXT,
    created_at   TEXT NOT NULL
);

-- Расшифровка и разбор. Заполняется отдельно и может отставать.
CREATE TABLE IF NOT EXISTS transport_cards (
    id          TEXT PRIMARY KEY,   -- id карточки транспорта в Synergy
    name        TEXT,               -- «Название» из карточки
    type_id     TEXT,               -- id типа техники в справочнике
    type_name   TEXT,               -- «Экскаватор погрузчик» и прочие 72 вида
    phone10     TEXT,               -- телефон, последние 10 цифр
    contact_id  TEXT,
    contact_name TEXT,
    status      TEXT,               -- «Работаем», «Не отвечает», «Выключен»
    updated_at  TEXT,
    synced_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS transport_cards_phone ON transport_cards (phone10);

-- Сверка разговора поисковика с базой: что поставщик назвал вслух против
-- того, что занесено в его карточки. Ключ — звонок: один разговор даёт
-- один вердикт, повторный разбор его переписывает.
CREATE TABLE IF NOT EXISTS search_checks (
    call_uid    TEXT PRIMARY KEY REFERENCES calls (uid),
    local_date  TEXT NOT NULL,
    vats_login  TEXT,
    phone10     TEXT,
    contact_name TEXT,
    asked_type  TEXT,               -- о чём звонил менеджер
    offered     TEXT,               -- что у поставщика есть, по разговору
    known       TEXT,               -- что уже есть в карточках
    missing     TEXT,               -- чего в карточках нет
    verdict     TEXT NOT NULL,      -- all_saved / missing / no_cards / unclear
    checked_at  TEXT NOT NULL,
    is_demo     INTEGER NOT NULL DEFAULT 0
);

-- Справочник номеров: кто нам звонит и кем он нам приходится.
--
-- Заведён 23.09.2026 по требованию владельца: проверка одного входящего
-- звонка стоила до десяти обращений в CRM (поиск контакта по четырём полям
-- в двух написаниях номера, потом заявки, потом компания). Номеров при этом
-- всего три с половиной тысячи, и меняются они редко. Поэтому номер
-- разбирается один раз и кладётся сюда, а решения по звонку принимаются
-- из этой таблицы — мгновенно и без сети.
CREATE TABLE IF NOT EXISTS numbers (
    phone10     TEXT PRIMARY KEY,
    name        TEXT,
    contact_id  TEXT,
    category    TEXT NOT NULL DEFAULT '',  -- заказчик / исполнитель / оба / неизвестный
    cards       INTEGER NOT NULL DEFAULT 0,
    types       TEXT,                      -- типы техники через запятую
    orders      INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT
);

-- Разбор звонков с рекламных линий и отметки человека к нему.
--
-- Зачем отдельная таблица. Эти звонки обрабатывает боевой сервер по своему
-- сценарию, и заявка по ним заводится ещё до всякого разбора. Здесь другое:
-- владелец и агент смотрят разговоры вместе и помечают, верно ли разобрано.
-- Из этих отметок вырастает проверочный набор — без него смену модели
-- нельзя измерить, можно только поверить.
--
-- `verdict` заполняет человек, а не машина: «верно», «неверно», «спорно».
-- Пустой вердикт значит «ещё не смотрели», и это не то же самое, что «плохо».
CREATE TABLE IF NOT EXISTS ad_calls (
    call_uid     TEXT PRIMARY KEY,
    line         TEXT NOT NULL DEFAULT '',
    engine       TEXT NOT NULL DEFAULT '',   -- чем распознано
    transcript   TEXT,
    analysis_json TEXT,
    made_at      TEXT,
    verdict      TEXT NOT NULL DEFAULT '',
    verdict_note TEXT,
    verdict_at   TEXT
);

-- НАШИ линии: номера, на которые звонят нам. ВАТС подписывает рекламные
-- линии сама — поле `telnum_name` в истории: «Авито СПБ», «Сайт МСК»,
-- «Виджет Реклама». Имя есть только у них; прямые номера сотрудников
-- приходят безымянными. Это и есть готовое различение, которое раньше
-- пытались вывести из списка менеджеров и выводили неверно.
--
-- Заполняется `scripts/sync_lines.py` из истории ВАТС: она доступна только
-- питерскому серверу, поэтому запрос уходит туда по ssh.
CREATE TABLE IF NOT EXISTS lines (
    phone10    TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',   -- как линия названа в ВАТС; пусто — прямой номер
    kind       TEXT NOT NULL DEFAULT '',   -- рекламная / прямой / неизвестная
    calls_in   INTEGER NOT NULL DEFAULT 0,
    seen_at    TEXT
);

-- Что из звонков подборщика уже ушло в заявку. Нужна, чтобы не писать
-- один и тот же вариант в ленту заявки дважды: разбор перезапускается,
-- а комментарий в CRM удалить некому.
CREATE TABLE IF NOT EXISTS search_options (
    call_uid   TEXT NOT NULL,
    order_id   TEXT NOT NULL,
    posted_at  TEXT NOT NULL,
    PRIMARY KEY (call_uid, order_id)
);

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
    # распознавания. Тридцати секунд хватает, только пока никто не держит
    # запись дольше: 17.09.2026 сборщик листал историю внутри одной транзакции
    # и разбор всё равно упал. Правило — коммитить пачками, а не в конце
    # длинного цикла с сетевыми вызовами внутри.
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
    # На какой НАШ номер звонили. ВАТС присылает это обязательным полем, и
    # только по нему различимы сценарии: рекламный номер — звонок с рекламы,
    # прямой номер менеджера — клиент позвонил ему сам. Раньше признак
    # приходил и выбрасывался, из-за чего звонки с рекламы разбирались как
    # личные и по ним заводились лишние заявки.
    ("calls", "diversion", "TEXT"),
    ("managers", "dept", "TEXT NOT NULL DEFAULT 'прозвон'"),
    ("inbound_checks", "active_orders", "INTEGER NOT NULL DEFAULT 0"),
    ("inbound_checks", "active_names", "TEXT"),
    ("screens", "created_order_id", "TEXT"),
    ("screens", "approved", "INTEGER NOT NULL DEFAULT 0"),
    ("screens", "approved_at", "TEXT"),
    # С 18.09.2026 заявка заводится сразу, а человек смотрит её потом. Здесь
    # его вердикт: ok — заявка верная, wrong — лишняя. По этим отметкам
    # считается точность на живом потоке, а не только на проверочном наборе.
    # Логин в ВАТС — латиницей и не всегда по фамилии владельца: учётки
    # переиспользуют. Поэтому соответствие строится по имени, а не по логину,
    # и хранится здесь: без него звонок из ВАТС не привязать к менеджеру.
    # Номер менеджера из CRM. Привязка звонка идёт по нему, а не по имени:
    # менеджеры приходят и уходят, номера остаются в компании. Один номер
    # бывает у нескольких человек — это нормально, он всё равно «номер отдела
    # продаж», а кто именно ответил, ВАТС говорит отдельно.
    ("managers", "phone", "TEXT"),
    # Добавочный номер в ВАТС. По нему звонок сопоставляется с человеком:
    # Synergy кладёт в исходящий именно добавочный, а её собственное поле
    # автора врёт при переиспользовании — 23.09.2026 добавочный 766 числился
    # за Ратенковым, хотя там уже работал Никитин, и 58 его звонков за день
    # ушли в чужой счёт.
    ("managers", "ext", "TEXT"),
    ("managers", "vats_user", "TEXT"),
    # «У нас много разной техники» — поставщик сказал, что есть ещё, но не
    # назвал что. Типом это не станет, а работа для менеджера — да.
    ("search_checks", "more_unnamed", "TEXT"),
    # Что сейчас стоит в поле «Позвонить» карточки транспорта. Держим копию,
    # чтобы сторож ссылок сверял её у себя и ходил в CRM только на запись.
    ("transport_cards", "call_link", "TEXT"),
    ("screens", "verdict", "TEXT NOT NULL DEFAULT ''"),
    ("screens", "verdict_at", "TEXT"),
    # Разбор заявок спрашивает разное у проваленных и у живых: у первых —
    # «где сорвалось», у вторых — «где сейчас и нужна ли помощь». Держим
    # ответы в общей таблице, но помечаем, какой вопрос задавали.
    ("order_reports", "kind", "TEXT NOT NULL DEFAULT ''"),
    ("order_reports", "stage_now", "TEXT"),
    ("order_reports", "next_step", "TEXT"),
    ("order_reports", "next_step_due", "TEXT"),
    ("order_reports", "needs_rop", "INTEGER NOT NULL DEFAULT 0"),
    ("order_reports", "rop_reason", "TEXT"),
    # След созданной задачи. Задача — строка в CRM, и повторять её создание
    # вслепую нельзя: заведётся вторая. Здесь видно, что по этой заявке уже
    # поставлено и когда.
    ("order_reports", "task_id", "TEXT"),
    ("order_reports", "task_at", "TEXT"),
    ("order_reports", "task_note", "TEXT"),
    # Участвует ли линия в разборе потока 1. Решение владельца, а не ВАТС:
    # ВАТС подписывает именем всё, что считает рекламным, включая линию для
    # соискателей и офисные номера. Колонка живёт отдельно от `kind` именно
    # потому, что `kind` переписывается ночным обновлением справочника, а
    # решение владельца переписывать нельзя.
    ("lines", "in_scope", "INTEGER NOT NULL DEFAULT 1"),
    ("lines", "scope_note", "TEXT"),
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
        INSERT INTO managers (vats_login, display_name, synergy_user, plan_calls,
                              active, dept, is_demo)
        VALUES (:vats_login, :display_name, :synergy_user, :plan_calls,
                :active, :dept, :is_demo)
        ON CONFLICT (vats_login) DO UPDATE SET
            display_name = excluded.display_name,
            synergy_user = excluded.synergy_user,
            plan_calls   = excluded.plan_calls,
            active       = excluded.active,
            dept         = excluded.dept
        """,
        {
            "vats_login": row["vats_login"],
            "display_name": row["display_name"],
            "synergy_user": row.get("synergy_user"),
            "plan_calls": row.get("plan_calls"),
            "active": int(row.get("active", 1)),
            "dept": row.get("dept") or "прозвон",
            "is_demo": int(row.get("is_demo", 0)),
        },
    )


def save_call(conn: sqlite3.Connection, **row: Any) -> bool:
    """Записать звонок. Возвращает True, если он новый.

    Повторный опрос ВАТС приносит те же звонки — на это и стоит primary key.
    """
    row.setdefault("in_group", 1)
    row.setdefault("diversion", None)
    cur = conn.execute(
        """
        INSERT INTO calls (uid, vats_login, client_phone, direction, status,
                           started_at, local_date, local_hour, wait_sec,
                           duration_sec, record_url, in_group, diversion,
                           is_demo, fetched_at)
        VALUES (:uid, :vats_login, :client_phone, :direction, :status,
                :started_at, :local_date, :local_hour, :wait_sec,
                :duration_sec, :record_url, :in_group, :diversion,
                :is_demo, :fetched_at)
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


def save_search_task(conn: sqlite3.Connection, **row: Any) -> None:
    """Запомнить заявку, отданную в подбор. Повтор обновляет тип техники."""
    conn.execute(
        """
        INSERT INTO search_tasks (order_id, entered_at, local_date, title, equipment, is_demo)
        VALUES (:order_id, :entered_at, :local_date, :title, :equipment, :is_demo)
        ON CONFLICT (order_id) DO UPDATE SET
            entered_at = excluded.entered_at,
            local_date = excluded.local_date,
            title      = excluded.title,
            equipment  = excluded.equipment
        """,
        row,
    )


def phone10(number: str | None) -> str:
    """Последние десять цифр номера.

    В CRM телефон пишут как придётся: «+7951…», «8951…», со скобками и без.
    Сравнивать их как строки бессмысленно, поэтому и карточки, и звонки
    приводятся к одному виду — к десяти цифрам без кода страны.
    """
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def save_transport_card(conn: sqlite3.Connection, **row: Any) -> None:
    """Карточка транспорта из Synergy в местный справочник."""
    conn.execute(
        """
        INSERT INTO transport_cards (id, name, type_id, type_name, phone10,
                                     contact_id, contact_name, status,
                                     updated_at, synced_at, call_link)
        VALUES (:id, :name, :type_id, :type_name, :phone10, :contact_id,
                :contact_name, :status, :updated_at, :synced_at, :call_link)
        ON CONFLICT (id) DO UPDATE SET
            name = excluded.name, type_id = excluded.type_id,
            type_name = excluded.type_name, phone10 = excluded.phone10,
            contact_id = excluded.contact_id, contact_name = excluded.contact_name,
            status = excluded.status, updated_at = excluded.updated_at,
            synced_at = excluded.synced_at, call_link = excluded.call_link
        """,
        row,
    )


def save_search_check(conn: sqlite3.Connection, **row: Any) -> None:
    """Вердикт сверки разговора с карточками. Повтор разбора переписывает."""
    conn.execute(
        """
        INSERT INTO search_checks (call_uid, local_date, vats_login, phone10,
                                   contact_name, asked_type, offered, known,
                                   missing, verdict, checked_at, is_demo,
                                   more_unnamed)
        VALUES (:call_uid, :local_date, :vats_login, :phone10, :contact_name,
                :asked_type, :offered, :known, :missing, :verdict, :checked_at,
                :is_demo, :more_unnamed)
        ON CONFLICT (call_uid) DO UPDATE SET
            phone10 = excluded.phone10, contact_name = excluded.contact_name,
            asked_type = excluded.asked_type, offered = excluded.offered,
            known = excluded.known, missing = excluded.missing,
            verdict = excluded.verdict, checked_at = excluded.checked_at,
            more_unnamed = excluded.more_unnamed
        """,
        row,
    )


def save_number(conn: sqlite3.Connection, **row: Any) -> None:
    """Запомнить, кем нам приходится номер. Повтор обновляет."""
    conn.execute(
        """
        INSERT INTO numbers (phone10, name, contact_id, category, cards, types,
                             orders, resolved_at)
        VALUES (:phone10, :name, :contact_id, :category, :cards, :types,
                :orders, :resolved_at)
        ON CONFLICT (phone10) DO UPDATE SET
            name = excluded.name, contact_id = excluded.contact_id,
            category = excluded.category, cards = excluded.cards,
            types = excluded.types, orders = excluded.orders,
            resolved_at = excluded.resolved_at
        """,
        row,
    )


def save_ad_call(conn: sqlite3.Connection, **row: Any) -> None:
    """Сохранить разбор звонка с рекламной линии. Отметку человека не трогаем."""
    conn.execute(
        """
        INSERT INTO ad_calls (call_uid, line, engine, transcript, analysis_json, made_at)
        VALUES (:call_uid, :line, :engine, :transcript, :analysis_json, :made_at)
        ON CONFLICT (call_uid) DO UPDATE SET
            line = excluded.line, engine = excluded.engine,
            transcript = excluded.transcript,
            analysis_json = excluded.analysis_json, made_at = excluded.made_at
        """,
        row,
    )


def save_ad_verdict(conn: sqlite3.Connection, call_uid: str, verdict: str,
                    note: str, at: str) -> None:
    """Отметка человека: верно разобрано или нет. Разбор при этом не трогаем."""
    conn.execute(
        "UPDATE ad_calls SET verdict = ?, verdict_note = ?, verdict_at = ? WHERE call_uid = ?",
        (verdict, note, at, call_uid),
    )


def save_line(conn: sqlite3.Connection, **row: Any) -> None:
    """Запомнить нашу линию: номер, её имя в ВАТС и что это за линия."""
    conn.execute(
        """
        INSERT INTO lines (phone10, name, kind, calls_in, seen_at)
        VALUES (:phone10, :name, :kind, :calls_in, :seen_at)
        ON CONFLICT (phone10) DO UPDATE SET
            name = excluded.name, kind = excluded.kind,
            calls_in = excluded.calls_in, seen_at = excluded.seen_at
        """,
        # `in_scope` и `scope_note` намеренно не в списке обновляемых полей:
        # это решение владельца, ночная синхронизация его не трогает.
        row,
    )


def save_activity(conn: sqlite3.Connection, **row: Any) -> bool:
    """Сохранить событие ленты. Возвращает, новое ли оно.

    Лента перечитывается с перекрытием, поэтому одно и то же событие приходит
    много раз — на это и стоит primary key.
    """
    cur = conn.execute(
        """
        INSERT INTO activities (id, created_at, local_date, synergy_user, vats_login,
                                entity_type, entity_id, entity_title, action,
                                summary, changes_json, scenario, is_demo)
        VALUES (:id, :created_at, :local_date, :synergy_user, :vats_login,
                :entity_type, :entity_id, :entity_title, :action,
                :summary, :changes_json, :scenario, :is_demo)
        ON CONFLICT (id) DO NOTHING
        """,
        row,
    )
    return cur.rowcount > 0


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


def save_inbound_check(conn: sqlite3.Connection, **row: Any) -> None:
    row.setdefault("tasks_after", None)
    row.setdefault("active_orders", 0)
    row.setdefault("active_names", "")
    conn.execute(
        """
        INSERT INTO inbound_checks (call_uid, contact_id, contact_found, contact_name,
                                    company_name, orders_after, order_names,
                                    active_orders, active_names, tasks_after, checked_at)
        VALUES (:call_uid, :contact_id, :contact_found, :contact_name,
                :company_name, :orders_after, :order_names,
                :active_orders, :active_names, :tasks_after, :checked_at)
        ON CONFLICT (call_uid) DO UPDATE SET
            contact_id    = excluded.contact_id,
            contact_found = excluded.contact_found,
            contact_name  = excluded.contact_name,
            company_name  = excluded.company_name,
            orders_after  = excluded.orders_after,
            order_names   = excluded.order_names,
            active_orders = excluded.active_orders,
            active_names  = excluded.active_names,
            tasks_after   = excluded.tasks_after,
            checked_at    = excluded.checked_at
        """,
        row,
    )


def approve_screen(conn: sqlite3.Connection, call_uid: str, when: str, back: bool = False) -> None:
    """Подтвердить находку просева — по ней заведут заявку — или снять подтверждение."""
    conn.execute(
        "UPDATE screens SET approved = ?, approved_at = ? WHERE call_uid = ?",
        (0 if back else 1, None if back else when, call_uid),
    )


def judge_screen(conn: sqlite3.Connection, call_uid: str, verdict: str, when: str) -> None:
    """Вердикт человека по заведённой заявке: «верная» или «лишняя».

    Заявку в CRM это не трогает: закрывать её должен человек в самой CRM, где
    видно контекст. Здесь копится счёт — по нему на странице качества видно
    точность на живом потоке, а не только на проверочном наборе.
    """
    conn.execute(
        "UPDATE screens SET verdict = ?, verdict_at = ? WHERE call_uid = ?",
        (verdict if verdict in {"ok", "wrong"} else "", when if verdict else None, call_uid),
    )


def dismiss_inbound(conn: sqlite3.Connection, call_uid: str, when: str, back: bool = False) -> None:
    """Пометить звонок как «запроса не было» или вернуть его в список."""
    conn.execute(
        "UPDATE inbound_checks SET dismissed = ?, dismissed_at = ? WHERE call_uid = ?",
        (0 if back else 1, None if back else when, call_uid),
    )


def save_screen(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO screens (call_uid, head_text, verdict_json, is_request, created_at)
        VALUES (:call_uid, :head_text, :verdict_json, :is_request, :created_at)
        ON CONFLICT (call_uid) DO UPDATE SET
            head_text    = excluded.head_text,
            verdict_json = excluded.verdict_json,
            is_request   = excluded.is_request,
            created_at   = excluded.created_at
        """,
        row,
    )


def save_order_report(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO order_reports (
            order_id, contact_id, calls_count, verdict_json, created_at,
            kind, stage_now, next_step, next_step_due, needs_rop, rop_reason
        ) VALUES (
            :order_id, :contact_id, :calls_count, :verdict_json, :created_at,
            :kind, :stage_now, :next_step, :next_step_due, :needs_rop, :rop_reason
        )
        ON CONFLICT (order_id) DO UPDATE SET
            contact_id    = excluded.contact_id,
            calls_count   = excluded.calls_count,
            verdict_json  = excluded.verdict_json,
            created_at    = excluded.created_at,
            kind          = excluded.kind,
            stage_now     = excluded.stage_now,
            next_step     = excluded.next_step,
            next_step_due = excluded.next_step_due,
            needs_rop     = excluded.needs_rop,
            rop_reason    = excluded.rop_reason
        """,
        {"kind": "", "stage_now": None, "next_step": None, "next_step_due": None,
         "needs_rop": 0, "rop_reason": None, **row},
    )


def save_order_task(conn: sqlite3.Connection, order_id: str, *,
                    task_id: str | None, task_at: str, note: str = "") -> None:
    """Отметить, что по заявке поставлена задача менеджеру.

    Пишем след даже в сухом прогоне (`task_id` пустой): иначе при первом же
    боевом запуске скрипт заведёт задачи по всем заявкам разом.
    """
    conn.execute(
        "UPDATE order_reports SET task_id = ?, task_at = ?, task_note = ? WHERE order_id = ?",
        (task_id, task_at, note, order_id),
    )


def has_any_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM calls LIMIT 1").fetchone() is not None
