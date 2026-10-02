#!/usr/bin/env python
"""Собрать замечания коллег из поля «Коммент для ИИ» и превратить их в эталон.

    sudo -u claude .venv/bin/python scripts/read_ai_notes.py          показать
    sudo -u claude .venv/bin/python scripts/read_ai_notes.py --apply  записать в эталон

Владелец завёл в CRM поле `custom-30618`: если заявка, заведённая разбором,
оказалась лишней, коллега пишет туда, почему это не заявка. Это обратная
связь от людей, которые видят весь контекст, — и она дороже любой нашей
догадки.

Каждое такое замечание — строка эталона `checkset/truth-screen.json`: звонок,
из которого выросла заявка, помечается как «не заявка» со словами коллеги.
Дальше `score_trial.py` проверяет на нём любую правку промпта.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402

ПОЛЕ = "custom-30618"
ЭТАЛОН = Path(__file__).resolve().parents[1] / "checkset" / "truth-screen.json"
logger = logging.getLogger("замечания")


def main() -> int:
    ap = argparse.ArgumentParser(description="Замечания коллег из поля «Коммент для ИИ».")
    ap.add_argument("--apply", action="store_true", help="дописать в эталон")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    s = get_settings()
    conn = connect(s.db_path)
    client = SynergyClient(base_url=s.synergy_url, token=s.synergy_api_token,
                           timeout_sec=30.0, min_interval_sec=s.synergy_min_interval_sec,
                           retries=s.synergy_retries)

    # Заявки, которые завёл разбор: по ним и ждём замечаний.
    наши = {r["created_order_id"]: r["call_uid"] for r in conn.execute(
        """SELECT s.created_order_id, s.call_uid FROM screens s
           JOIN calls k ON k.uid = s.call_uid
           WHERE COALESCE(s.created_order_id,'') <> ''
             AND k.local_date >= date('now', ?)""", (f"-{args.days} days",))}
    if not наши:
        print("заявок, заведённых разбором, за период нет")
        return 0

    найдено = []
    for order_id, call_uid in наши.items():
        try:
            a = (client.get(f"orders/{order_id}").get("data") or {}).get("attributes") or {}
        except Exception as exc:  # noqa: BLE001 — одна недоступная заявка не повод падать
            logger.warning("заявка %s не прочиталась: %s", order_id, exc)
            continue
        замечание = str((a.get("customs") or {}).get(ПОЛЕ) or "").strip()
        if замечание:
            найдено.append((call_uid, order_id, замечание))

    print(f"наших заявок за {args.days} дней: {len(наши)}, с замечанием: {len(найдено)}\n")
    for call_uid, order_id, текст in найдено:
        print(f"   заявка {order_id} (звонок {call_uid})")
        print(f"      «{текст[:150]}»")
    if not найдено:
        print("замечаний пока нет — коллеги ещё не писали")
        return 0
    if not args.apply:
        print("\nничего не записано. Чтобы дописать в эталон: --apply")
        return 0

    d = json.loads(ЭТАЛОН.read_text(encoding="utf-8"))
    уже = {x["uid"] for x in d["звонки"]}
    добавлено = 0
    for call_uid, order_id, текст in найдено:
        if call_uid in уже:
            continue
        d["звонки"].append({
            "uid": call_uid, "заявка": False,
            "слова_владельца": текст,
            "откуда": f"поле «Коммент для ИИ» в заявке {order_id}",
        })
        добавлено += 1
    ЭТАЛОН.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nдописано в эталон: {добавлено}, всего в нём {len(d['звонки'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
