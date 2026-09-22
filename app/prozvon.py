"""Разбор разговора прозвона — один разговор, один вызов.

Раньше этот код жил внутри `scripts/analyze_calls.py` и работал только
пачками: записи качал таймер, разбор запускали руками. 18–22.09.2026 отчёт
из-за этого простоял четыре дня — запускать забыли, и никто не заметил.

Теперь то же самое умеет вызываться сразу после разговора, из приёмника
звонков. Пакетный скрипт остался для хвоста за прошлые даты и зовёт эту же
функцию: разойтись они не могут.

**Важная оговорка про «упущено».** Разбор сверяет разговор с карточкой
клиента в CRM: что менеджер записал, какие задачи и заявки завёл. Сразу после
звонка карточка ещё пуста — менеджер заполняет её потом. Поэтому при разборе
по горячим следам «упущено» считается по тому, что есть на тот момент, и
уточняется позже, когда придёт проверка карточки. Расшифровка и содержание
разговора от этого не зависят и верны сразу.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app import analyzer
from app.config import Settings
from app.db import save_transcript

logger = logging.getLogger(__name__)


def transcribe_full(path: Path, asr_url: str, timeout_sec: float) -> dict[str, Any]:
    """Отдать запись сервису распознавания и получить диалог по ролям."""
    with path.open("rb") as handle:
        response = httpx.post(
            f"{asr_url.rstrip('/')}/transcribe",
            files={"file": (path.name, handle, "audio/mpeg")},
            data={"mode": "split"},
            timeout=timeout_sec,
        )
    response.raise_for_status()
    return response.json()


def card_of(conn: sqlite3.Connection, uid: str) -> dict[str, Any]:
    """Карточка звонка: что менеджер внёс, какие задачи и заявки завёл."""
    row = conn.execute(
        """
        SELECT k.uid, k.client_phone, k.duration_sec, k.local_date,
               c.need_value, c.company_name, c.contact_name,
               c.objects_filled, c.inn_filled, c.task_created
        FROM calls k LEFT JOIN card_checks c ON c.call_uid = k.uid
        WHERE k.uid = ?
        """,
        (uid,),
    ).fetchone()
    call = dict(row) if row else {"uid": uid}
    call["tasks"] = [dict(r) for r in conn.execute(
        "SELECT * FROM call_tasks WHERE call_uid = ?", (uid,))]
    call["orders"] = [dict(r) for r in conn.execute(
        "SELECT * FROM call_orders WHERE call_uid = ?", (uid,))]
    return call


def analyze_call(conn: sqlite3.Connection, settings: Settings, uid: str,
                 path: Path, *, text: str | None = None) -> dict[str, Any]:
    """Расшифровать и разобрать один разговор. Возвращает, чем дело кончилось.

    Расшифровку сохраняем **до** разбора: если разбор упадёт или кончатся
    деньги у модели, текст разговора всё равно останется. Слушать запись
    заново ради того, что уже распознано, — расточительство.
    """
    now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731
    if not text:
        result = transcribe_full(path, settings.asr_url, settings.asr_timeout_sec)
        text = analyzer.dialog_text(result.get("dialog") or [])
        save_transcript(conn, call_uid=uid, text=text, analysis_json=None,
                        created_at=now(), is_demo=0)
        conn.commit()
    if not text.strip():
        logger.info("разговор %s: в записи нет речи", uid)
        return {"uid": uid, "transcribed": True, "analyzed": False,
                "note": "в записи нет речи"}
    if not settings.analysis_configured:
        return {"uid": uid, "transcribed": True, "analyzed": False,
                "note": "ключ OpenAI не задан"}

    analysis = analyzer.analyze(
        text, card_of(conn, uid), api_key=settings.openai_api_key,
        model=settings.analysis_model, own_company=settings.own_company,
    )
    save_transcript(conn, call_uid=uid, text=text,
                    analysis_json=json.dumps(analysis, ensure_ascii=False),
                    created_at=now(), is_demo=0)
    conn.commit()
    missed = ", ".join(m["field"] for m in analysis.get("missed") or [])
    if missed:
        logger.info("разговор %s: не попало в карточку — %s", uid, missed)
    return {"uid": uid, "transcribed": True, "analyzed": True, "missed": missed}
