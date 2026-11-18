#!/usr/bin/env python
"""Разбор почты info@ на заявки об аренде техники и поставке нерудных материалов.

    sudo -u claude .venv/bin/python scripts/mail_intake.py --apply
    ... --limit 5          максимум писем за один проход (по умолчанию 20)
    ... (без --apply)      сухой прогон — только показать, что бы сделали

Проверяет контакт по телефону из письма; нет — заводит. Вложения из письма
в заявку не кладёт — у Synergy нет рабочего способа загрузить файл через
публичный API (решение владельца 15.10.2026, см. app/mail_intake.py).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import mail_intake  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_schema  # noqa: E402

logger = logging.getLogger("mail-intake")


def main() -> int:
    ap = argparse.ArgumentParser(description="Заявки с почты info@.")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--apply", action="store_true",
                    help="заводить контакты/заявки в CRM; без него — сухой прогон")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)

    result = mail_intake.run(conn, settings, apply=args.apply, limit=args.limit)
    if "error" in result:
        logger.error(result["error"])
        return 1

    logger.info(
        "проверено писем: %s, заявок похоже: %s, заведено: %s",
        result["checked"], result["requests"], result["orders"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
