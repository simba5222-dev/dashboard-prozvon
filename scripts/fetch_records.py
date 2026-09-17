#!/usr/bin/env python
"""Скачать записи разговоров через российский сервер.

    scripts/fetch_records.py --days 14           за две недели
    scripts/fetch_records.py --days 14 --limit 50

ВАТС МегаФон не отдаёт записи на зарубежные адреса, поэтому запись тянет
российский сервер (у него в `.env` логин и пароль к хранилищу записей), а сюда
она приезжает по ssh потоком.

Ключ к тому серверу читает только пользователь `agent`, поэтому качать надо
под ним:

    ./scripts/fetch_records.py --days 14

Запись кладётся в `data/records/<uid>.mp3`, повторный запуск пропускает уже
скачанное. Дальше разбором занимается `analyze_calls.py` — он работает под
`claude`, потому что пишет в базу.
"""

from __future__ import annotations

import argparse
import shlex
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402


def settings_without_secrets() -> Settings:
    """Настройки для запуска под `agent`.

    `.env` дашборда читает только `claude` — там токены Synergy и OpenAI.
    Скачиванию записей они не нужны: путь к базе, каталог записей и адрес
    российского сервера берутся из значений по умолчанию и переменных
    окружения. Если `.env` вдруг прочитается — пользуемся им.
    """
    try:
        return get_settings()
    except PermissionError:
        return Settings(_env_file=None)

# Скачивание идёт командой на той стороне: логин и пароль к записям лежат в
# `.env` российского сервера, и вытаскивать их сюда незачем.
REMOTE = (
    "cd /opt/asr && set -a && . ./.env && set +a && "
    'curl -sS --fail --max-time 120 -u "$ASR_MEGAFON_RECORD_LOGIN:$ASR_MEGAFON_RECORD_PASSWORD" {url}'
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Скачать записи разговоров с российского сервера.")
    ap.add_argument("--days", type=int, default=1, help="за сколько последних дней")
    ap.add_argument("--day", help="по какую дату, ГГГГ-ММ-ДД (по умолчанию сегодня)")
    ap.add_argument("--limit", type=int, default=0, help="не больше стольких записей")
    ap.add_argument("--min-sec", type=int, default=0,
                    help="пропускать разговоры короче, секунд (0 — порог из настроек)")
    ap.add_argument("--leads", action="store_true",
                    help="только входящие отдела продаж: их разбирает поиск "
                         "потерянных заявок. Без этого качается всё подряд, а "
                         "в базе теперь лежат все звонки компании — это гигабайты")
    args = ap.parse_args()

    settings = settings_without_secrets()
    if not settings.record_ssh_host or not settings.record_ssh_key:
        print("не задано, откуда качать записи: DASH_RECORD_SSH_HOST / DASH_RECORD_SSH_KEY")
        return 1
    if not Path(settings.record_ssh_key).exists():
        print(f"ключ {settings.record_ssh_key} недоступен — запускайте под пользователем agent")
        return 1

    until = args.day or date.today().isoformat()
    since = (date.fromisoformat(until) - timedelta(days=max(args.days - 1, 0))).isoformat()
    min_sec = args.min_sec or settings.transcribe_min_duration_sec or settings.talk_threshold_sec

    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    where = ["local_date BETWEEN ? AND ?", "duration_sec >= ?",
             "record_url IS NOT NULL", "record_url <> ''"]
    params: list = [since, until, min_sec]
    if args.leads:
        where.append("direction = 'in'")
        where.append("vats_login IN (SELECT vats_login FROM managers "
                     "WHERE dept = ? AND active = 1)")
        params.append(settings.sales_dept)
    rows = conn.execute(
        f"""
        SELECT uid, record_url, duration_sec, local_date FROM calls
        WHERE {' AND '.join(where)}
        ORDER BY started_at DESC
        """,
        params,
    ).fetchall()
    conn.close()

    target = Path(settings.records_dir)
    target.mkdir(parents=True, exist_ok=True)
    todo = [r for r in rows if not (target / f"{r['uid']}.mp3").exists()]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{since}…{until}: разговоров {len(rows)}, качать {len(todo)}")

    done = failed = 0
    for row in todo:
        path = target / f"{row['uid']}.mp3"
        command = [
            "ssh", "-i", settings.record_ssh_key, "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15", settings.record_ssh_host,
            REMOTE.format(url=shlex.quote(row["record_url"])),
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            print(f"  {row['uid']}: не дождались записи")
            failed += 1
            continue
        # Пустой ответ — это не запись: пишем файл только когда что-то приехало,
        # иначе повторный запуск сочтёт звонок скачанным.
        if result.returncode != 0 or len(result.stdout) < 1024:
            print(f"  {row['uid']}: не отдалась ({result.stderr.decode()[:80].strip()})")
            failed += 1
            continue
        path.write_bytes(result.stdout)
        done += 1
        if done % 25 == 0:
            print(f"  скачано {done} из {len(todo)}")

    print(f"скачано {done}, не отдалось {failed}, всего в {target}: "
          f"{len(list(target.glob('*.mp3')))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
