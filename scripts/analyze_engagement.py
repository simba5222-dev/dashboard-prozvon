#!/usr/bin/env python
"""Вовлечённость менеджера (0-5) + совет ИИ — по всем заявкам всех потоков.

    sudo -u claude .venv/bin/python scripts/analyze_engagement.py            # сухой прогон
    sudo -u claude .venv/bin/python scripts/analyze_engagement.py --apply    # записать в CRM
    ... --order 730095       одна заявка, для ручной проверки
    ... --limit 100          не больше новых оценок (не пересчётов «пропустить») за прогон
    ... --since 2026-10-19   с какой даты создания заявки (по умолчанию из настройки)

Зачем. Владелец 27.10.2026 завёл в Synergy custom-30756 «Вовлечённость
менеджера» (число 0-5) и custom-30757 «Совет ИИ» (текст) — типы проверены
вживую через GET /custom-fields перед первой записью. Хочет держать их
заполненными по каждой заявке всех потоков, созданной с 19.10.2026, в любой
момент её жизни, не только по факту проигрыша, как уже делает
analyze_orders.py. Шкала и стиль совета — тот же разбор, что делался руками
по 94 заявкам Ткачевина в этой же сессии.

Устроено в три шага:

1. Развёртка — находим заявки, которые появились или изменились с прошлого
   прогона (`orders_by_sort`, высокий водяной знак в `kv_state`, отдельно
   для -created-at и -updated-at — иначе каждый тик пересматривал бы всю
   историю с даты начала). Корневой `/orders` фильтры тихо игнорирует (skill
   «синержи»), поэтому отбор только через сортировку + ранний стоп.

2. Рабочий набор — объединяем свежеразвёрнутые заявки с уже известными, но
   ещё не закрытыми (`stage_kind_snapshot` не lost/won — закрытую пересчи-
   тываем последний раз и больше не трогаем, тот же приём, что уже есть в
   analyze_orders.py для проваленных). Для каждой — читаем заявку по
   отдельности (`order_detail`): на списке `/orders` `responsible` врёт
   (грабли, найденные 27.10.2026), по одной заявке — нет.

3. Пересчёт — только если: заявки не было в нашей базе вовсе, она
   обновилась в Synergy с прошлой оценки, или нашёлся звонок новее
   прошлой оценки. Иначе пропускаем: пересчёт стоит тех же денег, что и
   первый расчёт. Без звонков и без ответственного решаем сами, не спрашивая
   модель — тот же принцип защиты, что у analyze_order (без материала модель
   начинает выдумывать).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.collector import (  # noqa: E402
    SynergyClient, collect_calls_for_phones, contact_phones, load_stages,
    order_detail, orders_by_sort,
)
from app.config import get_settings  # noqa: E402
from app.crm_write import CrmWriter  # noqa: E402
from app.db import (  # noqa: E402
    connect, get_engagement_report, get_state, init_schema,
    save_engagement_report, set_state,
)

logger = logging.getLogger("engagement")

# Id проверены вживую через GET /custom-fields/{id} 27.10.2026 перед первой
# записью: custom-30756 — "number" (целое), custom-30757 — "text".
FIELD_ENGAGEMENT = "custom-30756"
FIELD_ADVICE = "custom-30757"
FIELD_CRM_REASON = "custom-27969"   # «Причина статистика» — ставит менеджер сам
FIELD_PHONE = "custom-276"          # телефон клиента прямо в заявке (запасной путь)

# Та же причина, что в discover_ad_orders.py: такие заявки — не настоящая
# сделка, вовлечённость по ним не считаем вовсе (ни локально, ни в CRM).
NOT_A_LEAD = {"Спам", "СПАМ", "Дубль", "Кадры", "Не целевой звонок"}

STATE_CREATED = "engagement_sweep_created_at"
STATE_UPDATED = "engagement_sweep_updated_at"


def digits10(phone: str | None) -> str | None:
    d = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else None


def calls_with_client(conn, phones: set[str], after_iso: str) -> list[dict]:
    """Разговоры с этим клиентом после `after_iso` — чьи угодно.

    Тот же запрос, что в analyze_orders.py: сравниваем телефоны на своей
    стороне, потому что в базе они лежат как их отдала CRM.
    """
    rows = conn.execute(
        """
        SELECT k.uid, k.started_at, k.direction, k.duration_sec, k.vats_login,
               k.client_phone, t.text AS transcript_text
        FROM calls k LEFT JOIN transcripts t ON t.call_uid = k.uid
        WHERE k.started_at > ? ORDER BY k.started_at
        """,
        (after_iso,),
    ).fetchall()
    out = []
    for row in rows:
        d = digits10(row["client_phone"])
        if d and d in phones:
            out.append(dict(row))
    return out


def active_order_ids(conn) -> set[str]:
    rows = conn.execute(
        "SELECT order_id FROM engagement_reports WHERE stage_kind_snapshot NOT IN ('lost','won')"
    ).fetchall()
    return {r["order_id"] for r in rows}


def format_advice(note: str, advice: str, *, limit: int = 1500) -> str:
    text = " ".join(part for part in (note.strip(), advice.strip()) if part)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="с какой даты создания заявки (ISO), по умолчанию из настройки")
    ap.add_argument("--order", help="только эта заявка (для ручной проверки)")
    ap.add_argument("--limit", type=int, default=120,
                     help="не больше оценок с обращением к модели за прогон")
    ap.add_argument("--batch-limit", type=int, default=300,
                     help="не больше новых/обновлённых заявок разбирать за прогон — "
                     "догоняем отставание по кускам, не одним рывком")
    ap.add_argument("--apply", action="store_true", help="записать в Synergy")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        timeout_sec=30.0, min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries)
    stages = load_stages(client)
    crm = CrmWriter(client, apply=args.apply)

    since_raw = args.since or settings.engagement_since
    since_dt = datetime.fromisoformat(since_raw).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    candidate_ids: list[str] = []
    new_batch: list[dict] = []
    upd_batch: list[dict] = []
    if args.order:
        candidate_ids = [str(args.order)]
    else:
        created_wm = get_state(conn, STATE_CREATED)
        created_boundary = max(since_dt, datetime.fromisoformat(created_wm)) if created_wm else since_dt
        new_rows = orders_by_sort(client, sort="-created-at", boundary=created_boundary)
        # Старые — вперёд: догоняем отставание по порядку, не выдёргивая
        # сначала самые свежие. Пачка ограничена --batch-limit, чтобы один
        # прогон после долгого перерыва не пытался вычитать разом тысячи
        # заявок (упёрлись бы в таймаут службы).
        new_rows.sort(key=lambda r: r["created_at"])
        new_batch = new_rows[: args.batch_limit]

        updated_wm = get_state(conn, STATE_UPDATED)
        updated_boundary = max(since_dt, datetime.fromisoformat(updated_wm)) if updated_wm else since_dt
        upd_rows = orders_by_sort(client, sort="-updated-at", boundary=updated_boundary)
        upd_rows = [r for r in upd_rows
                    if r["created_at"] and
                    datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= since_dt]
        upd_rows.sort(key=lambda r: r["updated_at"])
        upd_batch = upd_rows[: args.batch_limit]

        # Порядок важен: --limit режет обращения к модели по ходу перебора,
        # и раньше тут был set — порядок перебора случайный, и обрезка
        # лимитом получалась вразнобой по всему дню вместо «сперва старое».
        # 27.10.2026: владелец заметил дыры посреди сегодняшних заявок —
        # именно из-за этого. Список, не множество, дубли убираем вручную.
        candidate_ids = []
        seen: set[str] = set()
        for r in new_batch + upd_batch:
            if r["id"] not in seen:
                seen.add(r["id"])
                candidate_ids.append(r["id"])
        for order_id in active_order_ids(conn):
            if order_id not in seen:
                seen.add(order_id)
                candidate_ids.append(order_id)

        if len(new_rows) > len(new_batch) or len(upd_rows) > len(upd_batch):
            print(f"отставание больше одной пачки: новых {len(new_rows)} "
                  f"(берём {len(new_batch)}), обновлённых {len(upd_rows)} "
                  f"(берём {len(upd_batch)}) — остаток догоним в следующих прогонах")

    print(f"заявок в рабочем наборе: {len(candidate_ids)}")
    if not candidate_ids:
        conn.close()
        return 0

    # Детали по каждой — список /orders врёт про responsible, читаем по одной.
    details: dict[str, dict] = {}
    for order_id in candidate_ids:
        detail = order_detail(client, order_id, stages)
        if not detail:
            continue
        reason = detail["customs"].get(FIELD_CRM_REASON) or []
        reason = reason if isinstance(reason, list) else [reason]
        if NOT_A_LEAD & set(reason):
            continue
        details[order_id] = detail

    # Телефоны всех заявок разом, один проход по звонкам компании — тот же
    # приём, что в analyze_orders.py, не листать историю заново под каждую.
    phones_by_order: dict[str, set[str]] = {}
    for order_id, d in details.items():
        phones = contact_phones(client, d["contact_id"]) if d["contact_id"] else set()
        own_phone = digits10(d["customs"].get(FIELD_PHONE))
        if own_phone:
            phones.add(own_phone)
        phones_by_order[order_id] = phones
    all_phones = {p for phones in phones_by_order.values() for p in phones}
    if all_phones:
        oldest = min(d["created_at"] for d in details.values())[:10]
        today = now.date().isoformat()
        _saved, reached = collect_calls_for_phones(
            conn, client, settings, all_phones, since=oldest, until=today)
    else:
        reached = now.date().isoformat()

    engine = settings.engagement_engine
    if engine == "yandex":
        api_key, model, folder = settings.yandex_api_key or "", settings.yandex_model, settings.yandex_folder or ""
    elif engine == "claude":
        api_key, model, folder = settings.anthropic_api_key or "", settings.anthropic_model, ""
    else:
        api_key, model, folder = settings.openai_api_key or "", settings.analysis_model, ""

    scored = skipped = 0
    for order_id, d in details.items():
        phones = phones_by_order.get(order_id) or set()
        if not phones:
            logger.warning("заявка %s: телефон клиента не найден, пропускаю", order_id)
            skipped += 1
            continue
        calls = calls_with_client(conn, phones, d["created_at"])
        if not calls and reached > d["created_at"][:10]:
            # Листание не дошло до даты заявки — «звонков нет» означало бы
            # «не смотрели», а не «не звонили». Разбирать по этому нечестно.
            skipped += 1
            continue

        prior = get_engagement_report(conn, order_id)
        last_call_at = max((c["started_at"] or "" for c in calls), default="")
        needs_recompute = (
            prior is None
            or d["updated_at"] > (prior.get("order_updated_snapshot") or "")
            or last_call_at > (prior.get("last_call_at") or "")
        )
        if not needs_recompute:
            # Ничего не изменилось с прошлой оценки. Но если тот прогон был
            # сухим (--apply не передавали), в CRM она так и не попала —
            # досылаем то, что уже посчитано, не считая заново (27.10.2026:
            # после сброса водяного знака сухой прогон насчитал сотню заявок
            # локально, а записать забыли — без этой ветки они зависли бы
            # навечно, потому что «ничего не поменялось» и так не наступит).
            if (args.apply and prior and not prior.get("applied")
                    and prior.get("engagement_score") is not None):
                text = format_advice(prior.get("engagement_note") or "", prior.get("advice") or "")
                if crm.update_customs(order_id, {
                    FIELD_ENGAGEMENT: prior["engagement_score"], FIELD_ADVICE: text,
                }):
                    conn.execute("UPDATE engagement_reports SET applied = 1 WHERE order_id = ?",
                                 (order_id,))
                    conn.commit()
                    logger.info("заявка %s: досослал в CRM то, что уже было посчитано", order_id)
                else:
                    logger.warning("заявка %s: дозапись в CRM не прошла (балл=%s)",
                                    order_id, prior["engagement_score"])
            skipped += 1
            continue

        # analyze_engagement() сам решает без модели, если нет ответственного
        # или нет звонков — лимит нужен только обращениям, которые реально
        # дойдут до модели.
        will_call_model = bool(d.get("responsible")) and bool(calls)
        if will_call_model and scored >= args.limit:
            continue
        order_for_prompt = {**d, "order_id": order_id}
        verdict = analyzer.analyze_engagement(
            order_for_prompt, calls, api_key=api_key, model=model,
            own_company=settings.own_company, engine=settings.engagement_engine,
            folder=folder,
        )
        if will_call_model:
            scored += 1

        applied_now = 0
        snapshot_updated_at = d["updated_at"]
        if verdict["engagement_score"] is not None:
            text = format_advice(verdict["engagement_note"], verdict["advice"])
            if crm.update_customs(order_id, {
                FIELD_ENGAGEMENT: verdict["engagement_score"], FIELD_ADVICE: text,
            }):
                applied_now = 1
                # Наша же запись выше двигает `updated-at` заявки в Synergy.
                # Взять снимок ДО записи (как раньше) значит на следующем
                # прогоне увидеть «заявка изменилась с прошлой оценки» из-за
                # собственной правки — и считать по кругу бесконечно, без
                # единого нового звонка. Нашли вживую 15.11.2026: один и тот
                # же набор ~23 заявок пересчитывался каждые 30 минут часами
                # подряд. Перечитываем заявку после записи, чтобы снимок
                # отражал состояние уже с нашей правкой внутри.
                fresh = order_detail(client, order_id, stages)
                if fresh:
                    snapshot_updated_at = fresh["updated_at"]

        save_engagement_report(
            conn, order_id=order_id, contact_id=d.get("contact_id"),
            engagement_score=verdict["engagement_score"],
            engagement_note=verdict["engagement_note"], advice=verdict["advice"],
            calls_count=len(calls), last_call_at=last_call_at or None,
            order_updated_snapshot=snapshot_updated_at,
            stage_kind_snapshot=d.get("stage_kind") or "",
            applied=applied_now,
            evaluated_at=now.isoformat(),
        )
        conn.commit()
        print(f"  заявка {order_id} «{d['name']}»: балл {verdict['engagement_score']}")

    print(f"оценено с обращением к модели: {scored}, пропущено: {skipped}")

    # Водяной знак продвигаем только теперь, когда пачка действительно
    # досмотрена целиком — если прогон упадёт или будет прибит по таймауту
    # раньше этого места, отметка останется на месте и недосмотренное не
    # потеряется, просто повторим в следующий раз.
    if not args.order:
        if new_batch:
            set_state(conn, STATE_CREATED, max(r["created_at"] for r in new_batch))
        if upd_batch:
            set_state(conn, STATE_UPDATED, max(r["updated_at"] for r in upd_batch))
        conn.commit()

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
