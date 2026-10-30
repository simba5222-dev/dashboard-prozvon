"""«Компания И» и «ИНН И» должны повторять компанию исполнителя,
найденного по телефону на транспорте сделки — чисто по данным, без сети
(webhook сам читает/пишет через SynergyClient, здесь только логика
решения)."""

from app.deal_company_sync import (
    FIELD_PERFORMER_COMPANY,
    FIELD_PERFORMER_INN,
    FIELD_TRANSPORT_PHONE,
    decide_updates,
    decide_value,
    extract_deal_id,
    pick_transport_phone,
)


def test_pick_transport_phone_first_match():
    transports = [
        {"attributes": {"customs": {FIELD_TRANSPORT_PHONE: None}}},
        {"attributes": {"customs": {FIELD_TRANSPORT_PHONE: "+79213119332"}}},
    ]
    assert pick_transport_phone(transports) == "+79213119332"


def test_pick_transport_phone_none_when_empty():
    transports = [{"attributes": {"customs": {FIELD_TRANSPORT_PHONE: ""}}}]
    assert pick_transport_phone(transports) is None


def test_pick_transport_phone_no_transports():
    assert pick_transport_phone([]) is None


def test_decide_value_copies_when_different():
    assert decide_value(None, "566776") == "566776"
    assert decide_value("1", "566776") == "566776"


def test_decide_value_skips_when_already_equal():
    assert decide_value("566776", "566776") is None


def test_decide_value_skips_when_candidate_empty():
    assert decide_value("1", None) is None


def test_decide_value_compares_as_string_int_vs_str():
    # Synergy отдаёт id/ИНН то числом, то строкой -- сравнение не должно на этом спотыкаться.
    assert decide_value(566776, "566776") is None
    assert decide_value(781641296449, 781641296449) is None


def test_decide_updates_both_fields_empty_on_deal():
    updates = decide_updates({}, "566776", "781641296449")
    assert updates == {FIELD_PERFORMER_COMPANY: "566776", FIELD_PERFORMER_INN: "781641296449"}


def test_decide_updates_only_inn_missing():
    current = {FIELD_PERFORMER_COMPANY: "566776", FIELD_PERFORMER_INN: None}
    updates = decide_updates(current, "566776", "781641296449")
    assert updates == {FIELD_PERFORMER_INN: "781641296449"}


def test_decide_updates_nothing_to_change():
    current = {FIELD_PERFORMER_COMPANY: "566776", FIELD_PERFORMER_INN: "781641296449"}
    assert decide_updates(current, "566776", "781641296449") == {}


def test_decide_updates_no_inn_on_company():
    current = {FIELD_PERFORMER_COMPANY: None, FIELD_PERFORMER_INN: None}
    updates = decide_updates(current, "566776", None)
    assert updates == {FIELD_PERFORMER_COMPANY: "566776"}


def test_extract_deal_id_top_level():
    assert extract_deal_id({"id": "3642022"}) == "3642022"


def test_extract_deal_id_nested_deal():
    assert extract_deal_id({"deal": {"id": 3642022}}) == "3642022"


def test_extract_deal_id_nested_data_attributes():
    assert extract_deal_id({"data": {"attributes": {"id": 3642022}}}) == "3642022"


def test_extract_deal_id_missing():
    assert extract_deal_id({"foo": "bar"}) is None


def test_extract_deal_id_not_a_dict():
    assert extract_deal_id("не словарь") is None
