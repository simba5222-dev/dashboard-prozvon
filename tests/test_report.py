"""Развёрнутый отчёт: что произошло по каждому разговору.

Дашборд отвечает на вопрос «сколько», отчёт — на вопрос «что вышло». Цена
ошибки здесь выше: по этой таблице владелец судит о работе людей, поэтому
проверяем, что заявка попадает в строку только если заведена после звонка,
что итог читается с той же стадии, что в CRM, и что непроверенная строка
не выглядит как «менеджер ничего не сделал».
"""

from __future__ import annotations

import sqlite3

import pytest

from app.collector import orders_after_call, text_of
from app.db import init_schema, save_call, save_call_order, save_card_check, upsert_manager
from app.stats import report_rows, report_totals

THRESHOLD = 15
DAY = "2026-09-16"
STAGES = {"84": ("Сделка", "won"), "85": ("Сделка провалена", "lost"),
          "81": ("Новый", "opened"), "4583": ("Выставлен счёт", "")}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    upsert_manager(c, vats_login="andy", display_name="Андрей", synergy_user="7",
                   plan_calls=None, active=1, is_demo=0)
    return c


def add_call(conn, uid, *, duration=90, hour=10):
    save_call(conn, uid=uid, vats_login="andy", client_phone="79101234567",
              direction="out", status="success", started_at=f"{DAY}T{hour:02d}:00:00+03:00",
              local_date=DAY, local_hour=hour, wait_sec=2, duration_sec=duration,
              record_url=None, is_demo=0, fetched_at=f"{DAY}T12:00:00Z")


def add_check(conn, uid, **over):
    row = dict(call_uid=uid, contact_id="1", contact_found=1, need_filled=1,
               objects_filled=1, inn_filled=1, task_created=1, orders_count=3,
               deals_count=1, contact_name="Пётр", company_name='ООО "Трест"',
               need_value="Экскаватор", checked_at=f"{DAY}T12:00:00Z", is_demo=0)
    row.update(over)
    save_card_check(conn, **row)


def add_order(conn, uid, order_id, *, kind="won", who="Быков С."):
    save_call_order(conn, order_id=order_id, call_uid=uid, name=f"№{order_id}",
                    created_at=f"{DAY}T11:00:00+03:00", responsible=who,
                    stage_name=STAGES[{"won": "84", "lost": "85", "opened": "81"}[kind]][0],
                    stage_kind=kind, amount=0.0, is_demo=0)


# ------------------------------------------------------------------- строки

def test_short_calls_are_left_out(conn):
    """Недозвон в отчёте бессмыслен: рассказывать не о чем, а таблицу он топит."""
    add_call(conn, "talk", duration=90)
    add_call(conn, "beep", duration=5, hour=11)
    conn.commit()
    uids = {r["uid"] for r in report_rows(conn, DAY, DAY, threshold_sec=THRESHOLD)}
    assert uids == {"talk"}


def test_row_carries_card_contents_not_only_ticks(conn):
    """Владельцу нужна не галочка «потребность есть», а сама потребность."""
    add_call(conn, "c1")
    add_check(conn, "c1")
    conn.commit()
    row = report_rows(conn, DAY, DAY, threshold_sec=THRESHOLD)[0]
    assert row["contact_name"] == "Пётр"
    assert row["company_name"] == 'ООО "Трест"'
    assert row["need_value"] == "Экскаватор"


def test_orders_and_responsibles_land_in_the_row(conn):
    add_call(conn, "c1")
    add_check(conn, "c1")
    add_order(conn, "c1", "900", kind="won", who="Быков С.")
    add_order(conn, "c1", "901", kind="lost", who="Скатченко С.")
    conn.commit()
    row = report_rows(conn, DAY, DAY, threshold_sec=THRESHOLD)[0]
    assert row["orders_made"] == 2
    assert row["responsibles"] == ["Быков С.", "Скатченко С."]


def test_unchecked_row_is_marked_as_unchecked(conn):
    """Пока сборщик не дошёл до звонка, пустые колонки — не вина менеджера."""
    add_call(conn, "c1")
    conn.commit()
    row = report_rows(conn, DAY, DAY, threshold_sec=THRESHOLD)[0]
    assert row["checked"] is False
    assert row["orders_made"] == 0


