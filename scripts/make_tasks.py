#!/usr/bin/env python
"""Поставить менеджеру задачу по тому, о чём он договорился с клиентом.

    sudo -u claude .venv/bin/python scripts/make_tasks.py            сухой прогон
    ... --apply        действительно заводить задачи в CRM
    ... --days 14      за какой период смотреть заявки
    ... --order 731741 только по одной заявке

Правило владельца: если менеджер договорился с клиентом о следующем шаге —
перезвонить такого-то числа, отправить КП, — ставим ему задачу. Но **только
если заявка открыта**: по закрытой и по проваленной задач не ставим.

Откуда берётся шаг: разбор живой заявки (`analyze_orders.py --open`) слышит в
разговоре договорённость и кладёт её в `order_reports.next_step`. Здесь мы
только превращаем её в строку CRM.

Задача — строка в CRM, и повторять её создание вслепую нельзя. Поэтому:

- по заявке, где задача уже стоит (`task_at` заполнен), второй раз не ходим;
- след пишем и в сухом прогоне, но помечаем его как сухой, чтобы первый
  боевой запуск не завёл разом задачи по всем заявкам за месяц;
- перед записью заново спрашиваем CRM про стадию заявки: пока мы считали,
  менеджер мог закрыть её сам.
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

from app.collector import SynergyClient  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crm_write import CrmWriter  # noqa: E402
from app.db import connect, init_schema, save_order_task  # noqa: E402

logger = logging.getLogger("tasks")

# Как назвать задачу. Менеджер увидит одну строку в списке дел — она должна
# сразу говорить, что делать и с кем, без открывания карточки.
ЗАГОЛОВКИ = {
    "позвонить": "Позвонить клиенту: {what}",
    "отправить КП": "Отправить КП: {what}",
    "выставить счёт": "Выставить счёт: {what}",
    "подготовить договор": "Подготовить договор: {what}",
    "привезти технику на осмотр": "Показать технику: {what}",
}


def pick(conn, args) -> list[dict]:
    """Живые заявки с договорённостью, по которым задача ещё не ставилась."""
    where = ["r.kind = 'live'", "r.next_step IS NOT NULL", "r.next_step <> ''",
             "r.task_at IS NULL", "o.stage_kind NOT IN ('won', 'lost')"]
    params: list = []
    if args.order:
        where.append("o.order_id = ?")
        params.append(args.order)
    else:
        since = (date.today() - timedelta(days=args.days)).isoformat()
        where.append("o.created_at >= ?")
        params.append(since)
    rows = conn.execute(
        f"""
        SELECT o.order_id, o.name, o.responsible, o.stage_name,
               r.next_step, r.next_step_due, r.verdict_json
        FROM order_reports r JOIN call_orders o ON o.order_id = r.order_id
        WHERE {' AND '.join(where)}
        GROUP BY o.order_id
        ORDER BY o.created_at DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def due_at(verdict: dict, hour: int) -> str:
    """Срок задачи в виде, который понимает Synergy.

    Модель отдаёт дату отдельно от того, как срок прозвучал в разговоре.
    Даты может не быть — «созвонимся на неделе» датой не становится; тогда
    ставим завтра.

    Срок уже прошёл — ставим на сегодня, а не на завтра. «Отправлю счёт через
    пятнадцать минут» со вчерашнего разговора менеджер просрочил сегодня, и
    задача на завтра эту просрочку только удлинит. Если рабочий час уже прошёл,
    даём час на реакцию, но не позже шести вечера.
    """
    сегодня = date.today()
    try:
        день = date.fromisoformat(verdict.get("next_step_date") or "")
    except ValueError:
        день = сегодня + timedelta(days=1)
    if день < сегодня:
        день = сегодня
    if день == сегодня:
        hour = min(max(hour, datetime.now().hour + 1), 18)
    # Пояс проставляем свой, московский: без него Synergy считает время по
    # Гринвичу и утренняя задача встаёт на ночь.
    return f"{день.isoformat()}T{hour:02d}:00:00.000+03:00"


def still_open(client: SynergyClient, order_id: str) -> bool:
    """Заявка всё ещё открыта? Спрашиваем CRM, а не свою копию."""
    try:
        data = client.get(f"orders/{order_id}", include="stage")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("заявка %s не прочиталась: %s", order_id, exc)
        return False
    # Вид стадии заполнен только у трёх: «Сделка» (won), «Новый» (opened) и
    # «Сделка провалена» (lost). Промежуточные — с пустым видом, и они открыты.
    for item in (data or {}).get("included") or []:
        if item.get("type") == "order-stages":
            kind = str((item.get("attributes") or {}).get("kind") or "")
            return kind not in ("won", "lost")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Задачи менеджерам по договорённостям с клиентом.")
    ap.add_argument("--apply", action="store_true", help="заводить задачи в CRM")
    ap.add_argument("--days", type=int, default=30, help="за сколько последних дней")
    ap.add_argument("--order", help="только одна заявка")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token, timeout_sec=30.0,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries,
    )
    writer = CrmWriter(client, apply=args.apply)

    rows = pick(conn, args)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("договорённостей без задач нет")
        return 0
    print(f"договорённостей к постановке: {len(rows)}"
          + ("" if args.apply else " — сухой прогон, в CRM ничего не уйдёт"))

    поставлено = пропущено = 0
    for row in rows:
        order_id = row["order_id"]
        verdict = json.loads(row["verdict_json"] or "{}")
        шаг = verdict.get("next_step_kind") or "позвонить"

        if not still_open(client, order_id):
            print(f"  {order_id} «{row['name']}»: заявка уже закрыта — задачу не ставим")
            save_order_task(conn, order_id, task_id=None,
                            task_at=datetime.now(timezone.utc).isoformat(),
                            note="заявка закрылась до постановки")
            conn.commit()
            пропущено += 1
            continue

        responsible = writer.order_responsible(order_id)
        if not responsible:
            print(f"  {order_id} «{row['name']}»: ответственный не назначен — пропуск")
            пропущено += 1
            continue

        name = ЗАГОЛОВКИ.get(шаг, "{what}").format(what=row["next_step"])
        срок = due_at(verdict, settings.crm_task_hour)
        описание = "\n".join(filter(None, [
            f"Договорённость из разговора: {row['next_step']}",
            f"Клиент назвал срок: {row['next_step_due']}" if row["next_step_due"] else "",
            f"Где заявка сейчас: {verdict.get('stage_now') or row['stage_name'] or '—'}",
            f"Что говорит клиент: {verdict['client_position']}"
            if verdict.get("client_position") else "",
            "Задачу поставил разбор звонков.",
        ]))

        task_id = writer.create_task(
            order_id=order_id, name=name, due_at=срок, responsible_id=responsible,
            type_id=settings.crm_task_type, description=описание,
        )
        save_order_task(
            conn, order_id, task_id=task_id,
            task_at=datetime.now(timezone.utc).isoformat(),
            note="сухой прогон" if writer.dry_run else "",
        )
        conn.commit()
        поставлено += 1
        print(f"  {order_id} «{row['name']}»: {name} — на {срок[:10]}, "
              f"{row['responsible'] or 'ответственный ' + responsible}")

    conn.close()
    print(f"задач поставлено: {поставлено}"
          + (f", пропущено: {пропущено}" if пропущено else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
