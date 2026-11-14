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
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.collector import (  # noqa: E402
    SynergyClient, collect_calls_for_phones, contact_phones,
    load_stages, order_detail, orders_by_sort,
)
from app.config import get_settings  # noqa: E402
from app.crm_write import CrmWriter  # noqa: E402
from app.db import connect, init_schema, save_order_report  # noqa: E402

logger = logging.getLogger("orders")

# Разбор заявки — отдельные поля от тех, что пишет первичный анализ звонка
# (custom-30609/30600/30601 на заявке, custom-17265 на звонке): это другой,
# более поздний шаг — общая картина по заявке из всех звонков, живой или
# проваленной. Заведены владельцем в Synergy 22.10.2026 специально под это,
# id проверены через GET /custom-fields перед первой записью.
FIELD_ORDER_BREAKDOWN = "custom-30753"     # «Разбор заявки» — длинный текст
FIELD_ORDER_ROP_ATTENTION = "custom-30754"  # «Внимание РОПа» — пусто, если не нужно
FIELD_CRM_REASON = "custom-27969"          # «Причина статистика» — ставит менеджер сам


def verdict_text(live: bool, v: dict[str, Any], crm_reason: str = "") -> str:
    """Человекочитаемый текст для поля «Разбор заявки».

    `crm_reason` — значение «Причина статистика» (custom-27969), которое
    менеджер ставит сам: наш разбор видит только звонки из ВАТС (не встречи,
    переписку, звонки с личных телефонов), поэтому иногда расходится с
    ручной отметкой. Показываем рядом, а не молчим о расхождении — владелец
    попросил 22.10.2026, на живом примере (разбор решил «не дозвонились»,
    менеджер отметил «Выбрал конкурента»)."""
    if v.get("no_calls"):
        return "Звонков с клиентом за это время не нашли — текст не придумываем, ждём разговора."
    if not live and crm_reason:
        lines_crm = [f"Причина в CRM (отметил менеджер): {crm_reason}"]
    else:
        lines_crm = []
    if live:
        lines = [f"Этап: {v.get('stage_now') or '—'}"]
        if v.get("client_position"):
            lines.append(f"Позиция клиента: {v['client_position']}")
        if v.get("next_step"):
            due = f" (к {v['next_step_due']})" if v.get("next_step_due") else ""
            lines.append(f"Дальше: {v['next_step']}{due}")
        if v.get("blocker"):
            lines.append(f"Мешает: {v['blocker']}")
    else:
        lines = [f"Причина: {v.get('outcome') or '—'}"]
        if v.get("stage_failed"):
            lines.append(f"Сорвалось на этапе: {v['stage_failed']}")
        for gap in v.get("gaps") or []:
            lines.append(f"Не сделал менеджер: {gap}")
        if v.get("recoverable") is not None:
            lines.append("Можно вернуть клиента: " + ("да" if v["recoverable"] else "нет"))
    for rec in v.get("recommendations") or []:
        lines.append(f"Рекомендация: {rec}")
    return "\n".join(lines_crm + lines)


def rop_attention_text(live: bool, v: dict[str, Any]) -> str:
    """«Внимание РОПа» — пусто, если звать руководителя не нужно."""
    if live and v.get("needs_rop"):
        return v.get("rop_reason") or "нужно внимание РОПа"
    return ""


def order_extra(client: SynergyClient, order_id: str) -> tuple[str, str | None]:
    """Причина в CRM и ответственный — одним запросом на заявку."""
    try:
        data = client.get(f"orders/{order_id}", include="responsible")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("заявка %s: доп. сведения не прочитались: %s", order_id, exc)
        return "", None
    attrs = (data.get("data") or {}).get("attributes") or {}
    customs = attrs.get("customs") or {}
    reason = customs.get(FIELD_CRM_REASON) or []
    reason_text = ", ".join(reason) if isinstance(reason, list) else str(reason)
    rels = (data.get("data") or {}).get("relationships") or {}
    ref = (rels.get("responsible") or {}).get("data")
    return reason_text, (str(ref["id"]) if ref else None)


def write_order_breakdown(client: SynergyClient, order_id: str, live: bool,
                          v: dict[str, Any], crm_reason: str) -> None:
    try:
        client.patch(f"orders/{order_id}", {"data": {
            "type": "orders", "id": order_id,
            "attributes": {"customs": {
                FIELD_ORDER_BREAKDOWN: verdict_text(live, v, crm_reason if not live else ""),
                FIELD_ORDER_ROP_ATTENTION: rop_attention_text(live, v),
            }},
        }})
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("заявка %s: разбор не записался в Synergy: %s", order_id, exc)


# Задача на 3 дня — только для проваленных заявок, где разбор считает, что
# клиента можно вернуть (recoverable). Решение владельца 22.10.2026: спросить
# тогда, какая ещё техника может понадобиться. Тот же тип, что уже выбрал
# владелец 30.09.2026 для похожих задач (`make_tasks.py`) — 5305 «Теплые».
FOLLOWUP_DIARY_TYPE = "5305"
FOLLOWUP_DAYS = 3
FOLLOWUP_NAME = "Уточнить, какая ещё техника может понадобиться"


