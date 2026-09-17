#!/usr/bin/env python
"""Завести заявки по звонкам, где просев нашёл запрос на технику.

    sudo -u claude .venv/bin/python scripts/catch_leads.py --day 2026-09-16
    ... --apply            действительно создавать заявки (без него — сухой прогон)
    ... --limit 1          начать с одной

Схема та же, что у звонков на общие номера компании: запись распознаётся
целиком, расшифровка уходит в разбор (ручка `/analyze` соседнего сервиса — она
же обслуживает боевой поток), и результат ложится в заявку: тип техники, адрес
объекта, расшифровка, рекомендации менеджеру и РОПу, оценка звонка. Сверху —
комментарий с цитатой из разговора и временем звонка.

**Заявка создаётся только если её нет.** Перед записью заново спрашиваем CRM,
не появилась ли заявка по этому контакту после звонка: менеджер мог оформить
её сам, пока мы считали. Повторный запуск дубля не делает.
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
from app.collector import SynergyClient, contact_orders_around, load_stages  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crm_write import CrmWriter, lead_comment, lead_summary, order_customs  # noqa: E402
from app.db import connect, init_schema, save_transcript  # noqa: E402

logger = logging.getLogger("leads")


def transcribe_full(path: Path, settings) -> str:
    """Распознать запись целиком, с делением на менеджера и клиента."""
    with path.open("rb") as handle:
        response = httpx.post(
            f"{settings.asr_url.rstrip('/')}/transcribe",
            files={"file": (path.name, handle, "audio/mpeg")},
            data={"mode": "split"}, timeout=settings.asr_timeout_sec,
        )
    response.raise_for_status()
    return analyzer.dialog_text(response.json().get("dialog") or [])


def analyze_like_production(transcript: str, settings) -> dict:
    """Разбор той же ручкой, что обслуживает звонки с общих номеров.

    Так поля заявки заполняются одинаково независимо от того, пришёл звонок на
    общий номер или напрямую менеджеру. Своего второго промпта здесь нет
    намеренно: две копии разъедутся в первый же месяц.
    """
    response = httpx.post(
        f"{settings.asr_url.rstrip('/')}/analyze",
        json={"transcript": transcript},
        headers={"X-Analysis-Token": settings.asr_analysis_token or ""},
        timeout=180.0,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    ap = argparse.ArgumentParser(description="Заявки по пойманным звонкам.")
    ap.add_argument("--day", help="дата, ГГГГ-ММ-ДД")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true",
                    help="создавать заявки в CRM; без него только показать")
    ap.add_argument("--fix", action="store_true",
                    help="не создавать новые, а дозаполнить уже заведённые: "
                         "выжимка, соисполнитель, ответственный")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()

    conn = connect(settings.db_path)
    init_schema(conn)
    records = Path(settings.records_dir)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token, timeout_sec=30.0,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries,
    )
    writer = CrmWriter(client, apply=args.apply)
    stages = load_stages(client)
    stage_id = next((sid for sid, (name, _kind) in stages.items()
                     if name.strip().lower() == settings.crm_lead_stage.lower()), None)
    if stage_id is None:
        logger.warning("стадия «%s» в CRM не найдена — заявка ляжет в стадию по умолчанию",
                       settings.crm_lead_stage)

    have_order = "IS NOT NULL" if args.fix else "IS NULL"
    rows = conn.execute(
        f"""
        SELECT k.uid, k.started_at, k.duration_sec, k.client_phone, k.vats_login,
               m.display_name, c.contact_id, c.contact_name, c.company_name,
               s.verdict_json, s.created_order_id, t.text AS transcript_text
        FROM screens s
        JOIN calls k ON k.uid = s.call_uid
        JOIN inbound_checks c ON c.call_uid = s.call_uid
        LEFT JOIN managers m ON m.vats_login = k.vats_login
        LEFT JOIN transcripts t ON t.call_uid = s.call_uid
        WHERE s.is_request = 1 AND s.created_order_id {have_order}
          AND c.contact_id IS NOT NULL AND c.dismissed = 0
          AND k.local_date BETWEEN ? AND ?
        ORDER BY k.started_at
        """,
        (since, until),
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"{since}…{until}: заявок к заведению {len(rows)}"
          + ("" if args.apply else " (сухой прогон, ничего не создаётся)"))

    made = skipped = failed = 0
    started = time.monotonic()
    for row in rows:
        uid = row["uid"]
        screen = json.loads(row["verdict_json"] or "{}")
        if args.fix:
            # Дозаполнение уже заведённой заявки: разбор считаем заново по
            # сохранённой расшифровке — распознавать второй раз не нужно.
            try:
                order_id = row["created_order_id"]
                analysis = analyze_like_production(row["transcript_text"] or "", settings)
                writer.set_summary(order_id, lead_summary(dict(row), screen, analysis))
                performer = conn.execute(
                    "SELECT synergy_user FROM managers WHERE vats_login = ?",
                    (row["vats_login"],),
                ).fetchone()
                if performer and performer["synergy_user"]:
                    writer.add_performer(order_id, performer["synergy_user"])
                writer.set_responsible(order_id, settings.crm_lead_responsible)
                made += 1
                print(f"  заявка {order_id}: выжимка, соисполнитель и ответственный поставлены")
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("%s: дозаполнить не вышло — %s", row["created_order_id"], exc)
                failed += 1
            continue

        try:
            # Пока мы считали, менеджер мог завести заявку сам — проверяем заново.
            after, _active = contact_orders_around(
                client, row["contact_id"], row["started_at"], stages)
            if after:
                logger.info("%s: заявка уже появилась (%s) — пропускаем", uid, "; ".join(after[:2]))
                conn.execute(
                    "UPDATE inbound_checks SET orders_after = ?, order_names = ? WHERE call_uid = ?",
                    (len(after), "; ".join(after), uid),
                )
                conn.commit()
                skipped += 1
                continue

            transcript = row["transcript_text"]
            if not transcript:
                path = records / f"{uid}.mp3"
                if not path.exists():
                    logger.warning("%s: записи нет на диске", uid)
                    failed += 1
                    continue
                transcript = transcribe_full(path, settings)
                save_transcript(conn, call_uid=uid, text=transcript, analysis_json=None,
                                created_at=datetime.now(timezone.utc).isoformat(), is_demo=0)
                conn.commit()

            analysis = analyze_like_production(transcript, settings)
            comment = lead_comment(dict(row), screen, analysis)
            summary = lead_summary(dict(row), screen, analysis)
            order_id = writer.create_order(
                contact_id=row["contact_id"],
                name=settings.crm_lead_order_name,
                stage_id=stage_id,
                responsible_id=settings.crm_lead_responsible,
                customs=order_customs(analysis, transcript, summary),
                comment=comment,
            )
            if order_id:
                writer.post_comment(order_id, comment)
                # Соисполнитель — тот, кто говорил с клиентом: заявка не его,
                # но продолжать разговор всё равно ему.
                performer = conn.execute(
                    "SELECT synergy_user FROM managers WHERE vats_login = ?",
                    (row["vats_login"],),
                ).fetchone()
                if performer and performer["synergy_user"]:
                    writer.add_performer(order_id, performer["synergy_user"])
                # Ответственного ставим последним и с проверкой: правка полей
                # умеет возвращать владельца контакта обратно.
                writer.set_responsible(order_id, settings.crm_lead_responsible)
                conn.execute("UPDATE screens SET created_order_id = ? WHERE call_uid = ?",
                             (order_id, uid))
                conn.commit()
            made += 1
            print(f"  {row['started_at'][11:16]} {row['contact_name'] or row['client_phone']}"
                  f" — {screen.get('request') or screen.get('equipment') or 'запрос'}"
                  + (f" → заявка {order_id}" if order_id else " (сухой прогон)"))
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("%s: не вышло — %s: %s", uid, type(exc).__name__, exc)
            failed += 1

    conn.close()
    print(f"заведено {made}, пропущено (заявка уже есть) {skipped}, не вышло {failed}, "
          f"за {(time.monotonic() - started) / 60:.1f} мин")
    return 0


if __name__ == "__main__":
    sys.exit(main())
