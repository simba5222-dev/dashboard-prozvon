"""Разбор заявок (проваленные/живые): выбор головы через общий диспетчер.

07.10.2026: OpenAI решено больше не трогать вовсе — переведено на Яндекс.
Это последнее место в дашборде, которое звало OpenAI напрямую, без выбора
движка: analyze_order/analyze_live_order работают на dashboard-order-analysis
(06:30 МСК ежедневно) и были пропущены при переводе потоков 2/3/4.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer


ORDER = {"order_id": "1", "name": "Экскаватор", "stage_name": "Новый"}
CALLS = [{"vats_login": "andy", "direction": "out", "duration_sec": 60,
         "started_at": "2026-10-07T10:00:00Z", "transcript_text": "нужен экскаватор"}]


def test_analyze_order_uses_yandex_when_asked(monkeypatch):
    seen = {}

    def fake(prompt, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(analyzer, "_спросить_голову", fake)
    analyzer.analyze_order(ORDER, CALLS, api_key="yk", model="yandexgpt-lite",
                           engine="yandex", folder="f1")
    assert seen["engine"] == "yandex"
    assert seen["api_key"] == "yk"
    assert seen["model"] == "yandexgpt-lite"
    assert seen["folder"] == "f1"


def test_analyze_order_defaults_stay_openai(monkeypatch):
    seen = {}

    def fake(prompt, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(analyzer, "_спросить_голову", fake)
    analyzer.analyze_order(ORDER, CALLS, api_key="ok", model="gpt-4o")
    assert seen["engine"] == "openai"
    assert seen["api_key"] == "ok"


def test_analyze_live_order_uses_yandex_when_asked(monkeypatch):
    seen = {}

    def fake(prompt, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(analyzer, "_спросить_голову", fake)
    analyzer.analyze_live_order(ORDER, CALLS, api_key="yk", model="yandexgpt-lite",
                                engine="yandex", folder="f1", silence_days=2)
    assert seen["engine"] == "yandex"
    assert seen["api_key"] == "yk"
    assert seen["folder"] == "f1"


def test_analyze_order_handles_no_answer(monkeypatch):
    """Если голова не ответила (или вернула не JSON) — честный EMPTY_VERDICT,
    не падение."""
    monkeypatch.setattr(analyzer, "_спросить_голову", lambda *a, **k: None)
    out = analyzer.analyze_order(ORDER, CALLS, api_key="ok", model="gpt-4o")
    assert out["no_calls"] is False
    assert out == {**analyzer.EMPTY_VERDICT, "no_calls": False}
