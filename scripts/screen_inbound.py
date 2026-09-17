#!/usr/bin/env python
"""Просев входящих: в каком разговоре прозвучал запрос на технику.

    sudo -u claude .venv/bin/python scripts/screen_inbound.py --day 2026-09-16
    ... --limit 20          взять только двадцать звонков
    ... --seconds 90        сколько секунд начала разговора распознавать

Полное распознавание всего входящего потока в сутки не помещается, а запрос
на технику звучит в первую минуту: «здравствуйте, нужен экскаватор на завтра».
Поэтому здесь распознаётся только начало разговора и черновым качеством —
этот текст никому не показывается, он нужен только для ответа «запрос или нет».

Те звонки, где просев нашёл запрос, потом уходят в полный разбор:
`analyze_calls.py --uid …`, а по ним заводится заявка.
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
from app.db import connect, init_schema, save_screen  # noqa: E402

logger = logging.getLogger("screen")


def head_of_record(path: Path, seconds: int, duration_sec: int) -> bytes:
    """Начало записи — первые `seconds` секунд.

    Режем по длине файла, а не перекодированием: ВАТС отдаёт mp3 с постоянным
    битрейтом, поэтому байты и секунды пропорциональны, а длительность звонка
    мы и так знаем из CRM. Декодер спокойно переживает обрезанный последний
    кадр, зато не нужен ни ffmpeg (его на сервере нет), ни лишняя зависимость.
    """
    data = path.read_bytes()
    if duration_sec <= seconds or duration_sec <= 0:
        return data
    return data[: max(int(len(data) * seconds / duration_sec), 16384)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Просев входящих на запрос техники.")
    ap.add_argument("--day", help="дата, ГГГГ-ММ-ДД")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seconds", type=int, default=75,
                    help="сколько секунд начала разговора распознавать")
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()

    conn = connect(settings.db_path)
    init_schema(conn)
    records = Path(settings.records_dir)

    condition = "" if args.redo else "AND s.call_uid IS NULL"
    rows = conn.execute(
        f"""
        SELECT k.uid, k.duration_sec, k.started_at, k.vats_login,
               c.contact_name, c.active_names
        FROM calls k
        JOIN inbound_checks c ON c.call_uid = k.uid
        LEFT JOIN screens s ON s.call_uid = k.uid
        WHERE k.local_date BETWEEN ? AND ? AND k.direction = 'in'
          AND c.orders_after = 0 AND c.dismissed = 0 {condition}
        ORDER BY k.started_at DESC
        """,
        (since, until),
    ).fetchall()
    todo = [r for r in rows if (records / f"{r['uid']}.mp3").exists()]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{since}…{until}: к просеву {len(todo)} разговоров "
          f"(без записи пропущено {len(rows) - len(todo)})")

    found = failed = 0
    started = time.monotonic()
    for row in todo:
        uid = row["uid"]
        try:
            audio = head_of_record(records / f"{uid}.mp3", args.seconds, row["duration_sec"])
            if not audio:
                raise ValueError("не удалось нарезать начало записи")
            response = httpx.post(
                f"{settings.asr_url.rstrip('/')}/transcribe",
                files={"file": (f"{uid}.mp3", audio, "audio/mpeg")},
                data={"mode": "mono"}, timeout=settings.asr_timeout_sec,
            )
            response.raise_for_status()
            text = analyzer.dialog_text(response.json().get("dialog") or [])
            verdict = analyzer.screen_call(
                text, row["active_names"] or "",
                api_key=settings.openai_api_key, model=settings.analysis_model,
                own_company=settings.own_company,
            )
            save_screen(
                conn, call_uid=uid, head_text=text,
                verdict_json=json.dumps(verdict, ensure_ascii=False),
                is_request=int(verdict["is_request"] and not verdict["about_existing"]),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            conn.commit()
            if verdict["is_request"]:
                found += 1
                logger.info("%s %s: запрос — %s (%s%%)", row["started_at"][11:16],
                            row["contact_name"] or uid,
                            verdict["request"] or verdict["equipment"], verdict["confidence"])
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("%s: просев не вышел — %s: %s", uid, type(exc).__name__, exc)
            failed += 1

    conn.close()
    print(f"просеяно {len(todo)}, запросов найдено {found}, не вышло {failed}, "
          f"за {(time.monotonic() - started) / 60:.1f} мин")
    return 0


if __name__ == "__main__":
    sys.exit(main())
