"""Разбор разговора и фильтры отчёта.

Разбор пишет обвинения в адрес живых людей: «клиент сказал, а ты не записал».
Поэтому проверяем не то, что модель что-то вернула, а то, что мы выбрасываем:
пункт без цитаты, пункт с цитатой, которой в разговоре не было, и пункт про
поле, которое менеджер на самом деле заполнил.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from app.analyzer import (
    already_in_card,
    card_summary,
    dialog_text,
    normalize,
    quote_found,
)
from app.collector import tasks_after_call
from app.db import (
    init_schema,
    save_call,
    save_call_task,
    save_card_check,
    save_transcript,
    upsert_manager,
)
from app.stats import report_rows, report_totals

DAY = "2026-09-16"
TRANSCRIPT = (
    "operator: Добрый день, по поводу аренды техники, актуально?\n"
    "client: Нам на следующей неделе нужен экскаватор погрузчик в Шушарах\n"
    "operator: Хорошо, подберу и перезвоню\n"
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    upsert_manager(c, vats_login="andy", display_name="Андрей", synergy_user="7",
                   plan_calls=None, active=1, is_demo=0)
    save_call(c, uid="1", vats_login="andy", client_phone="79101234567",
              direction="out", status="success", started_at=f"{DAY}T10:00:00+03:00",
              local_date=DAY, local_hour=10, wait_sec=1, duration_sec=90,
              record_url="https://vats/record.mp3", is_demo=0,
              fetched_at=f"{DAY}T12:00:00Z")
    save_card_check(c, call_uid="1", contact_id="55", contact_found=1, need_filled=0,
                    objects_filled=0, inn_filled=0, task_created=0, orders_count=1,
                    deals_count=0, contact_name="Пётр", company_name='ООО "Трест"',
                    need_value="", checked_at=f"{DAY}T12:00:00Z", is_demo=0)
    return c


# --------------------------------------------------------------- цитаты
def test_цитата_из_разговора_принимается():
    assert quote_found("нужен экскаватор погрузчик в Шушарах", TRANSCRIPT)


def test_цитата_с_другими_окончаниями_принимается():
    # Модель склеивает реплики и правит слова — дословного совпадения не требуем.
    assert quote_found("нам нужен экскаватор погрузчик Шушары", TRANSCRIPT)


def test_выдуманная_цитата_отбрасывается():
    data = {"missed": [{"field": "потребность", "value": "нужен автокран",
                        "quote": "клиент просил автокран на понедельник"}]}
    assert normalize(data, TRANSCRIPT)["missed"] == []


def test_пункт_без_цитаты_отбрасывается():
    data = {"missed": [{"field": "потребность", "value": "нужен экскаватор", "quote": ""}]}
    assert normalize(data, TRANSCRIPT)["missed"] == []


def test_пункт_с_цитатой_остаётся():
    data = {"missed": [{"field": "потребность", "value": "экскаватор погрузчик, Шушары",
                        "quote": "нужен экскаватор погрузчик в Шушарах"}]}
    missed = normalize(data, TRANSCRIPT)["missed"]
    assert len(missed) == 1
    assert missed[0]["field"] == "потребность"


# ------------------------------------------------- сверка с карточкой
def test_заполненное_поле_не_считается_упущенным():
    call = {"company_name": 'ООО "Трест"', "tasks": []}
    assert already_in_card("компания", call)
    data = {"missed": [{"field": "компания", "value": 'ООО "Трест"',
                        "quote": "нужен экскаватор погрузчик в Шушарах"}]}
    assert normalize(data, TRANSCRIPT, call)["missed"] == []


def test_потребность_показываем_даже_если_поле_заполнено():
    # В карточке может стоять «автовышка», а в разговоре звучать экскаватор —
    # это и есть расхождение, ради которого всё затевалось.
    call = {"need_value": "автовышка", "company_name": "", "tasks": []}
    data = {"missed": [{"field": "потребность", "value": "экскаватор погрузчик",
                        "quote": "нужен экскаватор погрузчик в Шушарах"}]}
    assert len(normalize(data, TRANSCRIPT, call)["missed"]) == 1


def test_оценка_приводится_к_шкале():
    assert normalize({"call_quality": "9"})["call_quality"] == 5
    assert normalize({"call_quality": "нет"})["call_quality"] is None


def test_рекомендаций_не_больше_трёх():
    data = {"recommendations": ["раз", "два", "три", "четыре"]}
    assert normalize(data)["recommendations"] == ["раз", "два", "три"]


def test_карточка_для_модели_перечисляет_пробелы():
    text = card_summary({"need_value": "", "company_name": 'ООО "Трест"',
                         "objects_filled": 0, "inn_filled": 1, "tasks": [], "orders": []})
    assert "потребность в карточке: не заполнена" in text
    assert "задач после звонка не поставлено" in text


def test_диалог_склеивается_по_ролям():
    text = dialog_text([{"role": "operator", "text": "Алло"},
                        {"role": "client", "text": "Да"}])
    assert text == "operator: Алло\nclient: Да"


# --------------------------------------------------------------- задачи
def test_задача_привязывается_только_в_окне_после_звонка():
    index = {"55": [
        {"task_id": "1", "created_at": f"{DAY}T10:10:00+03:00", "name": "Перезвонить"},
        {"task_id": "2", "created_at": f"{DAY}T12:00:00+03:00", "name": "Поздно"},
        {"task_id": "3", "created_at": f"{DAY}T09:00:00+03:00", "name": "Раньше звонка"},
    ]}
    found = tasks_after_call(index, "55", f"{DAY}T10:00:00+03:00", 30)
    assert [t["task_id"] for t in found] == ["1"]


# --------------------------------------------------------------- отчёт
def _filters(**over):
    base = {"need": "", "objects": "", "inn": "", "task": "", "contact": "",
            "company": "", "orders": "", "transcript": "", "missed": "", "q": "",
            "min_sec": 0}
    base.update(over)
    return base


def test_фильтр_по_незаполненной_потребности(conn):
    assert len(report_rows(conn, DAY, DAY, filters=_filters(need="0"))) == 1
    assert report_rows(conn, DAY, DAY, filters=_filters(need="1")) == []


def test_фильтр_по_упущенному_в_crm(conn):
    assert report_rows(conn, DAY, DAY, filters=_filters(missed="1")) == []
    save_transcript(conn, call_uid="1", text=TRANSCRIPT, analysis_json=json.dumps(
        {"summary": "о технике", "missed": [
            {"field": "потребность", "value": "экскаватор", "quote": "нужен экскаватор"}]},
        ensure_ascii=False,
    ), created_at=datetime.now(timezone.utc).isoformat(), is_demo=0)
    rows = report_rows(conn, DAY, DAY, filters=_filters(missed="1"))
    assert len(rows) == 1
    assert rows[0]["missed"][0]["value"] == "экскаватор"
    assert report_totals(rows)["with_missed"] == 1


def test_фильтр_по_поиску_смотрит_в_компанию(conn):
    assert len(report_rows(conn, DAY, DAY, filters=_filters(q="Трест"))) == 1
    assert report_rows(conn, DAY, DAY, filters=_filters(q="Рога и копыта")) == []


def test_задачи_попадают_в_строку_отчёта(conn):
    save_call_task(conn, task_id="9", call_uid="1", name="Перезвонить 22-го",
                   created_at=f"{DAY}T10:05:00+03:00", due_date=f"{DAY}T12:00:00+03:00",
                   status="opened", responsible="Андрей", completed_at="", is_demo=0)
    rows = report_rows(conn, DAY, DAY)
    assert rows[0]["tasks"][0]["name"] == "Перезвонить 22-го"
    assert report_totals(rows)["tasks_made"] == 1
