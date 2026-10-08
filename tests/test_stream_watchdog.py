"""Сторож потоков 2/3/4: проверки на лаговом окне, без спама повторами."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.stream_watchdog as wd  # noqa: E402
from app.db import init_schema, save_call, save_number, save_screen, save_transcript, \
    save_search_check, upsert_manager  # noqa: E402

ISO = "%Y-%m-%dT%H:%M:%S"


def ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(ISO)


def local_date_of(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d")


def fresh_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    wd._ensure_state_table(c)
    return c


def add_call(conn, uid, *, minutes_ago, direction, duration=60, in_group=0,
             client_phone="79101234567"):
    save_call(conn, uid=uid, vats_login="andy", client_phone=client_phone,
             direction=direction, status="success", started_at=ts(minutes_ago),
             local_date=local_date_of(minutes_ago), local_hour=10, wait_sec=3, duration_sec=duration,
             record_url=None, in_group=in_group, is_demo=0, fetched_at=ts(minutes_ago))


# ---------- вспышка ошибок -------------------------------------------------

def test_error_spike_detected_at_threshold(monkeypatch):
    conn = fresh_conn()
    log = "\n".join(["blah", "openai.RateLimitError: credit_balance_exhausted"] * 3)
    monkeypatch.setattr(wd, "_recent_dashboard_log", lambda: log)
    msg = wd.check_error_spike(conn)
    assert msg is not None and "credit_balance_exhausted" in msg


def test_error_spike_silent_below_threshold(monkeypatch):
    conn = fresh_conn()
    log = "openai.RateLimitError: credit_balance_exhausted"  # один раз, не вспышка
    monkeypatch.setattr(wd, "_recent_dashboard_log", lambda: log)
    assert wd.check_error_spike(conn) is None


# ---------- поток 2 ---------------------------------------------------------

def test_stream2_alerts_when_calls_but_no_screening():
    conn = fresh_conn()
    add_call(conn, "c1", minutes_ago=40, direction="in", duration=30)
    msg = wd.check_stream2_inbound(conn)
    assert msg is not None and "Поток 2" in msg


def test_stream2_ignores_calls_shorter_than_the_streams_own_threshold():
    """08.10.2026: 3-секундный звонок без записи ВАТС поймался как «просев
    встал», хотя сам поток такие короткие звонки даже не пытается просеивать
    (settings.inbound_min_duration_sec = 10 по решению от 29.09.2026)."""
    conn = fresh_conn()
    add_call(conn, "c1", minutes_ago=40, direction="in", duration=3)
    assert wd.check_stream2_inbound(conn, min_duration_sec=10) is None


def test_parse_started_handles_mixed_timezones():
    """07.10.2026: calls.started_at хранится то в UTC, то в +03:00 (вебхук ВАТС
    против опроса Synergy). Разница в 3 часа должна остаться разницей в 3 часа
    после разбора, а не потеряться из-за сравнения сырых строк."""
    utc = wd._parse_started("2026-10-07T09:44:29.022+00:00")
    msk = wd._parse_started("2026-10-07T09:44:29.022+03:00")
    assert (utc - msk).total_seconds() == 3 * 3600


def test_mixed_timezone_call_does_not_leak_into_wrong_window():
    """Тот самый живой случай 07.10.2026: звонок в 09:44 по МСК (= 06:44 UTC)
    лексикографически попадал в окно «09:37-09:57 UTC», потому что строка
    "09:44...+03:00" по буквам стоит между "09:37" и "09:57". По настоящему
    времени это на три часа раньше — окна не касается вовсе."""
    from datetime import datetime, timezone
    window_since = datetime(2026, 10, 7, 9, 37, 14, tzinfo=timezone.utc)
    window_until = datetime(2026, 10, 7, 9, 57, 14, tzinfo=timezone.utc)
    call_msk = wd._parse_started("2026-10-07T09:44:29.022+03:00")
    assert not (window_since <= call_msk <= window_until)
    # а честная UTC-метка на то же время окна — наоборот, туда попадает
    call_utc = wd._parse_started("2026-10-07T09:44:29.022+00:00")
    assert window_since <= call_utc <= window_until


def test_stream2_quiet_when_screened():
    conn = fresh_conn()
    add_call(conn, "c1", minutes_ago=40, direction="in", duration=30)
    save_screen(conn, call_uid="c1", head_text="т", verdict_json="{}",
               is_request=0, created_at=ts(35))
    assert wd.check_stream2_inbound(conn) is None


def test_stream2_ignores_calls_still_in_lag_window():
    """Звонок 5 минут назад ещё не обязан быть просеян — не паникуем раньше времени."""
    conn = fresh_conn()
    add_call(conn, "c1", minutes_ago=5, direction="in", duration=30)
    assert wd.check_stream2_inbound(conn) is None


def test_stream3_ignores_calls_still_waiting_for_hourly_batch():
    """07.10.2026: с окном в 30-45 минут здесь был ложный алерт — поток 3
    досчитывается раз в час, а не раз в минуту, как поток 2. Звонок 40 минут
    назад ещё законно ждёт своей волны."""
    conn = fresh_conn()
    add_call(conn, "p1", minutes_ago=40, direction="out", duration=60, in_group=1)
    assert wd.check_stream3_prozvon(conn) is None


def test_stream4_ignores_calls_still_waiting_for_hourly_batch():
    conn = fresh_conn()
    save_number(conn, phone10="9101234567", name="Поставщик", contact_id="1",
               category="исполнитель", cards=1, orders=0, types="Экскаватор",
               resolved_at=ts(100))
    add_call(conn, "s1", minutes_ago=40, direction="out", duration=60,
             client_phone="+79101234567")
    assert wd.check_stream4_search(conn) is None


# ---------- поток 3 ----------------------------------------------------------

def test_stream3_alerts_when_transcribed_but_not_analyzed():
    conn = fresh_conn()
    add_call(conn, "p1", minutes_ago=90, direction="out", duration=60, in_group=1)
    save_transcript(conn, call_uid="p1", text="разговор", analysis_json=None,
                    created_at=ts(88), is_demo=0)
    msg = wd.check_stream3_prozvon(conn)
    assert msg is not None and "Поток 3" in msg


def test_stream3_quiet_when_analysis_present():
    conn = fresh_conn()
    add_call(conn, "p1", minutes_ago=90, direction="out", duration=60, in_group=1)
    save_transcript(conn, call_uid="p1", text="разговор", analysis_json='{"ok":1}',
                    created_at=ts(88), is_demo=0)
    assert wd.check_stream3_prozvon(conn) is None


# ---------- поток 4 ----------------------------------------------------------

def test_stream4_alerts_when_supplier_call_not_checked():
    conn = fresh_conn()
    save_number(conn, phone10="9101234567", name="Поставщик", contact_id="1",
               category="исполнитель", cards=1, orders=0, types="Экскаватор",
               resolved_at=ts(100))
    add_call(conn, "s1", minutes_ago=90, direction="out", duration=60,
             client_phone="+79101234567")
    msg = wd.check_stream4_search(conn)
    assert msg is not None and "Поток 4" in msg


def test_stream4_quiet_when_checked():
    conn = fresh_conn()
    save_number(conn, phone10="9101234567", name="Поставщик", contact_id="1",
               category="исполнитель", cards=1, orders=0, types="Экскаватор",
               resolved_at=ts(100))
    add_call(conn, "s1", minutes_ago=90, direction="out", duration=60,
             client_phone="+79101234567")
    save_search_check(conn, call_uid="s1", local_date="2026-10-07", vats_login="andy",
                      phone10="9101234567", contact_name="П", asked_type="Экскаватор",
                      offered="", known=1, missing="", verdict="занесено",
                      checked_at=ts(88), is_demo=0, more_unnamed=0, presented=1,
                      clarified=1)
    assert wd.check_stream4_search(conn) is None


# ---------- антиспам ---------------------------------------------------------

def test_alert_not_repeated_within_two_hours():
    conn = fresh_conn()
    wd._ensure_state_table(conn)
    assert wd._should_alert(conn, "stream2") is True
    wd._mark_alerted(conn, "stream2")
    assert wd._should_alert(conn, "stream2") is False


def test_alert_repeats_after_two_hours():
    conn = fresh_conn()
    wd._ensure_state_table(conn)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO watchdog_state (check_key, last_alert_at) VALUES (?,?)",
                ("stream2", old))
    assert wd._should_alert(conn, "stream2") is True


# ---------- контрольное сообщение раз в сутки ------------------------------

def test_heartbeat_due_at_the_right_hour_if_not_sent_today():
    conn = fresh_conn()
    now = datetime(2026, 10, 8, wd.HEARTBEAT_ЧАС_UTC, 5, tzinfo=timezone.utc)
    assert wd._heartbeat_due(conn, now) is True


def test_heartbeat_not_due_at_other_hours():
    conn = fresh_conn()
    now = datetime(2026, 10, 8, wd.HEARTBEAT_ЧАС_UTC + 1, 5, tzinfo=timezone.utc)
    assert wd._heartbeat_due(conn, now) is False


def test_heartbeat_not_repeated_same_day():
    """08.10.2026: канал молчал с вечера, владелец решил, что сторож умер,
    хотя тот исправно отрабатывал каждые 10 минут. Контрольное сообщение раз
    в сутки — но не на каждом из шести прогонов внутри одного часа."""
    conn = fresh_conn()
    now = datetime(2026, 10, 8, wd.HEARTBEAT_ЧАС_UTC, 5, tzinfo=timezone.utc)
    assert wd._heartbeat_due(conn, now) is True
    wd._mark_alerted(conn, "heartbeat")
    later_same_hour = datetime(2026, 10, 8, wd.HEARTBEAT_ЧАС_UTC, 45, tzinfo=timezone.utc)
    assert wd._heartbeat_due(conn, later_same_hour) is False
    next_day = datetime(2026, 10, 9, wd.HEARTBEAT_ЧАС_UTC, 5, tzinfo=timezone.utc)
    assert wd._heartbeat_due(conn, next_day) is True
