"""Сводный отчёт владельцу по менеджерам — звонки, заявки, вовлечённость.

Не новый анализ: всё, что здесь считается, уже посчитано где-то в проекте
(вовлечённость — `analyze_engagement.py`, звонки прозвона — `report_rows`,
входящие продаж — `inbound_rows`). Этот модуль только сводит их в одну
картину по каждому менеджеру и просит модель написать по ней текст.

Единственное, чего не было нигде: «выигранная сделка + сумма + ответственный
менеджер» в одной строке. Кирпичи были (`responsible_id` у заявки,
`stage_kind == 'won'`, `amount`), но нигде вместе. Join идёт через
`managers.synergy_user` (Synergy ID менеджера) — тот же приём, что уже
применяется для начисления соисполнителей (`app/collector.py`, `catch_leads.py`).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.collector import SynergyClient, order_detail, orders_by_sort
from app.db import get_engagement_report
from app.stats import coaching_picks, inbound_rows, managers, report_rows, report_totals

logger = logging.getLogger(__name__)

DIGEST_PROMPT = """Ты помогаешь владельцу небольшой компании (аренда спецтехники)
разобраться, как увеличить объём продаж. Вот сводка по менеджерам за {period_label}.

Данные по каждому менеджеру ниже. Это не сырые разговоры — уже посчитанные
числа и несколько отобранных примеров. Твоя задача:

1. Короткий общий вывод (2-3 предложения): как дела в целом, по сравнению
   с чем можно судить (если цифр для сравнения нет — не придумывай сравнение).
2. По 1-2 менеджерам, которые выделяются в лучшую сторону — что именно у них
   получается (если это видно из советов/примеров ниже).
3. По 1-2 менеджерам, которые выделяются в худшую сторону — что конкретно
   стоит обсудить (опирайся только на данные ниже, не выдумывай причины).
4. Если в данных есть заявки без определённого ответственного менеджера —
   упомяни это отдельно, это сигнал о пробеле в CRM, не о плохой работе.

Пиши по-русски, коротко (это сообщение в Telegram, не отчёт на страницу),
без markdown-таблиц (только обычный текст и списки с «-»). Если данных мало
или они не позволяют сделать какой-то из пунктов — честно скажи об этом,
а не заполняй пункт общими словами.

