#!/usr/bin/env python
"""Сторож потоков 2, 3 и 4 — проверка за последние полчаса, не за сутки.

    sudo -u claude .venv/bin/python scripts/stream_watchdog.py            # сухой прогон
    sudo -u claude .venv/bin/python scripts/stream_watchdog.py --apply    # слать алерты

Проверяет не «работает ли сервис», а «дошёл ли звонок до конца конвейера» —
то, что `systemctl is-active` не видит в принципе (см. историю 21.09 и 23.09:
сервис был «active», а работа молча стояла).

Окно с задержкой: берём звонки в возрасте 30–45 минут, а не последние 15 —
конвейеру нужно время на распознавание и разбор, иначе сторож будет слать
ложную тревогу на каждый живой звонок, который просто ещё не досчитался.

Три проверки:
1. Вспышка однотипных ошибок в своём же журнале (`journalctl -u dashboard`) —
   ловит отказ внешнего сервиса (как 06.10: credit_balance_exhausted) за
   15 минут, а не за сутки.
2. Поток 2 — входящие на прямые номера: звонки есть, а просева (`screens`) нет.
3. Поток 3 — прозвон: расшифровка есть, а разбор (`analysis_json`) пуст.
4. Поток 4 — поиск техники: состоявшиеся звонки поставщикам есть,
   а `search_checks` нет.

Повтор не спамит: один и тот же алерт не шлётся чаще, чем раз в 2 часа,
пока причина не исчезла (таблица `watchdog_state`).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx                                     # noqa: E402

from app.config import get_settings               # noqa: E402

logger = logging.getLogger("stream_watchdog")

ОКНО_ЗАДЕРЖКИ_МИН = 30     # не трогаем звонки свежее этого — им ещё считаться
ОКНО_ШИРИНА_МИН = 15       # ширина проверяемого окна
ПОВТОР_ЧЕРЕЗ_ЧАСОВ = 2     # не долбить одним и тем же алертом чаще


def _window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    until = now - timedelta(minutes=ОКНО_ЗАДЕРЖКИ_МИН)
    since = until - timedelta(minutes=ОКНО_ШИРИНА_МИН)
    return since.isoformat(timespec="seconds"), until.isoformat(timespec="seconds")


def _ensure_state_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS watchdog_state (
        check_key TEXT PRIMARY KEY,
        last_alert_at TEXT,
        last_ok_at TEXT
    )""")
    conn.commit()


def _should_alert(conn: sqlite3.Connection, key: str) -> bool:
    """Слать ли алерт: новая проблема или прошло больше двух часов с прошлого."""
    row = conn.execute("SELECT last_alert_at FROM watchdog_state WHERE check_key=?",
                       (key,)).fetchone()
    if row is None or row[0] is None:
        return True
    last = datetime.fromisoformat(row[0])
    return datetime.now(timezone.utc) - last > timedelta(hours=ПОВТОР_ЧЕРЕЗ_ЧАСОВ)


