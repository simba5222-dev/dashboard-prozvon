#!/usr/bin/env python
"""Проверить заявки, заведённые разбором, и дозаполнить их.

    sudo -u claude .venv/bin/python scripts/audit_leads.py          только проверить
    sudo -u claude .venv/bin/python scripts/audit_leads.py --fix    ещё и починить

Заявку в CRM заводит машина, поэтому за ней нужен отдельный присмотр. Проверяем
по каждой:

1. **Запрос действительно был и просил его клиент.** Перепроверка идёт по полной
   расшифровке и по обезличенным сторонам: роли по номеру канала у входящих
   ненадёжны, а «наш менеджер искал технику у подрядчика» — не заявка.
2. **Это не дубль.** У контакта не должно быть другой заявки, заведённой вокруг
   того же звонка.
3. **Тип техники проставлен** — из справочника CRM, а не своими словами.
4. **Ответственный и соисполнитель на месте:** ответственный — служебная учётка,
   соисполнитель — менеджер, который говорил с клиентом.
5. **Выжимка и расшифровка заполнены** — иначе заявку невозможно понять, не
   слушая запись.

С `--fix` чинится то, что чинится машиной: тип техники, ответственный,
соисполнитель, выжимка. Сомнительные по существу (запроса не было, дубль) только
помечаются: закрывать заявку за человека скрипт не должен.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.collector import SynergyClient  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.crm_write import CrmWriter, lead_summary  # noqa: E402
from app.db import connect  # noqa: E402

logger = logging.getLogger("audit")


def transport_types(client: SynergyClient) -> dict[str, str]:
    """Справочник типов техники CRM: название → идентификатор."""
    out: dict[str, str] = {}
    for page in range(1, 6):
        rows = client.get("transport-types", per_page=100, page=page).get("data") or []
        if not rows:
            break
        for row in rows:
            name = str((row.get("attributes") or {}).get("name") or "").strip()
            if name:
                out[name] = str(row["id"])
    return out


def order_state(client: SynergyClient, order_id: str) -> dict:
    """Что сейчас в заявке: стадия, люди, тип техники, заполненность полей."""
    data = client.get(f"orders/{order_id}", include="responsible,performers,stage,transport-type")
    attrs = data["data"]["attributes"]
    rels = data["data"].get("relationships") or {}
    customs = attrs.get("customs") or {}
    included = data.get("included") or []
    return {
        "name": attrs.get("name"),
        "number": attrs.get("number"),
        "stage": next((str((i.get("attributes") or {}).get("name"))
                       for i in included if i["type"] == "order-stages"), ""),
        "transport": ((rels.get("transport-type") or {}).get("data") or {}).get("id"),
        "users": [str(i["id"]) for i in included if i["type"] == "users"],
        "summary": customs.get("custom-30609") or "",
        "transcript": customs.get("custom-30599") or "",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка заявок, заведённых разбором.")
    ap.add_argument("--fix", action="store_true", help="дозаполнить то, что чинится машиной")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    conn = connect(settings.db_path)
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token, timeout_sec=30.0,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries,
    )
    writer = CrmWriter(client, apply=args.fix)
    types = transport_types(client)

    rows = conn.execute(
        """
        SELECT s.call_uid, s.created_order_id, s.verdict_json, s.is_request,
               k.started_at, k.vats_login, k.client_phone, k.duration_sec,
               m.display_name, m.synergy_user,
               i.contact_id, i.contact_name, i.company_name, i.active_names,
               t.text AS transcript
        FROM screens s
        JOIN calls k ON k.uid = s.call_uid
        LEFT JOIN managers m ON m.vats_login = k.vats_login
        LEFT JOIN inbound_checks i ON i.call_uid = s.call_uid
        LEFT JOIN transcripts t ON t.call_uid = s.call_uid
        WHERE s.created_order_id IS NOT NULL AND s.is_request = 1
        ORDER BY k.started_at
        """
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"заявок к проверке: {len(rows)}" + ("" if args.fix else " (только проверка)"))

    trouble = 0
    for row in rows:
        order_id = row["created_order_id"]
        screen = json.loads(row["verdict_json"] or "{}")
        state = order_state(client, order_id)
        problems: list[str] = []
        fixed: list[str] = []

        # 1. Запрос от клиента — перепроверка по полной расшифровке.
        text = (row["transcript"] or "").replace("operator:", "сторона A:").replace(
            "client:", "сторона B:")
        recheck = analyzer.screen_call(
            text, row["active_names"] or "", api_key=settings.openai_api_key,
            model=settings.analysis_model, own_company=settings.own_company)
        if not recheck["is_request"]:
            problems.append("по полной записи запроса от клиента не видно"
                            + (": технику искал наш менеджер"
                               if recheck["asked_by"] == "our_manager" else ""))

        # 2. Дубль: другая заявка контакта вокруг того же звонка.
        if row["contact_id"]:
            try:
                payload = client.get(f"contacts/{row['contact_id']}/orders",
                                     sort="-created-at", per_page=20)
                same_day = [
                    str((o.get("attributes") or {}).get("name") or "")
                    for o in payload.get("data") or []
                    if str(o["id"]) != str(order_id)
                    and str((o.get("attributes") or {}).get("created-at") or "")[:10]
                    == row["started_at"][:10]
                ]
                if same_day:
                    problems.append(f"возможный дубль: у клиента в тот же день «{same_day[0][:40]}»")
            except (httpx.HTTPError, ValueError):
                pass

        # 3. Тип техники.
        if not state["transport"]:
            picked = analyzer.pick_transport_type(
                text, screen.get("equipment", ""), list(types),
                api_key=settings.openai_api_key, model=settings.analysis_model)
            if picked and args.fix:
                try:
                    client.patch(f"orders/{order_id}/relationships/transport-type",
                                 {"data": {"type": "transport-types", "id": types[picked]}})
                    fixed.append(f"тип техники → {picked}")
                except (httpx.HTTPError, ValueError) as exc:
                    problems.append(f"тип техники не встал: {exc}")
            elif picked:
                problems.append(f"тип техники пуст, подошёл бы «{picked}»")
            else:
                problems.append("тип техники не определить по записи")

        # 4. Ответственный и соисполнитель.
        if settings.crm_lead_responsible and settings.crm_lead_responsible not in state["users"]:
            if args.fix and writer.set_responsible(order_id, settings.crm_lead_responsible):
                fixed.append("ответственный")
            else:
                problems.append("ответственный не служебная учётка")
        if row["synergy_user"] and str(row["synergy_user"]) not in state["users"]:
            if args.fix and writer.add_performer(order_id, row["synergy_user"]):
                fixed.append("соисполнитель")
            else:
                problems.append(f"нет соисполнителя ({row['display_name']})")

        # 5. Выжимка и расшифровка.
        if not state["summary"].strip():
            if args.fix and writer.set_summary(
                order_id, lead_summary(dict(row), screen, {"summary": screen.get("request", "")})
            ):
                fixed.append("выжимка")
            else:
                problems.append("выжимка пуста")
        if not state["transcript"].strip():
            problems.append("расшифровка в заявке пуста")

        mark = "  ".join(problems) if problems else "в порядке"
        trouble += bool(problems)
        print(f"  №{state['number']} ({order_id}) {row['started_at'][11:16]} "
              f"{(row['display_name'] or '')[:20]:20} — {mark}"
              + (f" | починено: {', '.join(fixed)}" if fixed else ""))

    conn.close()
    print(f"с замечаниями: {trouble} из {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
