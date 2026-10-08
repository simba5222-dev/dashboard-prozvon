#!/usr/bin/env python
"""Удалить старые записи разговоров — но только уже разобранные.

    sudo -u claude .venv/bin/python scripts/cleanup_old_records.py            # показать
    sudo -u claude .venv/bin/python scripts/cleanup_old_records.py --apply    # удалить

Запись звонка нужна, пока её не досмотрели все три конвейера: просев входящих
(`screens`), разбор прозвона (`transcripts.analysis_json`), сверка поиска
техники (`search_checks`). Как только хотя бы один применимый к звонку
результат есть в базе — сам звук больше не нужен, транскрипт и разбор уже
легли в базу навсегда.

08.10.2026: владелец нашёл, что записи зеркалятся на оба сервера без чистки
(растёт `/opt/backups/ams` на боевом). Решение владельца — удалять старое,
но строго разобранное: звонок без записи, который почему-то не долетел до
конца конвейера, трогать нельзя, иначе причина никогда не найдётся.

Файл `{uid}.mp3` без ЛЮБОГО следа обработки в базе пропускается всегда,
независимо от возраста — значит он либо ещё не дошёл до разбора, либо
вовсе не относится ни к одному из трёх потоков (тогда его трогать не наше
дело). Про поток 1 (боевой сервер) эта чистка ничего не знает: там разбор
отмечается не локально, а записью в Synergy, и без обращения в CRM по
каждому звонку такой проверки добросовестно не сделать.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings   # noqa: E402
from app.db import connect, init_schema   # noqa: E402

logger = logging.getLogger("cleanup_records")


def is_processed(conn, uid: str) -> bool:
    row = conn.execute(
        """SELECT
             EXISTS(SELECT 1 FROM screens WHERE call_uid = ?) OR
             EXISTS(SELECT 1 FROM transcripts WHERE call_uid = ?
                    AND analysis_json IS NOT NULL AND analysis_json != '') OR
             EXISTS(SELECT 1 FROM search_checks WHERE call_uid = ?)
        """,
        (uid, uid, uid),
    ).fetchone()
    return bool(row[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=60, help="старше скольких дней")
    ap.add_argument("--apply", action="store_true", help="действительно удалить")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)
    records = Path(settings.records_dir)

    cutoff = time.time() - args.days * 86400
    old = [p for p in records.glob("*.mp3") if p.stat().st_mtime < cutoff]
    if not old:
        print(f"записей старше {args.days} дней нет")
        return 0

    deleted = kept_unprocessed = 0
    for path in old:
        uid = path.stem
        if is_processed(conn, uid):
            if args.apply:
                path.unlink()
            deleted += 1
        else:
            kept_unprocessed += 1
            logger.info("%s: старше %s дней, но следов разбора в базе нет — не трогаю",
                        uid, args.days)

    conn.close()
    print(f"старше {args.days} дней: {len(old)}, разобрано и " +
          ("удалено" if args.apply else "годится к удалению") +
          f" {deleted}, оставлено без разбора {kept_unprocessed}" +
          ("" if args.apply else "   (сухой прогон, ничего не удалено)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