def maybe_create_followup_task(writer: CrmWriter, order_id: str, v: dict[str, Any],
                               responsible_id: str | None) -> None:
    """Для проваленной, но возвратной заявки — задача менеджеру через 3 дня.

    22.10.2026 проверено вживую: создание задач (`POST /diaries`) у Synergy
    всё ещё отвечает 500 на любой корректный запрос — тот же баг, что нашли
    30.09.2026 (см. `make_tasks.py`, письмо в поддержку уже отправлено).
    Поэтому пишем код сейчас, в боевом режиме (не сухом прогоне) — как только
    Synergy починит у себя, задачи начнут вставать сами, без правок здесь.
    Если это конкретная заявка уже не попадёт на повторный разбор (проваленные
    разбираются один раз), задачу по ней придётся поставить руками запуском
    с `--redo`, когда баг у Synergy закроют — ограничение именно отсюда.
    """
    if not v.get("recoverable"):
        return
    if not responsible_id:
        logger.warning("заявка %s: ответственный неизвестен, задачу не ставлю", order_id)
        return
    due = datetime.now(timezone(timedelta(hours=3))) + timedelta(days=FOLLOWUP_DAYS)
    due_iso = due.strftime("%Y-%m-%dT%H:%M:%S.000%z")
    due_iso = due_iso[:-2] + ":" + due_iso[-2:]
    writer.create_task(
        order_id=order_id, name=FOLLOWUP_NAME, due_at=due_iso,
        responsible_id=responsible_id, type_id=FOLLOWUP_DIARY_TYPE,
        description="Заявка проиграна, но разбор считает, что клиента можно "
                    "вернуть — уточнить, не нужна ли другая техника.",
    )


def pick_orders(args, client: SynergyClient, stages: dict[str, tuple[str, str]]) -> list[dict]:
    """Какие заявки разбирать: одну названную или все подходящие за период —
    из Synergy напрямую, любой канал (решение владельца, 14.11.2026: раньше
    бралось только из локальной `call_orders`, 377 строк за всё время —
    заводится лишь когда заявка возникла из звонка, который дашборд уже
    отследил локально, то есть почти только прозвон; «Пойманная с
    прослушки» и вообще любая заявка без такой локальной привязки сюда
    никогда не попадала).

    Отбор по стадии — дёшево, по `stage_id` из уже загруженного `stages`,
    без обращения к CRM на каждую заявку: полную карточку (`order_detail`)
    читаем позже, только для тех, что реально дойдут до разбора — так же,
    как уже сделано в `analyze_engagement.py`.
    """
    if args.order:
        row = order_detail(client, args.order, stages)
        return [row] if row else []

    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()
    boundary = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    kinds = set()
    if args.lost:
        kinds.add("lost")
    if args.open:
        kinds.update({"opened", ""})
    if not kinds:
        return []

    seen: dict[str, dict] = {}
    for sort in ("-created-at", "-updated-at"):
        for row in orders_by_sort(client, sort=sort, boundary=boundary):
            _, stage_kind = stages.get(row["stage_id"], ("", ""))
            if stage_kind not in kinds:
                continue
            seen[row["id"]] = {
                "order_id": row["id"], "call_uid": None,
                "created_at": row["created_at"], "stage_kind": stage_kind,
            }
    return sorted(seen.values(), key=lambda o: o["created_at"], reverse=True)


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
    engine = settings.order_analysis_engine
    head_ready = {
        "yandex": settings.yandex_stt_configured,
        "claude": bool(settings.anthropic_api_key),
    }.get(engine, settings.analysis_configured)
    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token, timeout_sec=30.0,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries,
    )
    writer = CrmWriter(client, apply=True)
    stages = load_stages(client)

    orders = pick_orders(args, client, stages)
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
        # Полную карточку (имя, ответственный, стадия, сумма, контакт) читаем
        # только теперь — для заявок, что реально дойдут до разбора. Для
        # `--order` она уже есть из pick_orders, второй раз не ходим.
        full = order if "name" in order else order_detail(client, order_id, stages)
        if not full:
            logger.warning("заявка %s не прочиталась", order_id)
            continue
        contact_id = full.get("contact_id")
        if not contact_id:
            logger.warning("заявка %s: контакт неизвестен", order_id)
            continue
        plan.append((full, contact_id, contact_phones(client, contact_id)))

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
        if not args.collect_only and head_ready:
            key, model = {
                "yandex": (settings.yandex_api_key, settings.yandex_model),
                "claude": (settings.anthropic_api_key, settings.anthropic_model),
            }.get(engine, (settings.openai_api_key, settings.analysis_model))
            key = key or ""
            folder = settings.yandex_folder or ""
            if live:
                verdict = analyzer.analyze_live_order(
                    {**order, "order_id": order_id}, calls,
                    api_key=key, model=model, own_company=settings.own_company,
                    silence_days=silence_days(calls),
                    engine=engine, folder=folder,
                )
            else:
                verdict = analyzer.analyze_order(
                    {**order, "order_id": order_id}, calls,
                    api_key=key, model=model, own_company=settings.own_company,
                    engine=engine, folder=folder,
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
        if verdict:
            crm_reason, responsible_id = order_extra(client, order_id)
            write_order_breakdown(client, order_id, live, v, crm_reason)
            if not live:
                maybe_create_followup_task(writer, order_id, v, responsible_id)
        done += 1
        if not verdict:
            mark = "звонки собраны"
        elif v.get("no_calls"):
            mark = "звонков с клиентом не нашли"
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
