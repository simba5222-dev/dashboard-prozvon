#!/usr/bin/env python
"""Загрузить порцию звонков для обучения в базу (страница /training).

    sudo -u claude .venv/bin/python scripts/training_import.py --batch 1

Файл `data/training/batch<N>.json` готовит `training_batch.py` под agent. Повторный
запуск обновляет черновики, но уже исправленный человеком текст не трогает.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, save_training_item  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", type=int, required=True)
    args = ap.parse_args()

    settings = Settings()
    path = Path(settings.db_path).parent / "training" / f"batch{args.batch}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    conn = connect(settings.db_path)
    init_schema(conn)
    for position, item in enumerate(data["items"], 1):
        save_training_item(
            conn, call_uid=item["uid"], batch=data["batch"], position=position,
            source=item["source"], direction=item["direction"],
            duration_sec=item["duration_sec"], started_at=item["started_at"],
            manager=item["manager"],
            draft_json=json.dumps(item["segments"], ensure_ascii=False))
    conn.commit()
    total = sum(i["duration_sec"] for i in data["items"])
    print(f"порция {data['batch']}: загружено {len(data['items'])} звонков, {total / 60:.1f} мин")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
