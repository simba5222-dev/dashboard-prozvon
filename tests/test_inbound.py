"""Приёмник входящих: какой звонок вообще наш.

Сценарии различаются только тем, КУДА звонил клиент. Рекламный номер — это
звонок с рекламы, им занимается боевой сервер. Прямой номер менеджера — это
личный звонок, и вот в нём имеет смысл искать заявку, которую менеджер забыл
завести. Перепутать их дорого: 21.09.2026 из 18 заведённых заявок 8 оказались
по звонкам на рекламные номера, где менеджер лид уже вёл.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import init_schema, upsert_manager
from app.inbound import process, sales_number

# «Авито СПБ» — рекламный номер компании. ВАТС переводит такой звонок на
# свободного менеджера, и его прямой номер приезжает в поле telnum.
AD_NUMBER = "9213909268"
MANAGER_NUMBER = "9312534671"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    upsert_manager(c, vats_login="толстов", display_name="Толстов Иван",
                   synergy_user="42", plan_calls=None, active=1,
                   dept="продажи", is_demo=0)
    # Телефон проставляется отдельно: его приносит сверка с Synergy, а не
    # upsert_manager. Так же это устроено и в жизни.
    c.execute("UPDATE managers SET phone = ? WHERE vats_login = 'толстов'", (MANAGER_NUMBER,))
    return c


def test_прямой_номер_менеджера_это_наш_сценарий(conn):
    assert sales_number(conn, MANAGER_NUMBER) is True
    assert sales_number(conn, "+7 (931) 253-46-71") is True


def test_рекламный_номер_не_наш_сценарий(conn):
    """Главная проверка: за рекламный номер не должен заступаться telnum.

    Раньше функция принимала оба номера и соглашалась на любое совпадение,
    поэтому «Авито СПБ» проходил за счёт прямого номера Толстова.
    """
    assert sales_number(conn, AD_NUMBER) is False


def test_пустой_номер_не_наш_сценарий(conn):
    assert sales_number(conn, "") is False
    assert sales_number(conn, "—") is False


def test_номер_чужого_отдела_не_наш_сценарий(conn):
    upsert_manager(conn, vats_login="ткачевин", display_name="Ткачевин Михаил",
                   synergy_user="7", plan_calls=None, active=1,
                   dept="прозвон", is_demo=0)
    conn.execute("UPDATE managers SET phone = '9214401135' WHERE vats_login = 'ткачевин'")
    # Прозвон звонит сам, входящие к нему сценария «потерянная заявка» не
    # касаются: там другой отдел и другая задача.
    assert sales_number(conn, "9214401135") is False


def test_звонок_с_рекламы_не_разбирается_как_личный(conn):
    """Воспроизведение боевого случая от 21.09.2026.

    Клиент позвонил на «Авито СПБ», ВАТС перевела на Толстова. В вебхуке
    `diversion` — рекламный номер, `telnum` — прямой номер Толстова. Прежний
    код соглашался на совпадение любого из двух, `telnum` совпадал, и звонок
    уезжал в поиск потерянных заявок. Заявка 733208 родилась именно так.

    `settings` здесь не нужен: отказ случается раньше, чем до него доходит
    очередь. Если когда-нибудь порядок проверок изменится, тест упадёт на
    None — и это правильный сигнал, а не помеха.
    """
    call = {"uid": "M38MAJFPJG00004B", "client": "79697110282",
            "diversion": AD_NUMBER, "telnum": MANAGER_NUMBER,
            "user": "толстов", "duration": 57, "start": "2026-09-21T06:43:05Z"}
    result = process(conn, None, call, b"")
    assert "skipped" in result, f"звонок с рекламы приняли за личный: {result}"
    assert AD_NUMBER in result["skipped"]


def test_личный_звонок_менеджеру_разбирается(conn):
    """Обратная сторона: сузив отбор, нельзя потерять настоящий сценарий.

    Клиент набрал прямой номер менеджера — это и есть тот случай, ради
    которого весь поиск потерянных заявок затевался. Дальше звонок уходит в
    работу, и отказа по номеру быть не должно.
    """
    call = {"uid": "TEST-1", "client": "79697110282",
            "diversion": MANAGER_NUMBER, "telnum": MANAGER_NUMBER,
            "user": "толстов", "duration": 5, "start": "2026-09-21T06:43:05Z"}
    result = process(conn, _ShortCallSettings(), call, b"")
    # Пять секунд короче порога — значит, по номеру звонок прошёл.
    assert "короче порога" in result.get("skipped", ""), result


class _ShortCallSettings:
    """Ровно то, что читает `process` до проверки длительности."""

    inbound_min_duration_sec = 40
