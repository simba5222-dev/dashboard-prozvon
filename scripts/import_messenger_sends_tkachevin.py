#!/usr/bin/env python
"""Разовый импорт: отправки презентации/списка техники через личные
мессенджеры Ткачевина (Telegram, MAX), разобрано 12.10.2026 через VNC-браузер.

Не постоянный сборщик — данные руками/разбором переписки, а не живым
потоком. Запускать повторно безопасно: перед вставкой чистит старые строки
этого же менеджера/платформы (clear_messenger_sends), затем вставляет заново
— имена получателей не уникальны, дедуплицировать по ним нельзя.

Данные по MAX читались из `data/_tmp_max_parsed.json` (806 строк,
распарсено из DOM `.searchResultsList` через debug-порт браузера) — файл
одноразовый, после импорта удалён. Этот скрипт оставлен как исторический
снимок разбора на 12.10.2026, повторный запуск без того файла упадёт на
json.loads — это ожидаемо, не баг.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/home/claude/dashboard")

from app.config import get_settings
from app.db import clear_messenger_sends, connect, init_schema, save_messenger_send

MANAGER = "ткачевин"
FILE_NAME = "Техника.xlsx"  # маркер отправки — три файла уходят вместе

TELEGRAM = [
    ("Сергей", "2026-10-12"),
    ("Вадим", "2026-10-08"),
    ("Тигран", "2026-10-08"),
    ("Поднимают лодки", "2026-10-08"),
    ("Гаро", "2026-10-07"),
    ("Александр", "2026-10-06"),
    ("Ирина In-pro Proizvod", "2026-10-05"),
    ("Сергей", "2026-09-25"),
    ("Калерия", "2026-09-25"),
    ("Юрий", "2026-09-24"),
    ("Артём", "2026-09-24"),
    ("Владислав", "2026-09-24"),
    ("Михаил", "2026-09-23"),
    ("Илья Сергеевич", "2026-09-23"),
    ("Руслан", "2026-09-23"),
]


def main() -> int:
    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)

    collected_at = datetime.now(timezone.utc).isoformat()
    clear_messenger_sends(conn, manager_login=MANAGER, platform="telegram")
    clear_messenger_sends(conn, manager_login=MANAGER, platform="max")
    added = 0

    for recipient, sent_date in TELEGRAM:
        save_messenger_send(
            conn, manager_login=MANAGER, platform="telegram", recipient=recipient,
            sent_date=sent_date, file_name=FILE_NAME, collected_at=collected_at,
        )
        added += 1

    max_rows = json.loads(Path("/home/claude/dashboard/data/_tmp_max_parsed.json").read_text())
    for row in max_rows:
        save_messenger_send(
            conn, manager_login=MANAGER, platform="max", recipient=row["recipient"],
            sent_date=row["sent_date"], file_name=FILE_NAME, collected_at=collected_at,
        )
        added += 1

    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM messenger_file_sends WHERE manager_login=?", (MANAGER,)
    ).fetchone()[0]
    print(f"обработано записей: {added}, всего в базе для {MANAGER}: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
