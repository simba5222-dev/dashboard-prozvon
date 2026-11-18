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

-- Заметки РОПа по заявке на странице /order/{id} — свои, не в Synergy:
-- комментарии через POST /comments там пишутся, но нигде не видны в
-- веб-интерфейсе заявки (проверено вживую 15.11.2026). Лог, не одна строка
-- на заявку — РОП дописывает по ходу работы, историю терять нельзя.
CREATE TABLE IF NOT EXISTS order_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT '',
    comment     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_comments_order ON order_comments (order_id);

-- Эталонный набор для дообучения распознавания (16.11.2026): звонки, где человек
-- исправил текст по звуку. Черновик — с метками времени (сегменты), чтобы потом
-- нарезать звонок на куски до 30 секунд. Цель владельца — 50 часов, по 30 минут в
-- день. tags — метки через запятую: акцент, шум, роли_перепутаны, личный_разговор.
CREATE TABLE IF NOT EXISTS training_items (
    call_uid        TEXT PRIMARY KEY,
    batch           INTEGER NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT '',
    direction       TEXT NOT NULL DEFAULT '',
    duration_sec    INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL DEFAULT '',
    manager         TEXT NOT NULL DEFAULT '',
    draft_json      TEXT NOT NULL DEFAULT '[]',
    corrected_text  TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'new',
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_training_batch ON training_items (batch, position);

-- Поиск техники менеджерами продаж. Связь звонка с заявкой — родное поле
-- звонка в Synergy custom-11360 «Заявки»; через API списка связей нет, поэтому
-- копия лежит у нас. source: helper — проставил помощник вручную (эталон для
-- сверки), ai — предложили/проставили мы.
CREATE TABLE IF NOT EXISTS call_order_links (
    call_uid         TEXT PRIMARY KEY,
    synergy_call_id  TEXT,
    order_id         TEXT NOT NULL,
    source           TEXT NOT NULL,
    confidence       TEXT NOT NULL DEFAULT '',
    note             TEXT NOT NULL DEFAULT '',
    written_at       TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_order_links_order ON call_order_links (order_id);

-- Индекс заявок для привязки звонков: ответственный, тип техники, создание.
-- Пополняется инкрементально, чтобы не листать заявки Synergy при каждом разборе.
CREATE TABLE IF NOT EXISTS order_index (
    order_id        TEXT PRIMARY KEY,
    number          TEXT,
    responsible_id  TEXT,
    equipment       TEXT NOT NULL DEFAULT '',
    stage_kind      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_index_resp ON order_index (responsible_id, created_at);

-- Что ИИ предложил или добавил в базу «Транспорт» по разговорам с исполнителями.
-- Уникальность (phone10, type_name): один и тот же тип у одного номера не
-- предлагаем и не создаём дважды.
CREATE TABLE IF NOT EXISTS ai_transport_adds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    call_uid      TEXT NOT NULL,
    phone10       TEXT NOT NULL,
    type_name     TEXT NOT NULL,
    type_id       TEXT,
    quote         TEXT NOT NULL DEFAULT '',
    sure          INTEGER,
    mode          TEXT NOT NULL,
    transport_id  TEXT,
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (phone10, type_name)
);

-- Звонки по объявлениям Авито (CallTracking API, с 17.11.2026). Один звонок — одна строка, ключ — callId Авито.
-- seller_phone — наша линия (9213909268 = «Авито СПБ»), её же ВАТС называет diversion; buyer_phone — клиент;
-- item_id — объявление. По (линия, клиент, время) звонок сопоставляется со звонком в ВАТС.
CREATE TABLE IF NOT EXISTS avito_calls (
    call_id       INTEGER PRIMARY KEY,
    call_time     TEXT NOT NULL,
    local_date    TEXT NOT NULL,
    buyer_phone   TEXT NOT NULL DEFAULT '',
    seller_phone  TEXT NOT NULL DEFAULT '',
    virtual_phone TEXT NOT NULL DEFAULT '',
    item_id       INTEGER,
    talk_sec      INTEGER NOT NULL DEFAULT 0,
    wait_sec      REAL NOT NULL DEFAULT 0,
    fetched_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_avito_calls_date ON avito_calls(local_date);
CREATE INDEX IF NOT EXISTS ix_avito_calls_item ON avito_calls(item_id);

-- Чаты Авито по объявлениям (Messenger API, с 17.11.2026). Только чтение. Клиент в чате известен по имени и id Авито;
-- телефона Авито не отдаёт, но клиенты часто пишут его в тексте — по нему чат связывается с заявкой CRM.
-- msgs_fetched_updated — значение `updated` чата, при котором мы последний раз читали сообщения (так видно, что чат изменился).
CREATE TABLE IF NOT EXISTS avito_chats (
    chat_id              TEXT PRIMARY KEY,
    item_id              INTEGER,
    item_title           TEXT NOT NULL DEFAULT '',
    item_url             TEXT NOT NULL DEFAULT '',
    client_id            TEXT NOT NULL DEFAULT '',
    client_name          TEXT NOT NULL DEFAULT '',
    created              INTEGER NOT NULL DEFAULT 0,
    updated              INTEGER NOT NULL DEFAULT 0,
    last_direction       TEXT NOT NULL DEFAULT '',
    last_type            TEXT NOT NULL DEFAULT '',
    msgs_fetched_updated INTEGER NOT NULL DEFAULT 0,
    fetched_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_avito_chats_item ON avito_chats(item_id);
CREATE INDEX IF NOT EXISTS ix_avito_chats_created ON avito_chats(created);
CREATE TABLE IF NOT EXISTS avito_messages (
    message_id TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    created    INTEGER NOT NULL DEFAULT 0,
    direction  TEXT NOT NULL DEFAULT '',
    author_id  TEXT NOT NULL DEFAULT '',
    type       TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    is_read    INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_avito_messages_chat ON avito_messages(chat_id, created);

-- Вовлечённость менеджера после передачи заявки + совет эксперта по продажам
-- (custom-30756/custom-30757 в Synergy). Отдельно от order_reports: эта
-- оценка живёт на любой заявке с 19.10.2026, в любом статусе, а не только
-- на тех, что видит pick_orders() в analyze_orders.py. order_updated_snapshot
-- хранит updated-at заявки в Synergy на момент оценки — по нему решаем,
-- менялась ли заявка с прошлого прогона без нового звонка.
-- stage_kind_snapshot — этап заявки на момент этой оценки. Как только он
-- становится lost/won, заявка выходит из рабочего набора «пересматривать
-- каждый цикл» — тот же приём, что уже есть в analyze_orders.py для
-- проваленных заявок (пересчитывать закрытую сделку бессмысленно и платно).
CREATE TABLE IF NOT EXISTS engagement_reports (
    order_id               TEXT PRIMARY KEY,
    contact_id             TEXT,
    engagement_score       INTEGER,
    engagement_note        TEXT,
    advice                 TEXT,
    calls_count            INTEGER NOT NULL DEFAULT 0,
    last_call_at           TEXT,
    order_updated_snapshot TEXT,
    stage_kind_snapshot    TEXT NOT NULL DEFAULT '',
    evaluated_at           TEXT NOT NULL
);

-- Мелкое состояние между прогонами разных скриптов (например, до какого
-- момента уже пройдена развёртка заявок по -created-at/-updated-at) —
-- чтобы не пересматривать всю историю заново на каждом тике таймера.
CREATE TABLE IF NOT EXISTS kv_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ABC-анализ клиентской базы (custom-30764/30765/30766 на контакте).
-- Считается по всем выигранным сделкам контакта за всё время — деньги
-- (custom-14924 заявки, запасной путь custom-257) и частота. contact_id
-- здесь — настоящий id контакта Synergy; клиентов без привязки к контакту
-- (см. журнал/дерево про «сиротскую» треть выручки) сюда не кладём вовсе,
-- писать им некуда. Пересчёт — раз в сутки, полный, не инкрементальный:
-- объём (≈6 тыс. выигранных сделок) это позволяет. applied=0 означает
-- «посчитано, но в CRM ещё не записалось» — отдельно от того, изменилась
-- ли категория с прошлого раза (see активный набор analyze_abc.py).
CREATE TABLE IF NOT EXISTS abc_reports (
    contact_id    TEXT PRIMARY KEY,
    revenue       REAL NOT NULL DEFAULT 0,
    deals_count   INTEGER NOT NULL DEFAULT 0,
    last_deal_at  TEXT,
    category      TEXT NOT NULL,
    frequency     TEXT NOT NULL,
    risk          TEXT NOT NULL,
    applied       INTEGER NOT NULL DEFAULT 0,
    evaluated_at  TEXT NOT NULL
);

-- Пробный просев: тот же разговор, прогнанный другой головой или по новым
-- правилам. Боевой ответ в `screens` при этом не трогается: по нему работает
-- ловля заявок, и затереть его — значит остаться без точки отсчёта.
CREATE TABLE IF NOT EXISTS screen_trials (
    call_uid      TEXT PRIMARY KEY REFERENCES calls (uid),
    engine        TEXT NOT NULL DEFAULT '',
    head_text     TEXT NOT NULL DEFAULT '',
    verdict_json  TEXT NOT NULL DEFAULT '',
    made_at       TEXT NOT NULL,
    verdict       TEXT NOT NULL DEFAULT '',
    verdict_note  TEXT,
    verdict_at    TEXT
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

-- Отправки файлов (презентация, список техники) через личные мессенджеры
-- менеджеров — Telegram/MAX. Не наш звонковый конвейер: данные приходят
-- разовым разбором переписки (владелец читает аккаунты через VNC, агент —
-- через debug-порт браузера), не live-потоком.
--
-- Без UNIQUE по получателю: имена в мессенджерах не уникальны (несколько
-- разных «Сергеев» в один день — обычное дело), а отчёту важно именно
-- число отправок, не число разных имён. Повторный импорт того же разбора
-- не защищён уникальным ключом намеренно — вызывающий код (скрипт импорта)
-- сам отвечает за то, чтобы не вставить один и тот же разбор дважды
-- (стандартный приём — удалить старые строки по manager_login/platform
-- перед повторной вставкой).
CREATE TABLE IF NOT EXISTS messenger_file_sends (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    manager_login  TEXT NOT NULL,
    platform       TEXT NOT NULL,              -- 'telegram' / 'max'
    recipient      TEXT NOT NULL,               -- имя получателя, как в мессенджере (не уникально)
    sent_date      TEXT NOT NULL,               -- YYYY-MM-DD
    file_name      TEXT NOT NULL,               -- 'Техника.xlsx', 'СК ТЕХНОРЕСУРС Аренда Техники.pdf' и т.п.
    collected_at   TEXT NOT NULL                -- когда разобрали переписку
);
CREATE INDEX IF NOT EXISTS idx_messenger_sends_day
    ON messenger_file_sends (manager_login, platform, sent_date);

-- Письма на info@ (аренда техники, нерудные материалы), разобранные
-- автоматом. uid — IMAP UID письма в INBOX этого ящика: числа растут
-- монотонно, поэтому MAX(uid) и есть точка, откуда продолжать при
-- следующем проходе — без отдельного файла состояния.
CREATE TABLE IF NOT EXISTS mail_leads (
    uid              INTEGER PRIMARY KEY,
    message_id       TEXT,
    received_at      TEXT,
    from_addr        TEXT,
    subject          TEXT,
    is_request       INTEGER NOT NULL DEFAULT 0,   -- решение модели: заявка или нет
    phone            TEXT,
    contact_id       TEXT,
    contact_created  INTEGER NOT NULL DEFAULT 0,
    order_id         TEXT,
    attachments_count INTEGER NOT NULL DEFAULT 0,
    note             TEXT,                          -- почему пропустили / что решили
    processed_at     TEXT NOT NULL
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
    # Чем именно просев ошибся. Пояснение владельца — то, из чего растут
    # правила: «фронтальник = фронтальный погрузчик», «не придумывать адрес».
    ("screens", "verdict_note", "TEXT"),
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
    # Владелец подтвердил, что это общий номер компании. Нужно для линий,
    # которым ВАТС имени не дала: по её данным они «неизвестные», а на деле
    # на них приходят заявки. Ночная синхронизация справочника переписывает
    # `kind` и `name` из ВАТС, а эти два поля не трогает — слово владельца
    # сильнее догадки по имени.
    ("lines", "is_general", "INTEGER NOT NULL DEFAULT 0"),
    ("lines", "owner_name", "TEXT"),
    # Выжимка по разговору поисковика — решение владельца 05.10.2026: видеть
    # не только что занесено в карточку, но и как прошёл сам звонок. Цитата —
    # то же правило, что у `more_unnamed`: пусто значит «не было», не «не
    # смотрели».
    ("search_checks", "presented", "TEXT"),
    ("search_checks", "clarified", "TEXT"),
    # Когда задачу на самом деле тронули — решение владельца 05.10.2026.
    # Задачи привязываются к звонку по `updated_at`, а не по `created_at`
    # (см. `load_tasks_index`): команда прозвона не заводит новую задачу на
    # каждый звонок, а дополняет одну старую на контакте. Без этого поля
    # здесь виден только момент создания — не тот, что решил привязку.
    ("call_tasks", "updated_at", "TEXT"),
    # «Дата освобождения» (custom-26952) карточки транспорта -- когда
    # поставщик рассчитывает освободиться. Решение владельца 21.10.2026:
    # подключить эту базу к «Подбору техники» как второй источник, рядом
    # с телеграм-группами -- у кого эта дата в пределах ±3 дней от сегодня.
    ("transport_cards", "release_date", "TEXT"),
    # Для ежедневного отчёта «кто сколько добавил техники» (03.11.2026,
    # группа «Транспорт СРМ»). В Synergy `responsible` и `user` у карточки
    # транспорта всегда совпадают (проверено на полусотне карточек) — берём
    # responsible, это то же поле, что подписано «Ответственный» в самой CRM.
    ("transport_cards", "created_at", "TEXT"),
    ("transport_cards", "responsible_id", "TEXT"),
    ("transport_cards", "responsible_name", "TEXT"),
    # Отдельно от «считали ли балл»: сухой прогон считает и сохраняет
    # локально, но ничего не пишет в CRM. Без этого признака повторный прогон
    # с --apply решил бы «ничего не поменялось» и так и не записал бы в
    # Synergy то, что уже посчитано локально — на этом поймал себя сам
    # 27.10.2026, когда сухим прогоном после сброса водяного знака насчитал
    # сотню заявок, а записать их забыл.
    ("engagement_reports", "applied", "INTEGER NOT NULL DEFAULT 0"),
    # Поиск техники менеджерами продаж (15.11.2026): цена и дата освобождения,
    # которые назвал исполнитель, и заявка, под которую звонили. Пустое значение у
    # старых разборов — «не считали», а не «не было».
    ("search_checks", "prices_json", "TEXT"),
    ("search_checks", "busy_json", "TEXT"),
    ("search_checks", "order_id", "TEXT"),
    ("search_checks", "order_equipment", "TEXT"),
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
    row.setdefault("release_date", "")
    row.setdefault("created_at", "")
    row.setdefault("responsible_id", "")
    row.setdefault("responsible_name", "")
    conn.execute(
        """
        INSERT INTO transport_cards (id, name, type_id, type_name, phone10,
                                     contact_id, contact_name, status,
                                     updated_at, synced_at, call_link, release_date,
                                     created_at, responsible_id, responsible_name)
        VALUES (:id, :name, :type_id, :type_name, :phone10, :contact_id,
                :contact_name, :status, :updated_at, :synced_at, :call_link,
                :release_date, :created_at, :responsible_id, :responsible_name)
        ON CONFLICT (id) DO UPDATE SET
            name = excluded.name, type_id = excluded.type_id,
            type_name = excluded.type_name, phone10 = excluded.phone10,
            contact_id = excluded.contact_id, contact_name = excluded.contact_name,
            status = excluded.status, updated_at = excluded.updated_at,
            synced_at = excluded.synced_at, call_link = excluded.call_link,
            release_date = COALESCE(NULLIF(excluded.release_date, ''), release_date),
            created_at = COALESCE(NULLIF(excluded.created_at, ''), created_at),
            responsible_id = COALESCE(NULLIF(excluded.responsible_id, ''), responsible_id),
            responsible_name = COALESCE(NULLIF(excluded.responsible_name, ''), responsible_name)
        """,
        row,
    )


def save_search_check(conn: sqlite3.Connection, **row: Any) -> None:
    """Вердикт сверки разговора с карточками. Повтор разбора переписывает.

    Цена и занятость (`prices_json`, `busy_json`) приходят только из самого
    разбора разговора; пересверка по карточкам (`--recheck`) их не передаёт и
    затирать не должна — поэтому при пустом значении остаётся прежнее.
    """
    row.setdefault("prices_json", None)
    row.setdefault("busy_json", None)
    conn.execute(
        """
        INSERT INTO search_checks (call_uid, local_date, vats_login, phone10,
                                   contact_name, asked_type, offered, known,
                                   missing, verdict, checked_at, is_demo,
                                   more_unnamed, presented, clarified,
                                   prices_json, busy_json)
        VALUES (:call_uid, :local_date, :vats_login, :phone10, :contact_name,
                :asked_type, :offered, :known, :missing, :verdict, :checked_at,
                :is_demo, :more_unnamed, :presented, :clarified,
                :prices_json, :busy_json)
        ON CONFLICT (call_uid) DO UPDATE SET
            phone10 = excluded.phone10, contact_name = excluded.contact_name,
            asked_type = excluded.asked_type, offered = excluded.offered,
            known = excluded.known, missing = excluded.missing,
            verdict = excluded.verdict, checked_at = excluded.checked_at,
            more_unnamed = excluded.more_unnamed,
            presented = excluded.presented, clarified = excluded.clarified,
            prices_json = COALESCE(excluded.prices_json, search_checks.prices_json),
            busy_json = COALESCE(excluded.busy_json, search_checks.busy_json)
        """,
        row,
    )


def set_search_check_order(conn: sqlite3.Connection, call_uid: str,
                           order_id: str | None, equipment: str) -> None:
    """Заявка, под которую звонили. Отдельно от вердикта: считается после него."""
    conn.execute(
        "UPDATE search_checks SET order_id = ?, order_equipment = ? WHERE call_uid = ?",
        (order_id, equipment, call_uid),
    )


def save_order_index(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO order_index (order_id, number, responsible_id, equipment,
                                 stage_kind, created_at, updated_at, fetched_at)
        VALUES (:order_id, :number, :responsible_id, :equipment, :stage_kind,
                :created_at, :updated_at, :fetched_at)
        ON CONFLICT (order_id) DO UPDATE SET
            number = excluded.number, responsible_id = excluded.responsible_id,
            equipment = excluded.equipment, stage_kind = excluded.stage_kind,
            updated_at = excluded.updated_at, fetched_at = excluded.fetched_at
        """,
        row,
    )


def save_call_order_link(conn: sqlite3.Connection, call_uid: str, order_id: str,
                         source: str, *, synergy_call_id: str | None = None,
                         confidence: str = "", note: str = "",
                         written_at: str | None = None, at: str) -> None:
    """Связь звонка с заявкой. Связь помощника (helper) ИИ не перезаписывает."""
    conn.execute(
        """
        INSERT INTO call_order_links (call_uid, synergy_call_id, order_id, source,
                                      confidence, note, written_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (call_uid) DO UPDATE SET
            synergy_call_id = COALESCE(excluded.synergy_call_id, call_order_links.synergy_call_id),
            order_id = CASE WHEN call_order_links.source = 'helper' AND excluded.source <> 'helper'
                            THEN call_order_links.order_id ELSE excluded.order_id END,
            source = CASE WHEN call_order_links.source = 'helper' AND excluded.source <> 'helper'
                          THEN call_order_links.source ELSE excluded.source END,
            confidence = excluded.confidence, note = excluded.note,
            written_at = COALESCE(excluded.written_at, call_order_links.written_at)
        """,
        (call_uid, synergy_call_id, order_id, source, confidence, note, written_at, at),
    )


def save_avito_call(conn: sqlite3.Connection, *, call_id: int, call_time: str, local_date: str, buyer_phone: str,
                    seller_phone: str, virtual_phone: str, item_id: int | None, talk_sec: int,
                    wait_sec: float, fetched_at: str) -> None:
    """Звонок Авито. Повторная загрузка того же callId обновляет строку (идемпотентно)."""
    conn.execute(
        """INSERT OR REPLACE INTO avito_calls
           (call_id, call_time, local_date, buyer_phone, seller_phone, virtual_phone, item_id, talk_sec, wait_sec, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (call_id, call_time, local_date, buyer_phone, seller_phone, virtual_phone, item_id, talk_sec, wait_sec, fetched_at))


def save_avito_chat(conn: sqlite3.Connection, *, chat_id: str, item_id: int | None, item_title: str, item_url: str,
                    client_id: str, client_name: str, created: int, updated: int, last_direction: str,
                    last_type: str, fetched_at: str) -> None:
    """Чат Авито. Поле msgs_fetched_updated не трогаем: его ставит только чтение сообщений."""
    conn.execute(
        """INSERT INTO avito_chats (chat_id, item_id, item_title, item_url, client_id, client_name, created, updated,
                                    last_direction, last_type, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET item_id=excluded.item_id, item_title=excluded.item_title, item_url=excluded.item_url,
               client_id=excluded.client_id, client_name=excluded.client_name, created=excluded.created, updated=excluded.updated,
               last_direction=excluded.last_direction, last_type=excluded.last_type, fetched_at=excluded.fetched_at""",
        (chat_id, item_id, item_title, item_url, client_id, client_name, created, updated, last_direction, last_type, fetched_at))


def save_avito_message(conn: sqlite3.Connection, *, message_id: str, chat_id: str, created: int, direction: str,
                       author_id: str, type_: str, text: str, is_read: bool, fetched_at: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO avito_messages (message_id, chat_id, created, direction, author_id, type, text, is_read, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (message_id, chat_id, created, direction, author_id, type_, text, int(is_read), fetched_at))


def save_ai_transport_add(conn: sqlite3.Connection, *, call_uid: str, phone10: str,
                          type_name: str, type_id: str | None, quote: str,
                          sure: int | None, mode: str, at: str) -> bool:
    """Предложение/создание карточки. False — такая пара номер+тип уже есть."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO ai_transport_adds
            (call_uid, phone10, type_name, type_id, quote, sure, mode, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (call_uid, phone10, type_name, type_id, quote, sure, mode, at),
    )
    return cur.rowcount > 0


def mark_ai_transport_created(conn: sqlite3.Connection, add_id: int,
                              transport_id: str | None, error: str = "") -> None:
    conn.execute(
        "UPDATE ai_transport_adds SET mode = ?, transport_id = ?, error = ? WHERE id = ?",
        ("created" if transport_id else "failed", transport_id, error, add_id),
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


def save_training_item(conn: sqlite3.Connection, **row: Any) -> None:
    """Звонок в эталонном наборе. Повторная загрузка черновика исправленное не трогает."""
    conn.execute(
        """
        INSERT INTO training_items (call_uid, batch, position, source, direction,
                                    duration_sec, started_at, manager, draft_json)
        VALUES (:call_uid, :batch, :position, :source, :direction, :duration_sec,
                :started_at, :manager, :draft_json)
        ON CONFLICT (call_uid) DO UPDATE SET
            draft_json = excluded.draft_json, position = excluded.position
        """,
        row,
    )


def save_training_correction(conn: sqlite3.Connection, call_uid: str, corrected: str,
                             tags: str, status: str, at: str) -> None:
    conn.execute(
        "UPDATE training_items SET corrected_text = ?, tags = ?, status = ?, updated_at = ? "
        "WHERE call_uid = ?",
        (corrected, tags, status, at, call_uid),
    )


def training_rows(conn: sqlite3.Connection, batch: int | None = None) -> list[sqlite3.Row]:
    if batch is None:
        return conn.execute(
            "SELECT * FROM training_items ORDER BY batch DESC, position").fetchall()
    return conn.execute(
        "SELECT * FROM training_items WHERE batch = ? ORDER BY position", (batch,)).fetchall()


def save_order_comment(conn: sqlite3.Connection, order_id: str, author: str,
                       comment: str, at: str) -> None:
    """Заметка РОПа по заявке. Лог, поэтому INSERT, не апсерт."""
    conn.execute(
        "INSERT INTO order_comments (order_id, author, comment, created_at) VALUES (?, ?, ?, ?)",
        (order_id, author, comment, at),
    )


def order_comments(conn: sqlite3.Connection, order_id: str) -> list[sqlite3.Row]:
    """Заметки РОПа по заявке, от старых к новым."""
    return conn.execute(
        "SELECT * FROM order_comments WHERE order_id = ? ORDER BY created_at",
        (order_id,),
    ).fetchall()


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
        # `in_scope`, `scope_note`, `is_general` и `owner_name` намеренно не в
        # списке обновляемых полей: это решения владельца, ночная
        # синхронизация их не трогает.
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
    row.setdefault("updated_at", row.get("created_at"))
    conn.execute(
        """
        INSERT INTO call_tasks (task_id, call_uid, name, created_at, updated_at,
                                due_date, status, responsible, completed_at, is_demo)
        VALUES (:task_id, :call_uid, :name, :created_at, :updated_at,
                :due_date, :status, :responsible, :completed_at, :is_demo)
        ON CONFLICT (task_id, call_uid) DO UPDATE SET
            name         = excluded.name,
            updated_at   = excluded.updated_at,
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


def save_trial(conn: sqlite3.Connection, **row: Any) -> None:
    """Пробный просев: тот же разговор, другая голова или другие правила.

    Отдельно от `screens`, потому что боевой ответ затирать нельзя — по нему
    работает ловля заявок, и сравнивать будет не с чем.
    """
    conn.execute(
        """
        INSERT INTO screen_trials (call_uid, engine, head_text, verdict_json,
                                   made_at, verdict, verdict_note, verdict_at)
        VALUES (:call_uid, :engine, :head_text, :verdict_json, :made_at, '', NULL, NULL)
        ON CONFLICT (call_uid) DO UPDATE SET
            engine       = excluded.engine,
            head_text    = excluded.head_text,
            verdict_json = excluded.verdict_json,
            made_at      = excluded.made_at
        """,
        row,
    )


def save_trial_verdict(conn: sqlite3.Connection, call_uid: str, verdict: str,
                       note: str, at: str) -> None:
    conn.execute(
        "UPDATE screen_trials SET verdict = ?, verdict_note = ?, verdict_at = ? "
        "WHERE call_uid = ?",
        (verdict, note, at, call_uid),
    )


def save_screen_verdict(conn: sqlite3.Connection, call_uid: str, verdict: str,
                        note: str, at: str) -> None:
    """Отметка человека по просеву: верно, неверно, спорно — и чем именно.

    Пояснение важнее галочки: из галочки видно, что ошибка есть, а из
    пояснения — какая. Из этих пояснений растут правила, которым мы учим
    модель. Заявку в CRM отметка не трогает.
    """
    conn.execute(
        "UPDATE screens SET verdict = ?, verdict_note = ?, verdict_at = ? WHERE call_uid = ?",
        (verdict, note, at, call_uid),
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


def save_engagement_report(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO engagement_reports (
            order_id, contact_id, engagement_score, engagement_note, advice,
            calls_count, last_call_at, order_updated_snapshot, stage_kind_snapshot,
            applied, evaluated_at
        ) VALUES (
            :order_id, :contact_id, :engagement_score, :engagement_note, :advice,
            :calls_count, :last_call_at, :order_updated_snapshot, :stage_kind_snapshot,
            :applied, :evaluated_at
        )
        ON CONFLICT (order_id) DO UPDATE SET
            contact_id             = excluded.contact_id,
            engagement_score       = excluded.engagement_score,
            engagement_note        = excluded.engagement_note,
            advice                 = excluded.advice,
            calls_count             = excluded.calls_count,
            last_call_at            = excluded.last_call_at,
            order_updated_snapshot  = excluded.order_updated_snapshot,
            stage_kind_snapshot     = excluded.stage_kind_snapshot,
            applied                 = excluded.applied,
            evaluated_at            = excluded.evaluated_at
        """,
        {"contact_id": None, "engagement_note": None, "last_call_at": None,
         "order_updated_snapshot": None, "stage_kind_snapshot": "", "applied": 0, **row},
    )


def get_engagement_report(conn: sqlite3.Connection, order_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM engagement_reports WHERE order_id = ?", (order_id,)
    ).fetchone()
    return dict(row) if row else None


def save_abc_report(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT INTO abc_reports (
            contact_id, revenue, deals_count, last_deal_at,
            category, frequency, risk, applied, evaluated_at
        ) VALUES (
            :contact_id, :revenue, :deals_count, :last_deal_at,
            :category, :frequency, :risk, :applied, :evaluated_at
        )
        ON CONFLICT (contact_id) DO UPDATE SET
            revenue      = excluded.revenue,
            deals_count  = excluded.deals_count,
            last_deal_at = excluded.last_deal_at,
            category     = excluded.category,
            frequency    = excluded.frequency,
            risk         = excluded.risk,
            applied      = excluded.applied,
            evaluated_at = excluded.evaluated_at
        """,
        {"last_deal_at": None, "applied": 0, **row},
    )


def get_abc_report(conn: sqlite3.Connection, contact_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM abc_reports WHERE contact_id = ?", (contact_id,)
    ).fetchone()
    return dict(row) if row else None


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv_state (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
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


def save_messenger_send(conn: sqlite3.Connection, **row: Any) -> None:
    """Одна отправка файла через личный мессенджер менеджера (Telegram/MAX).

    Без защиты от дублей на уровне БД — получатели не уникальны по имени
    (см. комментарий у таблицы), так что дедуплицировать тут нечем. Вызывающий
    код сам чистит старые строки этого менеджера/платформы перед повторной
    вставкой, если разбор переписки пересчитывают заново.
    """
    conn.execute(
        """
        INSERT INTO messenger_file_sends
            (manager_login, platform, recipient, sent_date, file_name, collected_at)
        VALUES (:manager_login, :platform, :recipient, :sent_date, :file_name, :collected_at)
        """,
        row,
    )


def last_mail_uid(conn: sqlite3.Connection) -> int:
    """Последний разобранный UID письма info@ — 0, если ещё не начинали."""
    row = conn.execute("SELECT MAX(uid) AS m FROM mail_leads").fetchone()
    return int(row["m"]) if row and row["m"] is not None else 0


def save_mail_lead(conn: sqlite3.Connection, **row: Any) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO mail_leads
            (uid, message_id, received_at, from_addr, subject, is_request,
             phone, contact_id, contact_created, order_id, attachments_count,
             note, processed_at)
        VALUES (:uid, :message_id, :received_at, :from_addr, :subject, :is_request,
                :phone, :contact_id, :contact_created, :order_id, :attachments_count,
                :note, :processed_at)
        """,
        row,
    )


def clear_messenger_sends(conn: sqlite3.Connection, *, manager_login: str, platform: str) -> None:
    """Стереть прежний разбор переписки перед повторным импортом тех же данных."""
    conn.execute(
        "DELETE FROM messenger_file_sends WHERE manager_login = ? AND platform = ?",
        (manager_login, platform),
    )
