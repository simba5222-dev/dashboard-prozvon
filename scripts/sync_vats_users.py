#!/usr/bin/env python
"""Сопоставить сотрудников с их добавочными номерами в ВАТС.

    ./scripts/sync_vats_users.py --fetch                      под agent: забрать список
    sudo -u claude .venv/bin/python scripts/sync_vats_users.py --apply   записать

Два шага, потому что работают под разными пользователями: список ВАТС
достаёт российский сервер, а ключ к нему читает только `agent`; базу же
`agent` видит только на чтение. Между шагами список лежит в
`data/vats-users.json`.

Зачем. Synergy кладёт в исходящий звонок **добавочный** номер, а не мобильный,
и отдельным полем пишет, чей он. Это поле врёт, когда добавочный
переиспользуют: 23.09.2026 добавочный 766 числился за Ратенковым, хотя там уже
работал Никитин, и 58 его звонков за день ушли в чужой счёт.

ВАТС знает правду: у неё логин, имя, мобильный и добавочный лежат одной
строкой. Сопоставляем по мобильному — он у человека свой и приходит из
Synergy вместе с карточкой сотрудника.

Запускать под `agent`: список ВАТС достаёт российский сервер, ключ к нему
читает только он.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings, get_settings  # noqa: E402
from app.db import connect  # noqa: E402

# Скрипт на боевом: одна команда, чтобы не гонять ключ ВАТС сюда.
REMOTE = r'''cd /opt/asr && ./.venv/bin/python - <<'PYEOF'
import json, httpx
from dotenv import dotenv_values
env = dotenv_values(".env")
r = httpx.get(env["ASR_MEGAFON_VATS_URL"].rstrip("/") + "/users",
              headers={"X-API-KEY": env["ASR_MEGAFON_API_TOKEN"]},
              params={"limit": 500}, timeout=60)
data = r.json()
print(json.dumps(data.get("items") if isinstance(data, dict) else data, ensure_ascii=False))
PYEOF'''


def settings_without_secrets() -> Settings:
    """Настройки под `agent`: `.env` дашборда читает только `claude`.

    Здесь нужны лишь путь к базе и адрес российского сервера — они есть в
    значениях по умолчанию. Токен ВАТС не нужен вовсе: за списком ходит сам
    российский сервер, у него свой.
    """
    try:
        return get_settings()
    except PermissionError:
        return Settings(_env_file=None)


def digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))[-10:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true",
                    help="забрать список сотрудников из ВАТС в data/vats-users.json")
    ap.add_argument("--apply", action="store_true", help="записать добавочные в базу")
    args = ap.parse_args()

    settings = settings_without_secrets()
    cache = Path(settings.db_path).parent / "vats-users.json"

    if not args.fetch:
        if not cache.exists():
            print(f"нет {cache} — сначала запустите с --fetch под agent")
            return 1
        users = json.loads(cache.read_text(encoding="utf-8"))
        return apply_users(settings, users, args.apply)

    out = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
         "-i", settings.record_ssh_key, settings.record_ssh_host, REMOTE],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        print("ВАТС не ответила:", (out.stderr or "").strip()[:200])
        return 1
    try:
        users = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print("ответ ВАТС не разобрался")
        return 1
    cache.write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")
    print(f"список сохранён: {cache}")
    return apply_users(settings, users, args.apply)


def apply_users(settings, users, write: bool) -> int:
    by_phone = {digits(u.get("telnum")): u for u in users if digits(u.get("telnum"))}
    print(f"в ВАТС сотрудников: {len(users)}, с мобильными: {len(by_phone)}")

    conn = connect(settings.db_path)
    rows = conn.execute(
        "SELECT vats_login, display_name, dept, phone, ext FROM managers "
        "WHERE phone IS NOT NULL AND phone <> ''"
    ).fetchall()
    changed = 0
    for row in rows:
        user = by_phone.get(digits(row["phone"]))
        if not user:
            print(f"  {row['display_name']}: в ВАТС по номеру не нашёлся")
            continue
        ext = str(user.get("ext") or "").strip()
        if not ext or ext == (row["ext"] or ""):
            continue
        changed += 1
        print(f"  {row['display_name']} ({row['dept']}): добавочный "
              f"{row['ext'] or '—'} → {ext}")
        if write:
            conn.execute("UPDATE managers SET ext = ? WHERE vats_login = ?",
                         (ext, row["vats_login"]))
    if write:
        conn.commit()
        print(f"\nзаписано: {changed}")
    else:
        print(f"\nсухой прогон: изменилось бы {changed}. Повторите с --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
