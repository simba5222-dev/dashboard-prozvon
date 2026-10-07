"""Поток 3: выбор головы для разбора звонка — настройка, а не правка кода.

07.10.2026: OpenAI стоял без денег на счету, и разбор прозвона вставал молча —
расшифровка есть, оценки и рекомендаций РОПу нет. Переведено на Яндекс
отдельной настройкой `prozvon_engine`, независимой от screen_engine потока 2.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import analyzer, prozvon
from app.db import init_schema


class _Settings:
    """Ровно то, что читает analyze_call, без похода в настоящий .env."""

    def __init__(self, *, prozvon_engine="openai", yandex_api_key="", yandex_folder="",
                 openai_api_key=""):
        self.prozvon_engine = prozvon_engine
        self.yandex_api_key = yandex_api_key
        self.yandex_folder = yandex_folder
        self.yandex_model = "yandexgpt-lite"
        self.openai_api_key = openai_api_key
        self.analysis_model = "gpt-4o"
        self.own_company = "Техно-Ресурс"
        self.asr_url = "http://127.0.0.1:8080"
        self.asr_timeout_sec = 60.0

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
    return c


def test_yandex_engine_uses_yandex_key_and_model(conn, monkeypatch):
    seen = {}

    def fake_спросить(prompt, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(analyzer, "_спросить_голову", fake_спросить)
    settings = _Settings(prozvon_engine="yandex", yandex_api_key="yk", yandex_folder="f1")
    result = prozvon.analyze_call(conn, settings, "u1", Path("/dev/null"),
                                  text="клиент: нужен экскаватор")
    assert result["analyzed"] is True
    assert seen["engine"] == "yandex"
    assert seen["api_key"] == "yk"
    assert seen["model"] == "yandexgpt-lite"
    assert seen["folder"] == "f1"


def test_openai_engine_unchanged_by_default(conn, monkeypatch):
    seen = {}

    def fake_спросить(prompt, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(analyzer, "_спросить_голову", fake_спросить)
    settings = _Settings(prozvon_engine="openai", openai_api_key="ok")
    result = prozvon.analyze_call(conn, settings, "u2", Path("/dev/null"),
                                  text="клиент: нужен экскаватор")
    assert result["analyzed"] is True
    assert seen["engine"] == "openai"
    assert seen["api_key"] == "ok"
    assert seen["model"] == "gpt-4o"


def test_yandex_engine_without_key_skips_without_calling_model(conn, monkeypatch):
    called = []
    monkeypatch.setattr(analyzer, "_спросить_голову", lambda *a, **k: called.append(1))
    settings = _Settings(prozvon_engine="yandex", yandex_api_key="", yandex_folder="")
    result = prozvon.analyze_call(conn, settings, "u3", Path("/dev/null"),
                                  text="клиент: нужен экскаватор")
    assert result == {"uid": "u3", "transcribed": True, "analyzed": False,
                      "note": "ключ Яндекса не задан"}
    assert not called


def test_yandex_setting_does_not_fall_back_to_openai_key(conn, monkeypatch):
    """Переключили на yandex — значит не слать запрос в OpenAI вовсе."""
    monkeypatch.setattr(analyzer, "_спросить_голову",
                        lambda *a, **k: pytest.fail("не должны были звать модель"))
    settings = _Settings(prozvon_engine="yandex", yandex_api_key="", yandex_folder="",
                        openai_api_key="ok-still-set")
    result = prozvon.analyze_call(conn, settings, "u4", Path("/dev/null"),
                                  text="клиент: нужен экскаватор")
    assert result["note"] == "ключ Яндекса не задан"
