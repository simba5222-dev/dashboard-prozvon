#!/usr/bin/env python
"""Выгрузка заявок из Synergy для отчёта по рекламным линиям (только чтение).

    sudo -u claude .venv/bin/python scripts/ad_report_fetch_orders.py --since 2026-01-01

Листает `/orders` по `-created-at` (фильтры корневого списка Synergy тихо игнорирует, поэтому только
сортировка и ранний стоп), пока не дойдёт до `--since`. Пишет `data/ad_report/orders.jsonl`: по строке на заявку —
номер линии (custom-278), телефон клиента (custom-276), метка (custom-26243), тип транспорта, стадия,
причина проигрыша (custom-27969), сумма, дата создания. Ответственного не берём: на списке он приходит null.
Повторный запуск начинает заново (выгрузка укладывается в минуты).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient, load_stages  # noqa: E402
from app.config import Settings  # noqa: E402

MSK = timezone(timedelta(hours=3))


def listed(value) -> str:
    """Значение поля-списка Synergy: ["Реклама"] -> Реклама."""
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v not in (None, ""))
    return str(value)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--max-pages", type=int, default=3000)
    args = ap.parse_args()

    settings = Settings()
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token, timeout_sec=60.0,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries)
    stages = load_stages(client)
    boundary = datetime.fromisoformat(args.since).replace(tzinfo=MSK)
    out_dir = Path(settings.db_path).parent / "ad_report"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "orders.jsonl"
    started = time.monotonic()
    total = 0
    with path.open("w", encoding="utf-8") as fh:
        for page in range(1, args.max_pages + 1):
            payload = client.get("orders", sort="-created-at", include="stage,transport-type,loss-reason,contact",
                                 **{"page[number]": page})
            rows = payload.get("data") or []
            if not rows:
                break
            inc = {(i["type"], str(i["id"])): (i.get("attributes") or {}) for i in payload.get("included") or []}
            stop = False
            for row in rows:
                a = row.get("attributes") or {}
                raw = str(a.get("created-at") or "")
                try:
                    created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if created < boundary:
                    stop = True
                    break
                rel = row.get("relationships") or {}
                ref = lambda name: (rel.get(name) or {}).get("data") or {}  # noqa: E731
                stage_id = str(ref("stage").get("id") or "")
                stage_name, stage_kind = stages.get(stage_id, ("", ""))
                tt = ref("transport-type")
                tt_name = (inc.get(("transport-types", str(tt.get("id"))), {}) or {}).get("name") if tt else ""
                lr = ref("loss-reason")
                lr_name = (inc.get((lr.get("type"), str(lr.get("id"))), {}) or {}).get("name") if lr else ""
                ct = ref("contact")
                cta = (inc.get((ct.get("type"), str(ct.get("id"))), {}) or {}) if ct else {}
                # телефон клиента: custom-276 в карточке часто пуст (до апреля 2026 почти всегда) — тогда из контакта
                contact_phone = next((str(cta[k]) for k in ("general-phone", "mobile-phone", "work-phone", "other-phone") if cta.get(k)), "")
                c = a.get("customs") or {}
                fh.write(json.dumps({
                    "id": str(row["id"]), "number": a.get("number"), "name": a.get("name"),
                    "created_at": raw, "line": c.get("custom-278"), "client_phone": c.get("custom-276"), "contact_phone": contact_phone,
                    "mark": listed(c.get("custom-26243")), "source_mark": listed(c.get("custom-28739")),
                    "region": listed(c.get("custom-29475")),
                    "transport_field": listed(c.get("custom-18621")), "transport_rel": tt_name or "",
                    "stage": stage_name, "stage_kind": stage_kind,
                    "loss_field": listed(c.get("custom-27969")), "loss_rel": lr_name or "",
                    "amount": a.get("amount"), "adv_source": a.get("adv-source"), "channel": a.get("channel"),
                }, ensure_ascii=False) + "\n")
                total += 1
            fh.flush()
            if page % 20 == 0:
                print(f"стр. {page}, заявок {total}, последняя {raw[:16]}, {time.monotonic()-started:.0f} с", flush=True)
            if stop:
                break
    print(f"ГОТОВО: заявок {total} с {args.since}, {time.monotonic()-started:.0f} с → {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
