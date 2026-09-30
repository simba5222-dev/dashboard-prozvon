#!/usr/bin/env python
"""Разобрать заявки по разговорам с клиентом.

    sudo -u claude .venv/bin/python scripts/analyze_orders.py --order 732823
    ... --lost --days 30          все проваленные заявки за месяц
    ... --open                    и те, что висят в работе
    ... --lost --open             и те, и другие — так ходит таймер
    ... --collect-only            только собрать звонки, без разбора
    ... --refresh-hours 20        как часто освежать разбор живых заявок

У проваленной заявки и у живой спрашивают разное, и это два разных разбора:

- **провалена** — на каком этапе сорвалось и почему, можно ли вернуть клиента;
- **в работе** — где заявка сейчас, что говорит клиент, о чём менеджер
  договорился дальше и нужно ли вмешательство руководителя. Договорённость
  отсюда забирает `make_tasks.py` и ставит менеджеру задачу в CRM.

Проваленную разбираем один раз: её судьба решена. Живую освежаем, но **только
если с клиентом с тех пор разговаривали** — иначе модель ответит то же самое,
а деньги спишутся.

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

import httpx

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


def order_contact(client: SynergyClient, order_id: str) -> str | None:
    """Контакт заявки — из самой заявки.

    След проверки карточки (`card_checks`) есть только у заявок, заведённых
    разбором звонка: таких 37 из 170. У остальных заявку завёл человек, и
    единственный способ узнать клиента — спросить CRM. Без этого разбор
    молча проходит мимо четырёх заявок из пяти.
    """
    # `include` обязателен: без него Synergy отдаёт заявку со всеми связями,
    # но с пустым `data` в каждой — молча, без ошибки.
    try:
        data = client.get(f"orders/{order_id}", include="contact")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("заявка %s не прочиталась: %s", order_id, exc)
        return None
    rels = ((data or {}).get("data") or {}).get("relationships") or {}
    ref = (rels.get("contact") or {}).get("data")
    return str(ref["id"]) if ref else None


def silence_days(calls: list[dict]) -> int | None:
    """Сколько дней прошло с последнего разговора с клиентом.

    Считаем сами, а не спрашиваем модель: даты у нас точные, а она из
    расшифровки срок не выведет.
    """
    stamps = [c["started_at"] for c in calls if c.get("started_at")]
    if not stamps:
        return None
    last = max(stamps)[:10]
    try:
        return (date.today() - date.fromisoformat(last)).days
    except ValueError:
        return None


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
    ap.add_argument("--refresh-hours", type=float, default=20.0,
                    help="через сколько часов освежать разбор живых заявок")
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
    # Когда живую заявку уже разбирали. Если с тех пор с клиентом не
    # разговаривали, разбирать заново нечего: модель ответит то же самое, а
    # деньги спишутся. Тридцать живых заявок в день — это 750 ₽ в месяц на
    # пустом месте.
    разбирали: dict[str, str] = {}
    for order in orders:
        order_id = order["order_id"]
        # Проваленную заявку разбираем один раз: её судьба уже решена. Живая
        # меняется каждый день — её вердикт протухает, и руководитель увидит
        # вчерашнюю картину. Поэтому живые переразбираем, если прошли сутки.
        was = conn.execute(
            "SELECT created_at FROM order_reports "
            "WHERE order_id = ? AND verdict_json IS NOT NULL",
            (order_id,),
        ).fetchone()
        if was and not args.redo:
            if order.get("stage_kind") in ("lost", "won"):
                continue
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(was["created_at"])).total_seconds()
            if age < args.refresh_hours * 3600:
                continue
            разбирали[order_id] = was["created_at"]
        check = conn.execute(
            "SELECT contact_id FROM card_checks WHERE call_uid = ?", (order["call_uid"],)
        ).fetchone()
        contact_id = (check["contact_id"] if check else None) or order_contact(client, order_id)
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

        # Проваленную заявку и живую спрашиваем о разном. У первой — где
        # сорвалось и почему, у второй — где она сейчас, о чём договорились
        # и надо ли звать руководителя.
        live = order.get("stage_kind") not in ("lost", "won")

        # Разбор освежаем только при новом разговоре. Пустой прогон стоит
        # столько же, сколько содержательный.
        прошлый = разбирали.get(order_id)
        if прошлый and not any((c.get("started_at") or "") > прошлый for c in calls):
            logger.info("заявка %s: новых разговоров нет, вердикт оставляем", order_id)
            skipped += 1
            continue

        verdict = None
        if not args.collect_only and settings.analysis_configured:
            if live:
                verdict = analyzer.analyze_live_order(
                    {**order, "order_id": order_id}, calls,
                    api_key=settings.openai_api_key, model=settings.analysis_model,
                    own_company=settings.own_company,
                    silence_days=silence_days(calls),
                )
            else:
                verdict = analyzer.analyze_order(
                    {**order, "order_id": order_id}, calls,
                    api_key=settings.openai_api_key, model=settings.analysis_model,
                    own_company=settings.own_company,
                )
        v = verdict or {}
        save_order_report(
            conn, order_id=order_id, contact_id=contact_id, calls_count=len(calls),
            verdict_json=json.dumps(verdict, ensure_ascii=False) if verdict else None,
            created_at=datetime.now(timezone.utc).isoformat(),
            kind=("live" if live else "lost") if verdict else "",
            stage_now=v.get("stage_now") or v.get("stage_failed") or None,
            next_step=v.get("next_step") or None,
            next_step_due=v.get("next_step_due") or None,
            needs_rop=1 if v.get("needs_rop") else 0,
            rop_reason=v.get("rop_reason") or None,
        )
        conn.commit()
        done += 1
        if not verdict:
            mark = "звонки собраны"
        elif live:
            mark = ("РОП: " + v["rop_reason"][:70]) if v.get("needs_rop") else (
                f"{v.get('stage_now') or 'этап неясен'}"
                + (f", дальше: {v['next_step'][:50]}" if v.get("next_step") else ""))
        else:
            mark = v.get("outcome", "")[:80]
        print(f"  {order_id} «{order['name']}»: звонков {len(calls)} — {mark}")

    conn.close()
    print(f"разобрано заявок: {done}"
          + (f", пропущено: {skipped}" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
