"""Заявки: страница с проваливанием и разбор «почему не дошла до сделки».

Главная тонкость здесь — чужие звонки. Заявку ведёт не тот, кто её завёл,
поэтому в базу попадают звонки других менеджеров. В разбор заявки они обязаны
попадать, а в отчёт по прозвону — ни в коем случае: иначе менеджеру припишут
чужую работу.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from app.analyzer import calls_summary, normalize_verdict, order_summary
from app.db import (
    init_schema,
    save_call,
    save_call_order,
    save_card_check,
    save_order_report,
    upsert_manager,
)
from app.collector import contact_orders_around, group_logins
from app.stats import order_detail, orders_of_period, report_rows

DAY = "2026-09-16"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    upsert_manager(c, vats_login="andy", display_name="Андрей", synergy_user="7",
                   plan_calls=None, active=1, is_demo=0)
    # Звонок прозвона, с которого пошла заявка.
    save_call(c, uid="1", vats_login="andy", client_phone="+79101234567",
              direction="out", status="success", started_at=f"{DAY}T10:00:00+03:00",
              local_date=DAY, local_hour=10, wait_sec=1, duration_sec=90,
              record_url=None, is_demo=0, fetched_at=f"{DAY}T12:00:00Z")
    save_card_check(c, call_uid="1", contact_id="55", contact_found=1, need_filled=1,
                    objects_filled=0, inn_filled=0, task_created=0, orders_count=1,
                    deals_count=0, contact_name="Пётр", company_name='ООО "Трест"',
                    need_value="автовышка", checked_at=f"{DAY}T12:00:00Z", is_demo=0)
    save_call_order(c, order_id="900", call_uid="1", name="№107 автовышка",
                    created_at=f"{DAY}T10:30:00+03:00", responsible="Толстов И.",
                    stage_name="Сделка провалена", stage_kind="lost", amount=0.0, is_demo=0)
    # Звонок ответственного тому же клиенту — уже после заявки и не из группы.
    save_call(c, uid="2", vats_login="толстов", client_phone="79101234567",
              direction="out", status="success", started_at=f"{DAY}T11:00:00+03:00",
              local_date=DAY, local_hour=11, wait_sec=1, duration_sec=80,
              record_url=None, in_group=0, is_demo=0, fetched_at=f"{DAY}T12:00:00Z")
    return c


def test_чужой_звонок_не_попадает_в_отчёт_прозвона(conn):
    rows = report_rows(conn, DAY, DAY)
    assert [r["uid"] for r in rows] == ["1"]


def test_в_заявку_попадают_звонки_после_её_создания(conn):
    order = order_detail(conn, "900")
    assert order["responsible"] == "Толстов И."
    assert [c["uid"] for c in order["calls"]] == ["2"]
    assert order["calls"][0]["in_group"] == 0


def test_звонок_другому_клиенту_в_заявку_не_идёт(conn):
    save_call(conn, uid="3", vats_login="толстов", client_phone="79990000000",
              direction="out", status="success", started_at=f"{DAY}T11:30:00+03:00",
              local_date=DAY, local_hour=11, wait_sec=1, duration_sec=60,
              record_url=None, in_group=0, is_demo=0, fetched_at=f"{DAY}T12:00:00Z")
    assert [c["uid"] for c in order_detail(conn, "900")["calls"]] == ["2"]


def test_вердикт_читается_со_страницы_заявки(conn):
    save_order_report(conn, order_id="900", contact_id="55", calls_count=1,
                      verdict_json=json.dumps({"outcome": "клиент нашёл технику сам"},
                                              ensure_ascii=False),
                      created_at=datetime.now(timezone.utc).isoformat())
    assert order_detail(conn, "900")["verdict"]["outcome"] == "клиент нашёл технику сам"


def test_причина_проигрыша_видна_в_строке_отчёта(conn):
    # Владелец смотрит таблицу целиком: ради одной фразы проваливаться
    # в каждую заявку он не должен.
    save_order_report(conn, order_id="900", contact_id="55", calls_count=1,
                      verdict_json=json.dumps({"outcome": "клиент нашёл технику сам"},
                                              ensure_ascii=False),
                      created_at=datetime.now(timezone.utc).isoformat())
    row = report_rows(conn, DAY, DAY)[0]
    assert row["orders"][0]["reason"] == "клиент нашёл технику сам"


def test_без_разбора_причины_в_строке_нет(conn):
    assert report_rows(conn, DAY, DAY)[0]["orders"][0]["reason"] == ""


def test_отбор_заявок_по_исходу(conn):
    assert len(orders_of_period(conn, DAY, DAY, "lost")) == 1
    assert orders_of_period(conn, DAY, DAY, "won") == []


def test_несуществующая_заявка(conn):
    assert order_detail(conn, "нет такой") is None


# ------------------------------------------------------- разбор заявки
def test_недозвон_в_сводке_помечен_отдельно():
    text = calls_summary([{"started_at": f"{DAY}T11:00:00+03:00", "direction": "out",
                           "vats_login": "толстов", "duration_sec": 8,
                           "transcript_text": ""}])
    assert "не поговорили" in text


def test_нет_звонков_так_и_сказано():
    assert "не найдено" in calls_summary([])


def test_заявка_для_модели_содержит_ответственного():
    text = order_summary({"number": 107, "name": "автовышка", "responsible": "Толстов И.",
                          "stage_name": "Сделка провалена", "created_at": f"{DAY}T10:30:00+03:00"})
    assert "Толстов И." in text and "Сделка провалена" in text


def test_вердикт_обрезает_длинные_списки():
    verdict = normalize_verdict({"gaps": ["раз", "два", "три", "четыре"],
                                 "recommendations": "перезвонить",
                                 "recoverable": 1})
    assert verdict["gaps"] == ["раз", "два", "три"]
    assert verdict["recommendations"] == ["перезвонить"]
    assert verdict["recoverable"] is True


# ------------------------------------------------- кто считается «своим»

def test_group_logins_excludes_other_departments(conn):
    """Признак «звонок прозвона» ставится только по отделу прозвона.

    18.09.2026 в ту же таблицу добавили отдел продаж — их входящие нужны, чтобы
    ловить потерянные заявки. Сборщик брал оттуда всех подряд, и исходящие
    продажников стали помечаться как звонки прозвона: 545 чужих звонков в
    отчёте об одном человеке.
    """
    upsert_manager(conn, vats_login="sales", display_name="Продажник",
                   synergy_user="8", plan_calls=None, active=1,
                   dept="продажи", is_demo=0)
    assert group_logins(conn, "прозвон") == {"andy"}


def test_group_logins_keeps_dismissed_managers(conn):
    """Уволенный менеджер остаётся «своим»: он звонил тогда, когда звонил.

    Иначе пересчёт признака задним числом стёр бы его работу из истории.
    """
    upsert_manager(conn, vats_login="gone", display_name="Ушедший",
                   synergy_user="9", plan_calls=None, active=0, is_demo=0)
    assert group_logins(conn, "прозвон") == {"andy", "gone"}


# ------------------------------------------- живая заявка против заброшенной

class _FakeSynergy:
    """Отдаёт заранее собранный список заявок контакта."""

    def __init__(self, orders):
        self._orders = orders

    def get(self, _path, **_kw):
        return {"data": self._orders}


def _order(oid, stage_id, created, updated=None):
    return {"id": oid,
            "attributes": {"name": f"№{oid}", "created-at": created,
                           "updated-at": updated or created},
            "relationships": {"stage": {"data": {"id": stage_id}}}}


STAGES = {"s-new": ("Новый", "opened"), "s-won": ("Сделка", "won")}
CALL = "2026-09-22T10:00:00+00:00"


def test_брошенная_заявка_не_считается_открытой():
    """«Новый» двухлетней давности — не работа, а мертвец в базе.

    Закрывают стадию только «Сделка» и «Сделка провалена», поэтому такие
    висят открытыми годами: в базе нашлись возрастом 139, 169 и 836 дней.
    Считать их живыми — значит терять новые запросы от старых клиентов,
    а это ровно те заказы, про которые менеджер забывает.
    """
    client = _FakeSynergy([_order("1", "s-new", "2024-06-01T10:00:00+00:00")])
    after, active = contact_orders_around(client, "c1", CALL, STAGES, fresh_days=30)
    assert after == [] and active == []


def test_заявку_трогали_на_днях_значит_она_живая():
    client = _FakeSynergy([_order("1", "s-new", "2026-05-01T10:00:00+00:00",
                                  updated="2026-09-20T10:00:00+00:00")])
    after, active = contact_orders_around(client, "c1", CALL, STAGES, fresh_days=30)
    assert active == ["№1"]


def test_закрытая_сделка_живой_не_считается():
    client = _FakeSynergy([_order("1", "s-won", "2026-09-20T10:00:00+00:00")])
    after, active = contact_orders_around(client, "c1", CALL, STAGES, fresh_days=30)
    assert after == [] and active == []


def test_заявка_вокруг_звонка_попадает_в_after():
    """Менеджер завёл её сам, пока мы считали, — это не «открытая», а «уже есть»."""
    client = _FakeSynergy([_order("1", "s-new", "2026-09-22T09:55:00+00:00")])
    after, active = contact_orders_around(client, "c1", CALL, STAGES, fresh_days=30)
    assert after == ["№1"] and active == []
