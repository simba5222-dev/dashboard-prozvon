#!/usr/bin/env python
"""Распознать скачанные записи и разобрать разговоры.

    sudo -u claude .venv/bin/python scripts/analyze_calls.py --days 14
    ... --limit 20              взять только двадцать записей
    ... --analyze-only          не распознавать заново, только разобрать
    ... --redo                  переразобрать даже там, где разбор уже есть

Запись сначала уходит в сервис распознавания на этом же сервере (он делит
стерео на две дорожки: менеджер и клиент), потом расшифровка и карточка из CRM
уходят в модель, а её ответ ложится в таблицу `transcripts`.

Запускать под `claude`: скрипт пишет в базу дашборда. Записи к этому моменту
должны быть скачаны — этим занимается `fetch_records.py` под `agent`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_schema, save_transcript  # noqa: E402

logger = logging.getLogger("analyze")


def transcribe(path: Path, asr_url: str, timeout_sec: float) -> dict:
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


def card_of(conn, uid: str) -> dict:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Распознавание и разбор разговоров.")
    ap.add_argument("--days", type=int, default=1, help="за сколько последних дней")
    ap.add_argument("--day", help="по какую дату, ГГГГ-ММ-ДД")
    ap.add_argument("--limit", type=int, default=0, help="не больше стольких разговоров")
    ap.add_argument("--analyze-only", action="store_true",
                    help="не распознавать, только разобрать готовые расшифровки")
    ap.add_argument("--redo", action="store_true", help="переразобрать уже разобранные")
    ap.add_argument("--no-analysis", action="store_true", help="только расшифровка")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()

    conn = connect(settings.db_path)
    init_schema(conn)
    records = Path(settings.records_dir)

    rows = conn.execute(
        """
        SELECT k.uid, k.duration_sec, k.local_date, k.started_at,
               t.text AS transcript_text, t.analysis_json
        FROM calls k LEFT JOIN transcripts t ON t.call_uid = k.uid
        WHERE k.local_date BETWEEN ? AND ? AND k.direction = 'out'
          AND k.duration_sec >= ?
        ORDER BY k.started_at DESC
        """,
        (since, until, settings.talk_threshold_sec),
    ).fetchall()

    todo = []
    for row in rows:
        has_text = bool(row["transcript_text"])
        has_analysis = bool(row["analysis_json"])
        if has_analysis and not args.redo:
            continue
        if args.analyze_only and not has_text:
            continue
        if not has_text and not (records / f"{row['uid']}.mp3").exists():
            continue
        todo.append(row)
    if args.limit:
        todo = todo[: args.limit]

    analysis_on = settings.analysis_configured and not args.no_analysis
    if not analysis_on and not args.no_analysis:
        logger.warning("ключ OpenAI не задан — будет только расшифровка")
    print(f"{since}…{until}: к обработке {len(todo)} разговоров")

    done = analyzed = failed = 0
    started = time.monotonic()
    for row in todo:
        uid = row["uid"]
        text = row["transcript_text"]
        try:
            if not text:
                result = transcribe(records / f"{uid}.mp3", settings.asr_url,
                                    settings.asr_timeout_sec)
                text = analyzer.dialog_text(result.get("dialog") or [])
                if not text.strip():
                    logger.info("%s: в записи нет речи", uid)
                save_transcript(conn, call_uid=uid, text=text, analysis_json=None,
                                created_at=datetime.now(timezone.utc).isoformat(), is_demo=0)
                conn.commit()
                done += 1

            if analysis_on and text.strip():
                analysis = analyzer.analyze(
                    text, card_of(conn, uid),
                    api_key=settings.openai_api_key, model=settings.analysis_model,
                    own_company=settings.own_company,
                )
                save_transcript(
                    conn, call_uid=uid, text=text,
                    analysis_json=json.dumps(analysis, ensure_ascii=False),
                    created_at=datetime.now(timezone.utc).isoformat(), is_demo=0,
                )
                conn.commit()
                analyzed += 1
                if analysis["missed"]:
                    fields = ", ".join(m["field"] for m in analysis["missed"])
                    logger.info("%s: не попало в карточку — %s", uid, fields)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("%s: не обработан — %s: %s", uid, type(exc).__name__, exc)
            failed += 1
            continue

        if (done + analyzed) % 10 == 0:
            spent = time.monotonic() - started
            logger.info("обработано %s из %s, %.0f с", done or analyzed, len(todo), spent)

    conn.close()
    print(f"расшифровано {done}, разобрано {analyzed}, не вышло {failed}, "
          f"за {(time.monotonic() - started) / 60:.1f} мин")
    return 0


if __name__ == "__main__":
    sys.exit(main())
