"""Поиск техники менеджерами продаж (15.11.2026): цена, занятость, привязка к заявке,
защита карточек транспорта от дублей."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import analyzer, search_calls, search_sales
from app.db import (
    init_schema, save_call, save_call_order_link, save_order_index, save_transport_card,
)


class _S:
    transport_ai_mode = "propose"
    transport_ai_min_sure = 70
    link_ai_mode = "propose"
    link_max_age_days = 14


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    save_transport_card(c, id="1", name="Каток", type_id="t-roll", type_name="Каток",
                        phone10="9101234567", contact_id="c1", contact_name="Поставщик",
                        status="", updated_at="2026-11-15", synced_at="2026-11-15", call_link=None)
    save_transport_card(c, id="2", name="Самосвал", type_id="t-dump", type_name="Самосвал",
                        phone10="9109999999", contact_id="c2", contact_name="Другой",
                        status="", updated_at="2026-11-15", synced_at="2026-11-15", call_link=None)
    return c


TEXT = ("К: Каток есть, смена двадцать пять тысяч. "
        "Р: А когда освободится? К: Самосвал занят, освободится в пятницу.")


def _heard(monkeypatch, data):
    monkeypatch.setattr(analyzer, "_спросить_голову", lambda *a, **k: data)
    return search_calls.heard_types(TEXT, ["Каток", "Самосвал"], api_key="k", model="m")


def test_price_and_busy_are_kept_only_with_real_quote(conn, monkeypatch):
    heard = _heard(monkeypatch, {
        "asked": "Каток", "has": [{"type": "Каток", "quote": "Каток есть"}],
        "prices": [
            {"type": "Каток", "amount": 25000, "unit": "смена", "quote": "смена двадцать пять тысяч"},
            {"type": "Каток", "amount": 99999, "unit": "смена", "quote": "этого в разговоре нет"},
            {"type": "Бульдозер", "amount": 1, "unit": "час", "quote": "Каток есть"},
        ],
        "busy": [
            {"type": "Самосвал", "state": "занята", "free_from": "в пятницу",
             "manager_asked": True, "quote": "Самосвал занят, освободится в пятницу"},
            {"type": "Самосвал", "state": "непонятно", "quote": "Самосвал занят"},
        ],
        "sure": 90,
    })
    assert [p["amount"] for p in heard["prices"]] == [25000.0]
    assert heard["prices"][0]["unit"] == "смена"
    assert len(heard["busy"]) == 1 and heard["busy"][0]["free_from"] == "в пятницу"


def test_word_instead_of_date_is_not_a_date(conn, monkeypatch):
    heard = _heard(monkeypatch, {
        "asked": "Каток", "has": [],
        "busy": [{"type": "Самосвал", "state": "занята", "free_from": "неизвестно",
                  "manager_asked": True, "quote": "Самосвал занят"}],
        "sure": 80,
    })
    assert heard["busy"][0]["free_from"] == ""
    assert heard["busy"][0]["manager_asked"] is True


def test_price_without_number_is_dropped(conn, monkeypatch):
    heard = _heard(monkeypatch, {
        "asked": "Каток", "has": [],
        "prices": [{"type": "Каток", "amount": "дорого", "unit": "смена", "quote": "Каток есть"}],
        "sure": 80,
    })
    assert heard["prices"] == []


def _order(conn, oid, user="30557", equipment="Каток", created="2026-11-15T07:00:00+00:00",
           kind="opened"):
    save_order_index(conn, order_id=oid, number=oid, responsible_id=user, equipment=equipment,
                     stage_kind=kind, created_at=created, updated_at=created,
                     fetched_at=created)


def test_one_matching_open_order_gives_high_confidence(conn):
    _order(conn, "100", equipment="Каток")
    _order(conn, "101", equipment="Автокран")
    choice = search_sales.choose_order(conn, synergy_user="30557",
                                       started_at="2026-11-15T10:00:00+00:00", asked_type="Каток")
    assert choice["order_id"] == "100" and choice["confidence"] == search_sales.HIGH


def test_other_manager_order_is_not_chosen(conn):
    _order(conn, "100", user="999")
    choice = search_sales.choose_order(conn, synergy_user="30557",
                                       started_at="2026-11-15T10:00:00+00:00", asked_type="Каток")
    assert choice["order_id"] is None


def test_order_created_after_call_or_closed_is_not_chosen(conn):
    _order(conn, "100", created="2026-11-15T12:00:00+00:00")
    _order(conn, "101", kind="lost")
    choice = search_sales.choose_order(conn, synergy_user="30557",
                                       started_at="2026-11-15T10:00:00+00:00", asked_type="Каток")
    assert choice["order_id"] is None


def test_several_matches_is_only_medium(conn):
    _order(conn, "100", created="2026-11-15T06:00:00+00:00")
    _order(conn, "101", created="2026-11-15T08:00:00+00:00")
    choice = search_sales.choose_order(conn, synergy_user="30557",
                                       started_at="2026-11-15T10:00:00+00:00", asked_type="Каток")
    assert choice["order_id"] == "101" and choice["confidence"] == search_sales.MEDIUM


def test_unknown_equipment_on_order_is_medium_not_high(conn):
    _order(conn, "100", equipment="")
    choice = search_sales.choose_order(conn, synergy_user="30557",
                                       started_at="2026-11-15T10:00:00+00:00", asked_type="Каток")
    assert choice["order_id"] == "100" and choice["confidence"] == search_sales.MEDIUM


def _call(conn):
    save_call(conn, uid="c1", vats_login="mgr", client_phone="+79101234567",
              direction="out", status="success", started_at="2026-11-15T10:00:00Z",
              local_date="2026-11-15", local_hour=13, wait_sec=3, duration_sec=60,
              record_url=None, in_group=0, is_demo=0, fetched_at="2026-11-15T10:00:00Z")


def test_proposal_for_missing_type_and_no_duplicate(conn):
    _call(conn)
    heard = {"sure": 90, "has": [{"type": "Самосвал", "quote": "Самосвал занят"}]}
    first = search_sales.propose_transport_adds(
        conn, _S(), None, uid="c1", phone10="9101234567", heard=heard, missing=["Самосвал"])
    again = search_sales.propose_transport_adds(
        conn, _S(), None, uid="c1", phone10="9101234567", heard=heard, missing=["Самосвал"])
    assert [i["type"] for i in first] == ["Самосвал"] and first[0]["mode"] == "propose"
    assert again == []
    rows = conn.execute("SELECT mode FROM ai_transport_adds").fetchall()
    assert [r["mode"] for r in rows] == ["proposed"]


def test_low_confidence_is_shown_in_propose_but_blocks_create(conn):
    _call(conn)
    heard = {"sure": 40, "has": [{"type": "Самосвал", "quote": "q"}]}
    shown = search_sales.propose_transport_adds(
        conn, _S(), None, uid="c1", phone10="9101234567", heard=heard, missing=["Самосвал"])
    assert [i["type"] for i in shown] == ["Самосвал"]
    assert conn.execute("SELECT sure FROM ai_transport_adds").fetchone()["sure"] == 40
    create = _S()
    create.transport_ai_mode = "create"
    assert search_sales.propose_transport_adds(
        conn, create, None, uid="c1", phone10="9109999000", heard=heard, missing=["Самосвал"]) == []


def test_customer_numbers_are_skipped(conn):
    _call(conn)
    heard = {"sure": 95, "has": [{"type": "Самосвал", "quote": "q"}]}
    conn.execute("INSERT INTO numbers (phone10, name, contact_id, category, cards, types, orders, "
                 "resolved_at) VALUES ('9101234567','','','заказчик',0,0,3,'x')")
    assert search_sales.propose_transport_adds(
        conn, _S(), None, uid="c1", phone10="9101234567", heard=heard, missing=["Самосвал"]) == []


def test_mode_off_does_nothing(conn):
    _call(conn)
    s = _S()
    s.transport_ai_mode = "off"
    heard = {"sure": 99, "has": [{"type": "Самосвал", "quote": "q"}]}
    assert search_sales.propose_transport_adds(
        conn, s, None, uid="c1", phone10="9101234567", heard=heard, missing=["Самосвал"]) == []


def test_helper_link_is_not_overwritten_by_ai(conn):
    _call(conn)
    save_call_order_link(conn, "c1", "100", "helper", confidence="помощник", at="2026-11-15")
    save_call_order_link(conn, "c1", "200", "ai", confidence="высокая", at="2026-11-15")
    row = conn.execute("SELECT order_id, source FROM call_order_links WHERE call_uid='c1'").fetchone()
    assert (row["order_id"], row["source"]) == ("100", "helper")


CUSTOMER_TEXT = ("менеджер: Добрый день, по поводу манипулятора. Сегодня не положим, "
                 "к сожалению, заняты. Если не найдёте, звоните.\n"
                 "клиент: Понял, спасибо, я уже нашёл.")


def test_customer_call_is_not_a_search_for_sales_channel(conn, monkeypatch):
    monkeypatch.setattr(analyzer, "_спросить_голову", lambda *a, **k: {
        "asked": "Каток", "counterpart": "заказчик",
        "has": [{"type": "Каток", "quote": "по поводу манипулятора"}],
        "busy": [{"type": "Каток", "state": "занята", "quote": "Сегодня не положим",
                  "manager_asked": False}],
        "sure": 90})
    heard = search_calls.heard_types(CUSTOMER_TEXT, ["Каток"], api_key="k", model="m",
                                     require_supplier=True)
    assert heard["has"] == [] and heard["busy"] == [] and heard["asked"] == ""
    assert heard["counterpart"] == "заказчик"


def test_customer_gate_is_off_for_the_search_department(conn, monkeypatch):
    # Поток 4 (отдел поиска) эту проверку не получает: меняем только новый канал.
    monkeypatch.setattr(analyzer, "_спросить_голову", lambda *a, **k: {
        "asked": "Каток", "counterpart": "заказчик",
        "has": [{"type": "Каток", "quote": "по поводу манипулятора"}], "sure": 90})
    heard = search_calls.heard_types(CUSTOMER_TEXT, ["Каток"], api_key="k", model="m")
    assert [i["type"] for i in heard["has"]] == ["Каток"]


def test_quote_from_manager_line_is_rejected_for_supplier(conn, monkeypatch):
    monkeypatch.setattr(analyzer, "_спросить_голову", lambda *a, **k: {
        "asked": "Каток", "counterpart": "исполнитель",
        "has": [{"type": "Каток", "quote": "по поводу манипулятора"}],  # сказал менеджер
        "busy": [{"type": "Каток", "state": "занята", "quote": "Сегодня не положим",
                  "manager_asked": False}],
        "sure": 90})
    heard = search_calls.heard_types(CUSTOMER_TEXT, ["Каток"], api_key="k", model="m",
                                     require_supplier=True)
    assert heard["has"] == [] and heard["busy"] == []


def test_supplier_quote_in_counterpart_line_is_kept(conn, monkeypatch):
    text = "менеджер: Каток нужен.\nклиент: Каток есть, смена двадцать тысяч."
    monkeypatch.setattr(analyzer, "_спросить_голову", lambda *a, **k: {
        "asked": "Каток", "counterpart": "исполнитель",
        "has": [{"type": "Каток", "quote": "Каток есть"}],
        "prices": [{"type": "Каток", "amount": 20000, "unit": "смена",
                    "quote": "смена двадцать тысяч"}], "sure": 80})
    heard = search_calls.heard_types(text, ["Каток"], api_key="k", model="m",
                                     require_supplier=True)
    assert [i["type"] for i in heard["has"]] == ["Каток"]
    assert heard["prices"][0]["amount"] == 20000.0
