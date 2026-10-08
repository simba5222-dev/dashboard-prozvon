#!/usr/bin/env python
"""Поток 1 по каждому из семи каналов отдельно — не общей цифрой.

    sudo -u claude .venv/bin/python scripts/ad_lines_watchdog.py            # сухой прогон
    sudo -u claude .venv/bin/python scripts/ad_lines_watchdog.py --apply    # отправить в канал
    ... --hours 3      окно проверки (по умолчанию 3 — просьба владельца 08.10.2026)

Общий сторож (`stream_watchdog.py`) даёт один ответ «поток 1 в порядке/не в
порядке» на всю рекламу сразу. Здесь — по каждой линии отдельно: если
протекает одна конкретная линия (а не весь поток), общий сторож этого не
покажет, а этот отчёт — покажет.

Числа считаются так же, как в `app/daily_report.py`: «звонков» — из истории
ВАТС напрямую (поле `via`), «транскрибировано»/«разобрано» — из журнала
боевого сервиса, но здесь не агрегатом, а по каждому call_uid, чтобы
разложить по линиям.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx                                       # noqa: E402

from app.config import get_settings                 # noqa: E402
from app.db import connect, init_schema              # noqa: E402
from scripts.stream_watchdog import send_telegram     # noqa: E402

logger = logging.getLogger("ad_lines_watchdog")

PROD_HOST = "root@91.220.109.49"
PROD_KEY = "/home/agent/.ssh/ru-proxy_ed25519"

TRANSCRIBED_RE = re.compile(r"звонок (\S+) обработан:")
ANALYZED_RE = re.compile(r"(?:звонок (\S+) — .*заявку не трогаем|звонок (\S+) -> контакт)")


def _line_names(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT phone10, COALESCE(owner_name, name, '') AS label FROM lines "
        "WHERE in_scope = 1 AND (is_general = 1 OR kind = 'рекламная')"
    ).fetchall()
    return {r["phone10"]: (r["label"] or r["phone10"]) for r in rows}


def _fetch_vats_rows(settings, since_iso: str, until_iso: str) -> list[dict] | None:
    if not settings.prod_lookup_url or not settings.prod_lookup_token:
        return None
    try:
        r = httpx.get(
            f"{settings.prod_lookup_url}/lookup-records",
            params={"start": since_iso, "end": until_iso, "with_record_only": "false"},
            headers={"X-Lookup-Token": settings.prod_lookup_token}, timeout=30,
        )
        r.raise_for_status()
        return r.json()["rows"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.error("история ВАТС не получена: %s", exc)
        return None


def _fetch_journal(since_utc: str, until_utc: str) -> str | None:
    script = (
        f"journalctl -u asr --no-pager --since '{since_utc} UTC' "
        f"--until '{until_utc} UTC' 2>/dev/null | grep -E "
        "'обработан:|заявку не трогаем|-> контакт'"
    )
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             "-i", PROD_KEY, PROD_HOST, script],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode not in (0, 1):  # 1 = grep ничего не нашёл, это не ошибка
        return None
    return out.stdout


def build_text(conn, settings, hours: int) -> str | None:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    rows = _fetch_vats_rows(settings, since.isoformat(), now.isoformat())
    log = _fetch_journal(since.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S"))
    if rows is None or log is None:
        return None

    transcribed_uids = set(TRANSCRIBED_RE.findall(log))
    analyzed_uids = {u for pair in ANALYZED_RE.findall(log) for u in pair if u}

    names = _line_names(conn)
    per_line = {phone10: {"calls": 0, "transcribed": 0, "analyzed": 0} for phone10 in names}
    for row in rows:
        phone10 = (row.get("via") or "")[-10:]
        if phone10 not in per_line:
            continue
        uid = row.get("uid")
        d = per_line[phone10]
        d["calls"] += 1
        if uid in transcribed_uids:
            d["transcribed"] += 1
        if uid in analyzed_uids:
            d["analyzed"] += 1

    out = [f"📡 Поток 1 по каналам за {hours} ч (до {now.strftime('%H:%M')} UTC)"]
    total_calls = total_transcribed = total_pending = 0
    for phone10, name in sorted(names.items(), key=lambda kv: kv[1] or "я"):
        d = per_line[phone10]
        pending = max(d["calls"] - d["transcribed"], 0)
        total_calls += d["calls"]
        total_transcribed += d["transcribed"]
        total_pending += pending
        out.append(f"  {name or phone10}: звонков {d['calls']}, "
                  f"транскрибировано {d['transcribed']}, в разборе {pending}")
    out.append(f"\nВсего: звонков {total_calls}, транскрибировано {total_transcribed}, "
              f"в разборе {total_pending}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=3, help="окно проверки, часов")
    ap.add_argument("--apply", action="store_true", help="отправить в канал")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn)

    text = build_text(conn, settings, args.hours)
    conn.close()
    if text is None:
        logger.error("не собралось — боевой сервер или история ВАТС не ответили")
        return 1

    print(text)
    if args.apply:
        if not (settings.watchdog_bot_token and settings.watchdog_chat_id):
            logger.error("нет токена/chat_id сторожа — .env читается только под claude")
            return 1
        send_telegram(settings.watchdog_bot_token, settings.watchdog_chat_id, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