ДАННЫЕ:
{body}
"""


def _by_synergy_user(conn: sqlite3.Connection) -> dict[str, str]:
    """synergy_user → vats_login.

    Нашли вживую 15.11.2026: у Воронкова и Мущенко в таблице `managers`
    одинаковый `synergy_user` (30821) — опечатка при заведении, не наша
    логика. При дубле последняя строка тихо выигрывала бы и склеивала двух
    разных менеджеров в одного — вместо этого громко предупреждаем и не
    приписываем заявки НИКОМУ из дублирующихся (лучше пропуск, чем подмена
    человека).
    """
    rows = conn.execute(
        "SELECT vats_login, synergy_user FROM managers "
        "WHERE active = 1 AND synergy_user IS NOT NULL AND synergy_user <> ''"
    ).fetchall()
    by_user: dict[str, list[str]] = {}
    for r in rows:
        by_user.setdefault(str(r["synergy_user"]), []).append(r["vats_login"])
    result = {}
    for user, logins in by_user.items():
        if len(logins) > 1:
            logger.warning("synergy_user %s повторяется у менеджеров %s — "
                           "их заявки не будут приписаны никому, пока не "
                           "исправлена таблица managers", user, logins)
            continue
        result[user] = logins[0]
    return result


def collect_orders(client: SynergyClient, stages: dict[str, tuple[str, str]],
                   conn: sqlite3.Connection, since_dt: datetime,
                   max_pages: int = 60) -> list[dict[str, Any]]:
    """Заявки, тронутые (созданные или обновлённые) с `since_dt`, с вовлечённостью.

    Один обход по `-updated-at` достаточен: заявка, созданная в периоде, тем
    самым и обновлена в периоде (создание — это и есть первое обновление).
    """
    rows = orders_by_sort(client, sort="-updated-at", boundary=since_dt, max_pages=max_pages)
    out = []
    for r in rows:
        detail = order_detail(client, r["id"], stages)
        if not detail:
            continue
        eng = get_engagement_report(conn, r["id"])
        detail["engagement_score"] = eng["engagement_score"] if eng else None
        detail["advice"] = eng["advice"] if eng else None
        out.append(detail)
    return out


def build_manager_stats(conn: sqlite3.Connection, client: SynergyClient,
                        stages: dict[str, tuple[str, str]],
                        since: str, until: str) -> dict[str, Any]:
    """Собрать по каждому активному менеджеру (оба отдела) цифры за период."""
    since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    orders = collect_orders(client, stages, conn, since_dt)
    by_synergy_user = _by_synergy_user(conn)

    orders_by_login: dict[str, list[dict]] = {}
    unmatched = 0
    for o in orders:
        login = by_synergy_user.get(o.get("responsible_id") or "")
        if login:
            orders_by_login.setdefault(login, []).append(o)
        else:
            unmatched += 1

    prozvon_rows = report_rows(conn, since, until)
    coaching_by_login = {b["vats_login"]: b["calls"] for b in coaching_picks(prozvon_rows)}

    depts = [r[0] for r in conn.execute(
        "SELECT DISTINCT dept FROM managers WHERE active = 1").fetchall()]

    result = []
    for dept in depts:
        for m in managers(conn, dept=dept):
            login = m["vats_login"]
            my_orders = orders_by_login.get(login, [])
            scores = [o["engagement_score"] for o in my_orders if o.get("engagement_score") is not None]

            entry: dict[str, Any] = {
                "vats_login": login,
                "display_name": m["display_name"],
                "dept": dept,
                "won_count": sum(1 for o in my_orders if o["stage_kind"] == "won"),
                "won_amount": sum(o["amount"] or 0 for o in my_orders if o["stage_kind"] == "won"),
                "lost_count": sum(1 for o in my_orders if o["stage_kind"] == "lost"),
                "in_work_count": sum(1 for o in my_orders if o["stage_kind"] not in ("won", "lost")),
                "avg_engagement": round(sum(scores) / len(scores), 1) if scores else None,
                "advice_samples": [o["advice"] for o in my_orders if o.get("advice")][:2],
            }

            # Без добавочного/логина ВАТС звонки физически не могут попасть
            # этому менеджеру в таблицу `calls` — показывать 0 было бы ложью
            # (выглядит как «не звонил», а не «ВАТС не настроена»). Нашли
            # живьём 15.11.2026 на Сергееве: его добавочный в CRM устарел,
            # сейчас реально принадлежит Толстову (сисадмину поставлена
            # задача починить) — в managers у Сергеева поэтому `ext`/`vats_user`
            # оставлены пустыми намеренно, не забыты.
            has_vats_seat = bool(m["ext"] or m["vats_user"])

            if dept == "прозвон" and has_vats_seat:
                my_calls = [r for r in prozvon_rows if r["vats_login"] == login]
                entry["calls"] = report_totals(my_calls)
                entry["coaching"] = coaching_by_login.get(login, [])
            elif dept == "продажи" and has_vats_seat:
                in_rows = inbound_rows(conn, since, until, only_open=False, manager=login)
                entry["calls"] = {
                    "calls": len(in_rows),
                    "checked": sum(1 for r in in_rows if r["checked"]),
                    "with_order": sum(1 for r in in_rows if (r["orders_after"] or 0) > 0),
                }
                entry["coaching"] = []
            else:
                # Другие отделы (напр. «поиск» — поток 4, звонки поставщикам)
                # считаются по-своему и здесь не разобраны — не выдумываем
                # цифру звонков, честно показываем, что её нет.
                entry["calls"] = {"calls": None}
                entry["coaching"] = []

            result.append(entry)

    return {"managers": result, "unmatched_orders": unmatched}


def _format_body(stats: dict[str, Any]) -> str:
    lines: list[str] = []
    for m in stats["managers"]:
        lines.append(f"\n{m['display_name']} ({m['dept']}):")
        c = m["calls"]
        calls_n = c.get("calls")
        lines.append(f"  звонков: {calls_n if calls_n is not None else 'нет данных для этого отдела'}")
        lines.append(f"  заявки: выиграно {m['won_count']} на сумму {m['won_amount']:.0f}, "
                     f"проиграно {m['lost_count']}, в работе {m['in_work_count']}")
        lines.append(f"  вовлечённость (средняя 0-5): "
                     f"{m['avg_engagement'] if m['avg_engagement'] is not None else 'нет данных'}")
        if m["advice_samples"]:
            lines.append("  советы ИИ по отдельным заявкам:")
            for a in m["advice_samples"]:
                lines.append(f"    - {a}")
        if m["coaching"]:
            lines.append(f"  показательных звонков для разбора: {len(m['coaching'])}")
    if stats["unmatched_orders"]:
        lines.append(f"\nЗаявок без распознанного ответственного менеджера: "
                     f"{stats['unmatched_orders']}")
    return "\n".join(lines)


def render_digest(stats: dict[str, Any], period_label: str, *,
                  engine: str, api_key: str, model: str,
                  folder: str = "", timeout_sec: float = 90.0) -> str:
    from app.analyzer import render_digest_text

    prompt = DIGEST_PROMPT.format(period_label=period_label, body=_format_body(stats))
    return render_digest_text(prompt, engine=engine, api_key=api_key, model=model,
                              folder=folder, timeout_sec=timeout_sec,
                              kind="свод по менеджерам")
