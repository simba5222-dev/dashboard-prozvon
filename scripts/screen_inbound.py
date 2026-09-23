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

from app import analyzer, search_calls  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_schema, save_screen  # noqa: E402

logger = logging.getLogger("screen")


# Начало записи вырезает сам сервис распознавания: ему передаётся файл целиком
# и число секунд. Раньше резали здесь, по длине в байтах, — и на части записей
# такой кусок не декодировался: «Invalid data found when processing input»,
# звонок молча оставался непросеянным. Так потерялись 8229011, 8231782 и другие.


def main() -> int:
    ap = argparse.ArgumentParser(description="Просев входящих на запрос техники.")
    ap.add_argument("--day", help="дата, ГГГГ-ММ-ДД")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seconds", type=int, default=75,
                    help="сколько секунд начала разговора распознавать")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--uid", action="append", default=[],
                    help="пересеять конкретные звонки (можно повторять)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()

    conn = connect(settings.db_path)
    # Кто из контактов бывал заказчиком — список собирает classify_contacts.py.
    # Нет файла — просев просто не получит эту часть подсказки.
    buyers = search_calls.contacts_with_orders(
        Path(settings.db_path).parent / "contacts-with-orders.json")
    init_schema(conn)
    records = Path(settings.records_dir)

    if args.uid:
        # Точечный пересев: после правки запроса пересчитать конкретные находки
        # дешевле, чем весь день. Дата и «заявки ещё нет» здесь не проверяются —
        # раз звонок назван явно, значит его и надо пересеять.
        placeholders = ",".join("?" * len(args.uid))
        rows = conn.execute(
            f"""
            SELECT k.uid, k.duration_sec, k.started_at, k.vats_login,
                   c.contact_name, c.active_names
            FROM calls k
            LEFT JOIN inbound_checks c ON c.call_uid = k.uid
            WHERE k.uid IN ({placeholders})
            ORDER BY k.started_at DESC
            """,
            args.uid,
        ).fetchall()
    else:
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
            audio = (records / f"{uid}.mp3").read_bytes()
            if not audio:
                raise ValueError("запись пустая")
            response = httpx.post(
                f"{settings.asr_url.rstrip('/')}/transcribe",
                files={"file": (f"{uid}.mp3", audio, "audio/mpeg")},
                # Делим стерео на дорожки: без ролей не отличить «клиент
                # просит технику» от «наш менеджер ищет её у подрядчика».
                data={"mode": "split", "seconds": str(args.seconds)},
                timeout=settings.asr_timeout_sec,
            )
            response.raise_for_status()
            text = analyzer.dialog_text(response.json().get("dialog") or [])
            # Роли по каналам у входящих ненадёжны: в одной записи «оператор» —
            # наш менеджер, в другой — позвонивший. Проверено на записях.
            # Поэтому стороны обезличиваем, а кто есть кто, решает разбор.
            text = text.replace("operator:", "сторона A:").replace("client:", "сторона B:")
            verdict = analyzer.screen_call(
                text, row["active_names"] or "",
                api_key=settings.openai_api_key, model=settings.analysis_model,
                own_company=settings.own_company,
                known=search_calls.number_profile(conn, row["client_phone"], buyers),
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