def _mark_alerted(conn: sqlite3.Connection, key: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("INSERT INTO watchdog_state (check_key, last_alert_at) VALUES (?,?) "
                 "ON CONFLICT(check_key) DO UPDATE SET last_alert_at=excluded.last_alert_at",
                 (key, now))
    conn.commit()


def _mark_ok(conn: sqlite3.Connection, key: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("INSERT INTO watchdog_state (check_key, last_ok_at) VALUES (?,?) "
                 "ON CONFLICT(check_key) DO UPDATE SET last_ok_at=excluded.last_ok_at",
                 (key, now))
    conn.commit()


def _recent_dashboard_log() -> str:
    """Отдельная функция ради тестов — подменяется, чтобы не дёргать journalctl."""
    try:
        return subprocess.run(
            ["journalctl", "-u", "dashboard", "--since", "-15min", "--no-pager"],
            capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("журнал не прочитался: %s", exc)
        return ""


def check_error_spike(conn: sqlite3.Connection) -> str | None:
    """Вспышка однотипных ошибок в своём журнале за последние 15 минут."""
    out = _recent_dashboard_log()
    сигнатуры = ("RateLimitError", "credit_balance_exhausted", "Traceback (most recent")
    счёт: dict[str, int] = {}
    for line in out.splitlines():
        for sig in сигнатуры:
            if sig in line:
                счёт[sig] = счёт.get(sig, 0) + 1
    частые = {k: v for k, v in счёт.items() if v >= 3}
    if not частые:
        _mark_ok(conn, "error_spike")
        return None
    детали = ", ".join(f"«{k}» × {v}" for k, v in частые.items())
    return f"⚠️ В журнале дашборда за 15 минут: {детали}. Похоже на отказ внешнего сервиса."


def check_stream2_inbound(conn: sqlite3.Connection) -> str | None:
    since, until = _window()
    calls = conn.execute(
        "SELECT COUNT(*) FROM calls WHERE direction='in' AND is_demo=0 "
        "AND started_at BETWEEN ? AND ? AND duration_sec>0", (since, until)).fetchone()[0]
    if calls == 0:
        _mark_ok(conn, "stream2"); return None
    screened = conn.execute(
        "SELECT COUNT(*) FROM screens WHERE created_at BETWEEN ? AND ?",
        (since, until)).fetchone()[0]
    if screened > 0:
        _mark_ok(conn, "stream2"); return None
    return (f"🔴 Поток 2 (входящие, ловля потерянных): за {since[11:16]}–{until[11:16]} "
            f"было {calls} состоявшихся входящих, просеяно — 0. Похоже, просев встал.")


def check_stream3_prozvon(conn: sqlite3.Connection) -> str | None:
    since, until = _window()
    cur = conn.execute(
        """SELECT c.uid FROM calls c WHERE c.direction='out' AND c.in_group=1
           AND c.is_demo=0 AND c.duration_sec>15 AND c.started_at BETWEEN ? AND ?""",
        (since, until))
    uids = [r[0] for r in cur.fetchall()]
    if not uids:
        _mark_ok(conn, "stream3"); return None
    placeholders = ",".join("?" * len(uids))
    with_analysis = conn.execute(
        f"SELECT COUNT(*) FROM transcripts WHERE call_uid IN ({placeholders}) "
        f"AND analysis_json IS NOT NULL AND analysis_json != ''", uids).fetchone()[0]
    if with_analysis > 0:
        _mark_ok(conn, "stream3"); return None
    return (f"🔴 Поток 3 (прозвон): за {since[11:16]}–{until[11:16]} было {len(uids)} "
            f"звонков, у 0 из них есть разбор содержания (оценка, рекомендации). "
            f"Расшифровка может идти, а анализ — нет.")


def check_stream4_search(conn: sqlite3.Connection) -> str | None:
    since, until = _window()
    calls = conn.execute(
        """SELECT COUNT(*) FROM calls c
           JOIN numbers n ON n.phone10 = substr(c.client_phone,-10)
           WHERE c.direction='out' AND n.category IN ('исполнитель','оба')
           AND c.duration_sec>15 AND c.is_demo=0 AND c.started_at BETWEEN ? AND ?""",
        (since, until)).fetchone()[0]
    if calls == 0:
        _mark_ok(conn, "stream4"); return None
    checked = conn.execute(
        "SELECT COUNT(*) FROM search_checks WHERE checked_at BETWEEN ? AND ?",
        (since, until)).fetchone()[0]
    if checked > 0:
        _mark_ok(conn, "stream4"); return None
    return (f"🔴 Поток 4 (поиск техники): за {since[11:16]}–{until[11:16]} было {calls} "
            f"звонков поставщикам, разобрано — 0.")


def send_telegram(token: str, chat_id: int, text: str) -> None:
    r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   data={"chat_id": chat_id, "text": text}, timeout=15)
    r.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="действительно слать в Telegram")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    settings = get_settings()
    conn = sqlite3.connect(settings.db_path)
    _ensure_state_table(conn)

    checks = [
        ("error_spike", check_error_spike),
        ("stream2", check_stream2_inbound),
        ("stream3", check_stream3_prozvon),
        ("stream4", check_stream4_search),
    ]

    alerts = []
    for key, fn in checks:
        msg = fn(conn)
        if msg is None:
            continue
        if not _should_alert(conn, key):
            print(f"[{key}] проблема есть, но алерт недавно уже был — молчу")
            continue
        alerts.append((key, msg))

    if not alerts:
        print("всё в порядке, алертов нет")
        return 0

    for key, msg in alerts:
        print(msg)
        if args.apply:
            if not (settings.watchdog_bot_token and settings.watchdog_chat_id):
                logger.error("нет токена/chat_id сторожа — .env читается только под claude")
                return 1
            try:
                send_telegram(settings.watchdog_bot_token, settings.watchdog_chat_id, msg)
            except httpx.HTTPError as exc:
                logger.error("не отправилось: %s", exc)
                continue
            _mark_alerted(conn, key)

    return 0


if __name__ == "__main__":
    sys.exit(main())