def test_old_check_without_details_is_not_read_as_manager_fault(conn):
    """Звонок, проверенный до появления отчёта: галочки есть, подробностей нет.

    Такая строка не должна выглядеть как «клиента нет, компании нет»: это
    пробел сбора, и отличает его только признак detailed.
    """
    add_call(conn, "c1")
    add_check(conn, "c1", contact_name=None, company_name=None, need_value=None)
    conn.commit()
    row = report_rows(conn, DAY, DAY, threshold_sec=THRESHOLD)[0]
    assert row["checked"] is True
    assert row["detailed"] is False
    assert report_totals([row])["no_details"] == 1


def test_manager_filter_narrows_rows(conn):
    upsert_manager(conn, vats_login="bob", display_name="Борис", synergy_user="8",
                   plan_calls=None, active=1, is_demo=0)
    add_call(conn, "c1")
    save_call(conn, uid="c2", vats_login="bob", client_phone="79990000000",
              direction="out", status="success", started_at=f"{DAY}T10:00:00+03:00",
              local_date=DAY, local_hour=10, wait_sec=1, duration_sec=90,
              record_url=None, is_demo=0, fetched_at=f"{DAY}T12:00:00Z")
    conn.commit()
    rows = report_rows(conn, DAY, DAY, vats_login="bob", threshold_sec=THRESHOLD)
    assert [r["uid"] for r in rows] == ["c2"]


# -------------------------------------------------------------------- итоги

def test_totals_split_orders_by_outcome(conn):
    add_call(conn, "c1")
    add_check(conn, "c1")
    add_order(conn, "c1", "900", kind="won")
    add_order(conn, "c1", "901", kind="lost")
    add_order(conn, "c1", "902", kind="opened")
    conn.commit()
    totals = report_totals(report_rows(conn, DAY, DAY, threshold_sec=THRESHOLD))
    assert (totals["won"], totals["lost"], totals["in_work"]) == (1, 1, 1)
    assert totals["orders"] == 3


def test_totals_count_company_by_value_not_by_tick(conn):
    """Компании нет — это факт о работе менеджера, и он должен попасть в итог."""
    add_call(conn, "c1")
    add_call(conn, "c2", hour=11)
    add_check(conn, "c1", company_name="")
    add_check(conn, "c2", contact_id="2", company_name='ООО "Трест"')
    conn.commit()
    totals = report_totals(report_rows(conn, DAY, DAY, threshold_sec=THRESHOLD))
    assert totals["companies"] == 1
    assert totals["calls"] == 2


# ------------------------------------------------ отбор заявок по времени

class FakeClient:
    """Отдаёт заранее заданный ответ Synergy: связи живут в разделе included."""

    def __init__(self, orders):
        self.orders = orders

    def get(self, path, **params):
        data, included = [], []
        for i, (order_id, created, stage_id, user_id) in enumerate(self.orders):
            data.append({
                "id": order_id, "type": "orders",
                "attributes": {"created-at": created, "name": "Заявка",
                               "number": 1000 + i, "amount": 0},
                "relationships": {
                    "stage": {"data": {"type": "order-stages", "id": stage_id}},
                    "responsible": {"data": {"type": "users", "id": user_id}},
                },
            })
        included.append({"type": "users", "id": "7",
                         "attributes": {"as-string": "Быков Сергей"}})
        return {"data": data, "included": included}


def test_only_orders_created_after_the_call_count():
    """Заявка, заведённая до звонка, звонку не заслуга."""
    client = FakeClient([
        ("900", "2026-09-16T11:00:00+03:00", "84", "7"),   # через час после
        ("901", "2026-09-15T09:00:00+03:00", "84", "7"),   # накануне
    ])
    got = orders_after_call(client, "1", f"{DAY}T10:00:00+03:00", 24, STAGES)
    assert [o["order_id"] for o in got] == ["900"]


def test_order_outside_the_window_is_not_counted():
    client = FakeClient([("900", "2026-09-18T11:00:00+03:00", "84", "7")])
    assert orders_after_call(client, "1", f"{DAY}T10:00:00+03:00", 24, STAGES) == []


def test_stage_without_kind_is_neither_win_nor_loss():
    """«Выставлен счёт» — не исход: вид у стадии пустой, и это верно."""
    client = FakeClient([("900", "2026-09-16T11:00:00+03:00", "4583", "7")])
    order = orders_after_call(client, "1", f"{DAY}T10:00:00+03:00", 24, STAGES)[0]
    assert order["stage_name"] == "Выставлен счёт"
    assert order["stage_kind"] == ""
    assert order["responsible"] == "Быков Сергей"


def test_multivalue_field_is_joined_not_stringified():
    """Поля мультивыбора Synergy отдаёт списком — в отчёте нужен текст."""
    assert text_of(["Экскаватор", "Каток"]) == "Экскаватор, Каток"
    assert text_of(None) == ""
