"""Сценарий звонка считается из метаданных, а не угадывается по словам."""

import sqlite3

import pytest

from app import call_context


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE managers (vats_login TEXT, display_name TEXT, dept TEXT, phone TEXT);
        CREATE TABLE numbers (phone10 TEXT, name TEXT, category TEXT, cards INT, orders INT);
        CREATE TABLE calls (uid TEXT, direction TEXT, client_phone TEXT, started_at TEXT);
        CREATE TABLE lines (phone10 TEXT, name TEXT, kind TEXT, calls_in INT, seen_at TEXT);
        """
    )
    c.executemany(
        "INSERT INTO managers VALUES (?,?,?,?)",
        [("ткачевин", "Ткачевин Михаил", "прозвон", "+79214401135"),
         ("никитин", "Никитин Сергей", "поиск", "+79219999999"),
         ("егоров", "Егоров Игорь", "продажи", "")],
    )
    c.executemany(
        "INSERT INTO numbers VALUES (?,?,?,?,?)",
        [("9161112233", "Пётр", "заказчик", 0, 3),
         ("9164445566", "Валера", "исполнитель", 4, 0),
         ("9167778899", "Егор", "оба", 2, 2)],
    )
    c.executemany(
        "INSERT INTO lines VALUES (?,?,?,?,?)",
        [("9214401135", "", "прямой", 20, ""),
         ("8123091309", "Авито СПБ", "рекламная", 58, "")],
    )
    return c


def звонок(**over):
    row = {"uid": "u1", "direction": "in", "client_phone": "+79161112233",
           "vats_login": "ткачевин", "diversion": "+79214401135",
           "started_at": "2026-09-28T12:00:00+03:00"}
    row.update(over)
    return row


def test_входящий_на_рекламную_линию(conn):
    ctx = call_context.build(conn, звонок(diversion="+78123091309"))
    assert ctx.line == "рекламная"
    assert ctx.line_name == "Авито СПБ"
    assert ctx.scenario == "входящий_на_общий"
    assert "Авито СПБ" in ctx.hint()
    assert "новое обращение" in ctx.hint()


def test_незнакомая_линия_не_считается_рекламной(conn):
    """Номера нет в справочнике — молчим, а не записываем в рекламные."""
    ctx = call_context.build(conn, звонок(diversion="+79990001122"))
    assert ctx.line == "неизвестная"
    assert ctx.scenario != "входящий_на_общий"


def test_входящий_на_прямой_от_заказчика(conn):
    ctx = call_context.build(conn, звонок())
    assert ctx.line == "прямой"
    assert ctx.counterpart == "заказчик"
    assert ctx.scenario == "входящий_от_заказчика"
    assert "3" in ctx.hint()


def test_входящий_от_исполнителя_не_заявка(conn):
    ctx = call_context.build(conn, звонок(client_phone="+79164445566"))
    assert ctx.scenario == "входящий_от_исполнителя"
    assert "не заказчик" in ctx.hint()


def test_перезвон_исполнителя_отличается_от_обращения(conn):
    """Мы звонили ему час назад — значит он перезванивает, а не обращается."""
    conn.execute("INSERT INTO calls VALUES ('u0','out','+79164445566','2026-09-28T11:00:00+03:00')")
    ctx = call_context.build(conn, звонок(client_phone="+79164445566"))
    assert ctx.called_back is True
    assert ctx.scenario == "входящий_перезвон_исполнителя"
    assert "не новое обращение" in ctx.hint()


def test_старый_наш_звонок_за_окном_не_считается(conn):
    conn.execute("INSERT INTO calls VALUES ('u0','out','+79164445566','2026-09-25T11:00:00+03:00')")
    ctx = call_context.build(conn, звонок(client_phone="+79164445566"))
    assert ctx.called_back is False
    assert ctx.scenario == "входящий_от_исполнителя"


def test_исходящий_подборщика_это_работа_по_заявке(conn):
    ctx = call_context.build(conn, звонок(direction="out", vats_login="никитин",
                                          client_phone="+79164445566"))
    assert ctx.scenario == "исходящий_подбор"
    assert "по уже" in ctx.hint()


def test_исходящий_прозвона(conn):
    ctx = call_context.build(conn, звонок(direction="out"))
    assert ctx.scenario == "исходящий_прозвон"


def test_незнакомый_номер_ничего_не_придумывает(conn):
    ctx = call_context.build(conn, звонок(client_phone="+79990000000"))
    assert ctx.counterpart == "незнакомый"
    assert ctx.scenario == "неясный"
    assert "не приписывай" in ctx.hint()


def test_роли_дорожек_названы_прямо(conn):
    текст = call_context.describe(call_context.build(conn, звонок()))
    assert "Левая дорожка (operator) — наш менеджер" in текст
    assert "ПОВОД ЗВОНКА:" in текст
