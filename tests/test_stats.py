"""Проверки подсчётов дашборда.

Считать звонки просто ровно до тех пор, пока не появляются часовые пояса,
повторный опрос источника и деление на ноль в пустой день. Здесь проверяется
именно это.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import CARD_FIELDS, init_schema, save_call, save_card_check, upsert_manager
from app.stats import day_summary, local_parts, period_summary

THRESHOLD = 15
PLAN = 120


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    upsert_manager(
        c, vats_login="andy", display_name="Андрей", synergy_user="andy@crm",
        plan_calls=None, active=1, is_demo=0,
    )
    return c


def add_call(conn, uid, *, hour=10, duration=60, date="2026-09-16", login="andy"):
    return save_call(
        conn, uid=uid, vats_login=login, client_phone="79101234567",
        direction="out", status="success", started_at=f"{date}T{hour:02d}:00:00Z",
        local_date=date, local_hour=hour, wait_sec=3, duration_sec=duration,
        record_url=None, is_demo=0, fetched_at="2026-09-16T10:00:00Z",
    )


# ------------------------------------------------------------- часовые пояса

def test_utc_evening_becomes_next_day_locally():
    """22:30 UTC — это уже следующий день по Москве, и день не должен съехать."""
    day, hour = local_parts("2026-09-16T22:30:00Z", 3)
    assert day == "2026-09-17"
    assert hour == 1


def test_time_without_zone_is_treated_as_utc():
    day, hour = local_parts("2026-09-16T08:00:00", 3)
    assert (day, hour) == ("2026-09-16", 11)


# ------------------------------------------------------------- дедупликация

def test_repeated_poll_does_not_duplicate_calls(conn):
    """Опрос ВАТС повторяется каждые несколько минут и приносит те же звонки."""
    assert add_call(conn, "uid-1") is True
    assert add_call(conn, "uid-1") is False
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1


# ------------------------------------------------------------- подсчёты

def test_short_calls_do_not_count_as_conversations(conn):
    add_call(conn, "a", duration=14)   # гудки
    add_call(conn, "b", duration=15)   # ровно порог — уже разговор
    add_call(conn, "c", duration=200)
    conn.commit()
    row = day_summary(conn, "2026-09-16", default_plan=PLAN, threshold_sec=THRESHOLD)[0]
    assert row.total == 3
    assert row.talked == 2
    assert row.short == 1
    assert row.talk_seconds == 215


def test_empty_day_does_not_divide_by_zero(conn):
    row = day_summary(conn, "2026-09-16", default_plan=PLAN, threshold_sec=THRESHOLD)[0]
    assert row.total == 0
    assert row.plan_percent == 0
    assert row.avg_talk_sec == 0
    assert row.discipline_percent == 0


def test_plan_percent_and_hours(conn):
    for i in range(60):
        add_call(conn, f"h-{i}", hour=9 if i < 40 else 14)
    conn.commit()
    row = day_summary(conn, "2026-09-16", default_plan=PLAN, threshold_sec=THRESHOLD)[0]
    assert row.plan_percent == 50
    assert row.by_hour == {9: 40, 14: 20}


def test_personal_plan_overrides_default(conn):
    upsert_manager(
        conn, vats_login="andy", display_name="Андрей", synergy_user=None,
        plan_calls=60, active=1, is_demo=0,
    )
    for i in range(30):
        add_call(conn, f"p-{i}")
    conn.commit()
    row = day_summary(conn, "2026-09-16", default_plan=PLAN, threshold_sec=THRESHOLD)[0]
    assert row.plan == 60
    assert row.plan_percent == 50


# ------------------------------------------------------------- дисциплина CRM

def test_discipline_counts_only_full_cards(conn):
    """Карточка считается заполненной, только если внесены все пять пунктов."""
    add_call(conn, "full", duration=100)
    add_call(conn, "partial", duration=100)
    save_card_check(
        conn, call_uid="full", contact_id="1", contact_found=1,
        need_filled=1, frequency_filled=1, objects_filled=1, inn_filled=1,
        task_created=1, checked_at="2026-09-16T10:00:00Z", is_demo=0,
    )
    save_card_check(
        conn, call_uid="partial", contact_id="2", contact_found=1,
        need_filled=1, frequency_filled=1, objects_filled=1, inn_filled=0,
        task_created=1, checked_at="2026-09-16T10:00:00Z", is_demo=0,
    )
    conn.commit()
    row = day_summary(conn, "2026-09-16", default_plan=PLAN, threshold_sec=THRESHOLD)[0]
    assert row.checked == 2
    assert row.filled["inn_filled"] == 1
    assert row.filled["need_filled"] == 2
    assert row.discipline_percent == 50  # слабое звено — ИНН


def test_short_calls_are_not_checked_for_cards(conn):
    """По гудкам карточку требовать нельзя — разговора не было."""
    add_call(conn, "short", duration=5)
    save_card_check(
        conn, call_uid="short", contact_id="1", contact_found=1,
        need_filled=0, frequency_filled=0, objects_filled=0, inn_filled=0,
        task_created=0, checked_at="2026-09-16T10:00:00Z", is_demo=0,
    )
    conn.commit()
    row = day_summary(conn, "2026-09-16", default_plan=PLAN, threshold_sec=THRESHOLD)[0]
    assert row.checked == 0


def test_all_card_fields_are_known_to_stats(conn):
    add_call(conn, "x", duration=100)
    save_card_check(
        conn, call_uid="x", contact_id="1", contact_found=1,
        need_filled=1, frequency_filled=1, objects_filled=1, inn_filled=1,
        task_created=1, checked_at="2026-09-16T10:00:00Z", is_demo=0,
    )
    conn.commit()
    row = day_summary(conn, "2026-09-16", default_plan=PLAN, threshold_sec=THRESHOLD)[0]
    assert set(row.filled) == {column for column, _ in CARD_FIELDS}


# ------------------------------------------------------------- период

def test_period_returns_requested_number_of_days(conn):
    add_call(conn, "t", date="2026-09-16")
    conn.commit()
    rows = period_summary(
        conn, 7, default_plan=PLAN, threshold_sec=THRESHOLD, today="2026-09-16"
    )
    assert len(rows) == 7
    assert rows[-1]["day"] == "2026-09-16"
    assert rows[-1]["total"] == 1
    assert rows[0]["total"] == 0  # пустые дни тоже должны быть в ряду
