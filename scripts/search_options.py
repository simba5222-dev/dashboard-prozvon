#!/usr/bin/env python
"""Варианты подбора — в ленту заявки.

Менеджер отдал заявку на «Нужен Подбор», подборщик обзванивает поставщиков,
а результат остаётся у него в голове и в карточках транспорта. По заявке
вариантов не видно ни менеджеру, ни владельцу — с этим и разбираемся.

Берём разобранные разговоры подборщика после передачи заявки, отбираем те,
что относятся к её технике, и пишем в ленту заявки по строке на звонок:
кому звонил, что ответили, есть ли техника.

    ./scripts/search_options.py               показать, ничего не записывая
    ./scripts/search_options.py --apply       написать в CRM
    ./scripts/search_options.py --days 3      заявки за последние дни

**Связь заявки и звонка приблизительная** — по типу техники и времени.
Точной в данных нет: карточка транспорта связана с поставщиком, а не с
заявкой. Поэтому в каждом комментарии стоит пометка «связь по времени»:
догадка должна быть видна, а не выдаваться за факт.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.crm_write import CrmWriter  # noqa: E402
from app.db import connect, init_schema  # noqa: E402

logger = logging.getLogger("подбор")
MSK = timezone(timedelta(hours=3))
ПОЛЕ_ЗВОНКОВ = "custom-29790"      # «Звонков на поиск» у заявки


def момент(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def строка(row, оценка: dict) -> str:
    """Одна строка варианта: кто, что ответил, чем подтверждено."""
    когда = (row["started_at"] or "")[11:16]
    кто = row["contact_name"] or row["client_phone"] or "без имени"
    если_нет = int(row["duration_sec"] or 0) == 0
    if если_нет:
        return f"{когда} · {кто} — не дозвонился"
    предложено = [i["type"] for i in оценка.get("offered") or []]
    if предложено:
        цитата = (оценка["offered"][0].get("quote") or "").strip()
        хвост = f" — «{цитата}»" if цитата else ""
        return f"{когда} · {кто} — есть: {', '.join(предложено)}{хвост}"
    if оценка.get("more_unnamed"):
        return f"{когда} · {кто} — говорит, есть ещё техника: «{оценка['more_unnamed']}»"
    return f"{когда} · {кто} — поговорили, техника не прозвучала"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="Заявки, отданные за столько дней.")
    ap.add_argument("--apply", action="store_true", help="Писать в CRM.")
    ap.add_argument("--order", help="Только эта заявка.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token,
                           min_interval_sec=settings.synergy_min_interval_sec,
                           retries=settings.synergy_retries)
    writer = CrmWriter(client, apply=args.apply)

    граница = (datetime.now(MSK) - timedelta(days=args.days)).isoformat()
    условие = "AND order_id = ?" if args.order else "AND entered_at >= ?"
    заявки = conn.execute(
        f"SELECT * FROM search_tasks WHERE is_demo = 0 {условие} ORDER BY entered_at",
        (args.order or граница,)).fetchall()
    print(f"заявок в подборе: {len(заявки)}")

    написано = 0
    for заявка in заявки:
        техника = (заявка["equipment"] or "").strip()
        отдали = момент(заявка["entered_at"])
        if not техника or not отдали:
            print(f"  заявка {заявка['order_id']}: тип техники не указан — сопоставить не с чем")
            continue
        # Звонки подборщика после передачи заявки. Отбор по технике: либо
        # поставщик назвал её в разговоре, либо она есть в его карточках.
        звонки = conn.execute("""
            SELECT k.uid, k.started_at, k.duration_sec, k.client_phone,
                   s.contact_name, s.offered, s.known, s.more_unnamed
              FROM calls k JOIN search_checks s ON s.call_uid = k.uid
             WHERE k.direction = 'out' AND k.started_at >= ?
             ORDER BY k.started_at""", (заявка["entered_at"],)).fetchall()
        подходят = []
        for row in звонки:
            оценка = {"offered": json.loads(row["offered"] or "[]"),
                      "known": json.loads(row["known"] or "[]"),
                      "more_unnamed": row["more_unnamed"] or ""}
            назвал = any(i["type"] == техника for i in оценка["offered"])
            в_карточках = техника in оценка["known"]
            if назвал or в_карточках:
                подходят.append((row, оценка))
        if not подходят:
            print(f"  заявка {заявка['order_id']} ({техника}): подходящих звонков нет")
            continue

        уже = {r[0] for r in conn.execute(
            "SELECT call_uid FROM search_options WHERE order_id = ?", (заявка["order_id"],))}
        новые = [(row, оц) for row, оц in подходят if row["uid"] not in уже]
        print(f"  заявка {заявка['order_id']} ({техника}): вариантов {len(подходят)}, "
              f"новых {len(новые)}")
        # Один комментарий на заявку за прогон, а не по штуке на звонок:
        # шесть подряд превратят ленту заявки в ленту робота, и менеджер
        # перестанет её читать.
        ценные, пустые, недозвон = [], 0, 0
        for row, оценка in новые:
            если_нет = int(row["duration_sec"] or 0) == 0
            есть = (оценка.get("offered") or оценка.get("more_unnamed"))
            if если_нет:
                недозвон += 1
            elif есть:
                ценные.append(строка(row, оценка))
            else:
                пустые += 1
        хвост = []
        if недозвон:
            хвост.append(f"не дозвонился: {недозвон}")
        if пустые:
            хвост.append(f"поговорил без результата: {пустые}")
        for s_ in ценные:
            print("     " + s_)
        if хвост:
            print("     (" + ", ".join(хвост) + ")")
        if not ценные and not хвост:
            continue
        текст = ("<b>Подбор техники</b><br>"
                 + ("<br>".join(ценные) if ценные else "вариантов пока нет")
                 + ("<br>" + ", ".join(хвост) if хвост else "")
                 + "<br><i>собрано из звонков подборщика; связь с заявкой "
                   "по типу техники и времени</i>")
        if not args.apply:
            continue
        try:
            writer.post_comment(заявка["order_id"], текст)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("комментарий к заявке %s не ушёл: %s", заявка["order_id"], exc)
            continue
        for row, _ in новые:
            conn.execute("INSERT OR REPLACE INTO search_options VALUES (?, ?, ?)",
                         (row["uid"], заявка["order_id"],
                          datetime.now(timezone.utc).isoformat()))
        conn.commit()
        написано += 1
        if args.apply and подходят:
            # Счётчик звонков на поиск — то же число, что видно в ленте.
            writer.update_customs(заявка["order_id"], {ПОЛЕ_ЗВОНКОВ: len(подходят)})

    print(f"\nнаписано комментариев: {написано}" if args.apply
          else "\nэто разбор без записи, для записи: --apply")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
