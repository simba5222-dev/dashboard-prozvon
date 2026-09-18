#!/usr/bin/env python
"""Забрать пометки владельца из CRM и положить их в проверочный набор.

    sudo -u claude .venv/bin/python scripts/learn_from_crm.py          показать
    sudo -u claude .venv/bin/python scripts/learn_from_crm.py --apply  записать

Владелец слушает заявки, которые завела машина, и помечает ошибочные: стадия
«Сделка провалена», комментарий к поражению — «ошибка ИИ» (заявки не было) или
«дубль ИИ» (заявка уже была заведена менеджером).

Это самая ценная разметка, какая бывает: правильный ответ от того, кто знает
дело. Скрипт находит по каждой пометке исходный звонок и кладёт его в набор
`checkset/inbound-screening.json` — дальше правка правил проверяется цифрой.

Пометки владельца важнее моей разметки: при расхождении побеждает его.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient, load_stages  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402

logger = logging.getLogger("learn")

CHECKSET = Path(__file__).resolve().parents[1] / "checkset" / "inbound-screening.json"

# Как пометка владельца ложится в набор.
MARKS = {
    "ошибка ии": ("no_request", "owner_wrong", "владелец: заявки не было"),
    "дубль ии": ("request", "duplicate", "владелец: заявка уже была заведена менеджером"),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Пометки владельца из CRM в набор.")
    ap.add_argument("--pages", type=int, default=5, help="сколько страниц заявок смотреть")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
    stages = load_stages(client)

    rows = []
    for page in range(1, args.pages + 1):
        chunk = client.get("orders", sort="-created-at", per_page=100, page=page,
                           include="stage").get("data") or []
        if not chunk:
            break
        rows += chunk

    conn = connect(settings.db_path)
    by_order = {r["created_order_id"]: r["call_uid"] for r in conn.execute(
        "SELECT created_order_id, call_uid FROM screens WHERE created_order_id IS NOT NULL")}
    conn.close()

    doc = json.loads(CHECKSET.read_text(encoding="utf-8"))
    items = {i["uid"]: i for i in doc["items"]}

    added = changed = skipped = 0
    for row in rows:
        attrs = row.get("attributes") or {}
        if settings.crm_lead_order_name.lower() not in str(attrs.get("name") or "").lower():
            continue
        stage_id = str((((row.get("relationships") or {}).get("stage") or {}).get("data") or {}).get("id") or "")
        if stages.get(stage_id, ("", ""))[1] != "lost":
            continue
        mark = str(attrs.get("loss-comment") or "").strip().lower()
        if mark not in MARKS:
            continue
        uid = by_order.get(row["id"])
        if not uid:
            logger.warning("заявка %s помечена «%s», но звонок не найден", row["id"], mark)
            continue

        label, kind, note = MARKS[mark]
        full_note = f"{note} (заявка {row['id']})"
        current = items.get(uid)
        if current is None:
            items[uid] = {"uid": uid, "label": label, "kind": kind, "note": full_note}
            added += 1
            print(f"+ {uid}: {label} [{kind}] — {note}")
        elif current["label"] != label:
            print(f"~ {uid}: было {current['label']}, стало {label} — пометка владельца важнее")
            current.update({"label": label, "note": full_note})
            if not current.get("kind"):
                current["kind"] = kind
            changed += 1
        elif not current.get("kind"):
            # Ответ совпал, а вид ошибки не назван — подставляем общий.
            current["kind"] = kind
            changed += 1
        else:
            skipped += 1

    doc["items"] = sorted(items.values(), key=lambda x: x["uid"])
    doc["разговоров"] = len(doc["items"])
    doc["виды ошибок"].setdefault("owner_wrong", "владелец прослушал и сказал: заявки не было")
    doc["виды ошибок"].setdefault("duplicate", "запрос был, но заявку менеджер уже завёл")
    if args.apply:
        CHECKSET.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    positive = sum(1 for i in doc["items"] if i["label"] == "request")
    print(f"\n{'записано' if args.apply else 'сухой прогон'}: добавлено {added}, "
          f"исправлено {changed}, уже было {skipped}")
    print(f"в наборе {len(doc['items'])}: запросов {positive}, не запросов {len(doc['items']) - positive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
