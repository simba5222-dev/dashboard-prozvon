#!/usr/bin/env python
"""Разобрать заявки: почему переданная заявка не дошла до сделки.

    sudo -u claude .venv/bin/python scripts/analyze_orders.py --order 732823
    ... --lost --days 30          все проваленные заявки за месяц
    ... --open                    и те, что висят в работе
    ... --collect-only            только собрать звонки, без разбора

Менеджер прозвона заводит заявку и передаёт её ответственному. Дальше клиенту
звонит уже он — а его звонки дашборд не собирает, он следит за прозвоном.
Поэтому здесь звонки клиента добираются отдельно: по телефонам контакта за
период от создания заявки.

Записи этих звонков скачиваются тем же `fetch_records.py` под `agent` —
запустите его между сбором и разбором, иначе разбирать будет нечего:

    sudo -u claude .venv/bin/python scripts/analyze_orders.py --lost --collect-only
    ./scripts/fetch_records.py --days 30 --min-sec 15
    sudo -u claude .venv/bin/python scripts/analyze_calls.py --days 30 --min-sec 15
    sudo -u claude .venv/bin/python scripts/analyze_orders.py --lost
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.collector import SynergyClient, collect_calls_for_phones, contact_phones  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_schema, save_order_report  # noqa: E402

logger = logging.getLogger("orders")


def pick_orders(conn, args) -> list[dict]:
    """Какие заявки разбирать: одну названную или все подходящие за период."""
    if args.order:
        rows = conn.execute(
            "SELECT * FROM call_orders WHERE order_id = ? LIMIT 1", (args.order,)
        ).fetchall()
        return [dict(r) for r in rows]

    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()
    kinds = []
    if args.lost:
        kinds.append("lost")
    if args.open:
        kinds.extend(["opened", ""])
    marks = ",".join("?" * len(kinds)) if kinds else "''"
    rows = conn.execute(
        f"""
        SELECT o.*, k.local_date FROM call_orders o
        JOIN calls k ON k.uid = o.call_uid
        WHERE k.local_date BETWEEN ? AND ?
          AND o.stage_kind IN ({marks})
        GROUP BY o.order_id
        ORDER BY o.created_at DESC
        """,
        (since, until, *kinds),
    ).fetchall()
    return [dict(r) for r in rows]


def calls_with_client(conn, phones: set[str], after_iso: str) -> list[dict]:
    """Разговоры с этим клиентом после создания заявки — чьи угодно.

    Сверяем телефоны на своей стороне: в базе они лежат в том виде, в каком их
    отдала CRM, а сравнивать надо последние десять цифр.
    """
    rows = conn.execute(
        """
        SELECT k.uid, k.started_at, k.direction, k.duration_sec, k.vats_login,
               k.in_group, k.client_phone, t.text AS transcript_text
        FROM calls k LEFT JOIN transcripts t ON t.call_uid = k.uid
        WHERE k.started_at > ? ORDER BY k.started_at
        """,
        (after_iso,),
    ).fetchall()
    out = []
    for row in rows:
        digits = "".join(ch for ch in (row["client_phone"] or "") if ch.isdigit())
        if len(digits) >= 10 and digits[-10:] in phones:
            out.append(dict(row))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Разбор заявок: почему не дошли до сделки.")
    ap.add_argument("--order", help="идентификатор одной заявки")
    ap.add_argument("--lost", action="store_true", help="проваленные заявки")
    ap.add_argument("--open", action="store_true", help="заявки в работе")
    ap.add_argument("--days", type=int, default=30, help="за сколько последних дней")
    ap.add_argument("--day", help="по какую дату, ГГГГ-ММ-ДД")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--collect-only", action="store_true",
                    help="только собрать звонки клиентов, без обращения к модели")
    ap.add_argument("--redo", action="store_true", help="переразобрать уже разобранные")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token, timeout_sec=30.0,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries,
    )

    orders = pick_orders(conn, args)
    if args.limit:
        orders = orders[: args.limit]
    if not orders:
        print("подходящих заявок нет")
        return 0
    print(f"заявок к разбору: {len(orders)}")

    today = date.today().isoformat()

    # Сначала телефоны всех разбираемых заявок, потом один проход по периоду.
    # Компания делает около 1300 звонков в день: листать их заново под каждую
    # заявку — час работы и лишняя нагрузка на CRM.
    plan: list[tuple[dict, str, set[str]]] = []
    for order in orders:
        order_id = order["order_id"]
        if not args.redo and conn.execute(
            "SELECT 1 FROM order_reports WHERE order_id = ? AND verdict_json IS NOT NULL",
            (order_id,),
        ).fetchone():
            continue
        check = conn.execute(
            "SELECT contact_id FROM card_checks WHERE call_uid = ?", (order["call_uid"],)
        ).fetchone()
        contact_id = check["contact_id"] if check else None
        if not contact_id:
            logger.warning("заявка %s: контакт неизвестен", order_id)
            continue
        plan.append((order, contact_id, contact_phones(client, contact_id)))

    if not plan:
        print("всё разобрано, новых заявок нет")
        conn.close()
        return 0

    all_phones = {phone for _, _, phones in plan for phone in phones}
    oldest = min((order["created_at"] or today)[:10] for order, _, _ in plan)
    _saved, reached = collect_calls_for_phones(
        conn, client, settings, all_phones, since=oldest, until=today)

    done = skipped = 0
    for order, contact_id, phones in plan:
        order_id = order["order_id"]
        created = (order["created_at"] or "")[:10] or today
        calls = calls_with_client(conn, phones, order["created_at"])

        # Если листание не дошло до даты заявки, «звонков нет» означает «мы не
        # смотрели». Вердикт по такой заявке был бы обвинением на пустом месте.
        if not calls and reached > created:
            logger.warning("заявка %s (%s): звонки за период не просмотрены (дошли до %s)",
                           order_id, created, reached)
            save_order_report(
                conn, order_id=order_id, contact_id=contact_id, calls_count=0,
                verdict_json=None,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            conn.commit()
            skipped += 1
            continue

        verdict = None
        if not args.collect_only and settings.analysis_configured:
            verdict = analyzer.analyze_order(
                {**order, "order_id": order_id}, calls,
                api_key=settings.openai_api_key, model=settings.analysis_model,
                own_company=settings.own_company,
            )
        save_order_report(
            conn, order_id=order_id, contact_id=contact_id, calls_count=len(calls),
            verdict_json=json.dumps(verdict, ensure_ascii=False) if verdict else None,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        conn.commit()
        done += 1
        mark = verdict["outcome"][:80] if verdict else "звонки собраны"
        print(f"  {order_id} «{order['name']}»: звонков {len(calls)} — {mark}")

    conn.close()
    print(f"разобрано заявок: {done}"
          + (f", пропущено из-за неполного просмотра звонков: {skipped}" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
