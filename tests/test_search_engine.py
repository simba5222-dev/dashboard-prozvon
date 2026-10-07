"""Поток 4: выбор головы для сверки услышанной техники — настройка, не код.

07.10.2026: расшифровка здесь уже на Яндексе с 29.09 (через общий сервис),
но последний шаг — heard_types() — был жёстко на OpenAI и падал молча при
недоступности. Переведено на Яндекс отдельной настройкой `search_engine`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import analyzer, search_calls
from app.db import init_schema, save_call, save_transport_card


class _Settings:
    def __init__(self, *, search_engine="openai", yandex_api_key="", yandex_folder="",
                 openai_api_key=""):
        self.search_engine = search_engine
        self.yandex_api_key = yandex_api_key
        self.yandex_folder = yandex_folder
        self.yandex_model = "yandexgpt-lite"
        self.openai_api_key = openai_api_key
        self.analysis_model = "gpt-4o"
        self.asr_url = "http://127.0.0.1:8080"
        self.asr_timeout_sec = 60.0
        self.search_voice_model = ""

    @property
    def analysis_configured(self):
        return bool(self.openai_api_key)

    @property
    def yandex_stt_configured(self):
        return bool(self.yandex_api_key and self.yandex_folder)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    save_call(c, uid="s1", vats_login="andy", client_phone="79101234567",
             direction="out", status="success", started_at="2026-10-07T10:00:00Z",
             local_date="2026-10-07", local_hour=10, wait_sec=3, duration_sec=60,
             record_url=None, in_group=0, is_demo=0, fetched_at="2026-10-07T10:00:00Z")
    # vocabulary() и allowed должны быть непустыми, иначе heard_types выходит
    # раньше диспетчера, и тест ничего не проверит.
    save_transport_card(c, id="1", name="Самосвал 25т", type_id="t1",
                        type_name="Самосвал", phone10="9101234567", contact_id="c1",
                        contact_name="Поставщик", status="", updated_at="2026-10-07",
                        synced_at="2026-10-07", call_link=None)
    return c


def test_yandex_engine_uses_yandex_key_and_model(conn, monkeypatch):
    seen = {}

    def fake(prompt, **kwargs):
        seen.update(kwargs)
        return {"asked": "", "has": [], "sure": 0, "presented": "", "clarified": ""}

    monkeypatch.setattr(analyzer, "_спросить_голову", fake)
    settings = _Settings(search_engine="yandex", yandex_api_key="yk", yandex_folder="f1")
    result = search_calls.check_call(conn, settings, "s1", Path("/dev/null"),
                                     text="поставщик: у меня есть самосвал")
    assert result["checked"] is True
    assert seen["engine"] == "yandex"
    assert seen["api_key"] == "yk"
    assert seen["model"] == "yandexgpt-lite"
    assert seen["folder"] == "f1"


def test_openai_engine_unchanged_by_default(conn, monkeypatch):
    seen = {}

    def fake(prompt, **kwargs):
        seen.update(kwargs)
        return {"asked": "", "has": [], "sure": 0, "presented": "", "clarified": ""}

    monkeypatch.setattr(analyzer, "_спросить_голову", fake)
    settings = _Settings(search_engine="openai", openai_api_key="ok")
    result = search_calls.check_call(conn, settings, "s1", Path("/dev/null"),
                                     text="поставщик: у меня есть самосвал")
    assert result["checked"] is True
    assert seen["engine"] == "openai"
    assert seen["api_key"] == "ok"


def test_yandex_engine_without_key_skips_without_calling_model(conn, monkeypatch):
    called = []
    monkeypatch.setattr(analyzer, "_спросить_голову", lambda *a, **k: called.append(1))
    settings = _Settings(search_engine="yandex", yandex_api_key="", yandex_folder="",
                        openai_api_key="still-here")
    result = search_calls.check_call(conn, settings, "s1", Path("/dev/null"),
                                     text="поставщик: у меня есть самосвал")
    assert result == {"uid": "s1", "transcribed": True, "checked": False,
                      "note": "ключ Яндекса не задан"}
    assert not called
