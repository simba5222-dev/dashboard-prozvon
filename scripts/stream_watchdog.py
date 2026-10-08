#!/usr/bin/env python
"""Сторож потоков 2, 3 и 4 — проверка с задержкой, не «прямо сейчас».

    sudo -u claude .venv/bin/python scripts/stream_watchdog.py            # сухой прогон
    sudo -u claude .venv/bin/python scripts/stream_watchdog.py --apply    # слать алерты

Проверяет не «работает ли сервис», а «дошёл ли звонок до конца конвейера» —
то, что `systemctl is-active` не видит в принципе (см. историю 21.09 и 23.09:
сервис был «active», а работа молча стояла).

Окно с задержкой, и у потоков оно разное — по их реальному циклу, а не
единое число. Поток 2 досчитывается таймером раз в минуту — ему хватает
30–45 минут. До 08.10.2026 потоки 3 и 4 досчитывались батчем раз в час
(`dashboard-prozvon.timer`) — с окном в 30–45 минут сторож слал бы ложную
тревогу на каждый звонок, который просто ждёт своей часовой волны, поймано
на себе же 07.10.2026. Теперь их разбор вынесен в отдельный
`dashboard-prozvon-fast.timer` (раз в 10 минут, без синхронизации
справочников транспорта), поэтому окно снова как у потока 2 — 30–45 минут.

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

Раз в сутки, утром — контрольное сообщение, даже если сказать нечего.
08.10.2026: владелец открыл канал, увидел тишину с вечера и решил, что
сторож умер — хотя он исправно отрабатывал каждые 10 минут. Тишина при
молчаливом «всё хорошо» неотличима снаружи от тишины мёртвого сервиса.
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

# 08.10.2026: разбор потоков 3 и 4 переведён на dashboard-prozvon-fast.timer
# (раз в 10 минут) — окно сузилось обратно до уровня потока 2. Имя константы
# оставлено прежним, чтобы не трогать остальные вызовы без нужды.
ОКНО_ЗАДЕРЖКИ_ЧАСОВОЙ_МИН = 30
ОКНО_ШИРИНА_ЧАСОВАЯ_МИН = 15

HEARTBEAT_ЧАС_UTC = 4     # 07:00 по Москве — контрольное сообщение раз в сутки


def _window(delay_min: int = ОКНО_ЗАДЕРЖКИ_МИН,
           width_min: int = ОКНО_ШИРИНА_МИН) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    until = now - timedelta(minutes=delay_min)
    since = until - timedelta(minutes=width_min)
    return since, until


def _fmt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_started(ts: str) -> datetime:
    """`calls.started_at` хранится то в UTC, то в +03:00 (смотря по источнику —
    вебхук ВАТС или опрос Synergy). Сравнивать такие строки оператором BETWEEN
    молча неверно: "09:44...+03:00" лексикографически может попасть между
    двумя UTC-метками, хотя по факту это другое время. Поймано на себе же
    07.10.2026 — через fromisoformat сравнение честное."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _recent_local_dates(days: int = 2) -> tuple[str, str]:
    """Грубый SQL-предфильтр по local_date — он не зависит от смешанных поясов
    started_at, сужает выборку перед точным фильтром в Python."""
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


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


def check_stream2_inbound(conn: sqlite3.Connection, min_duration_sec: int = 10) -> str | None:
    """`min_duration_sec` — тот же порог, что у самого потока
    (`settings.inbound_min_duration_sec`, сейчас 10с). Звонок короче него
    поток и не пытается просеивать — звать его «зависшим» нечестно.
    Поймано на себе же 08.10.2026: 3-секундный звонок без записи ВАТС
    посчитался «зависшим просевом», хотя поток его и не должен был трогать."""
    since, until = _window()
    day_from, day_to = _recent_local_dates()
    rows = conn.execute(
        "SELECT uid, started_at FROM calls WHERE direction='in' AND is_demo=0 "
        "AND duration_sec>=? AND local_date BETWEEN ? AND ?",
        (min_duration_sec, day_from, day_to)).fetchall()
    uids = [r[0] for r in rows if since <= _parse_started(r[1]) <= until]
    if not uids:
        _mark_ok(conn, "stream2"); return None
    placeholders = ",".join("?" * len(uids))
    screened = conn.execute(
        f"SELECT COUNT(*) FROM screens WHERE call_uid IN ({placeholders})", uids).fetchone()[0]
    if screened > 0:
        _mark_ok(conn, "stream2"); return None
    return (f"🔴 Поток 2 (входящие, ловля потерянных): за {_fmt(since)[11:16]}–"
            f"{_fmt(until)[11:16]} было {len(uids)} состоявшихся входящих, "
            f"просеяно — 0. Похоже, просев встал.")


def check_stream3_prozvon(conn: sqlite3.Connection) -> str | None:
    since, until = _window(ОКНО_ЗАДЕРЖКИ_ЧАСОВОЙ_МИН, ОКНО_ШИРИНА_ЧАСОВАЯ_МИН)
    day_from, day_to = _recent_local_dates()
    rows = conn.execute(
        """SELECT uid, started_at FROM calls WHERE direction='out' AND in_group=1
           AND is_demo=0 AND duration_sec>15 AND local_date BETWEEN ? AND ?""",
        (day_from, day_to)).fetchall()
    uids = [r[0] for r in rows if since <= _parse_started(r[1]) <= until]
    if not uids:
        _mark_ok(conn, "stream3"); return None
    placeholders = ",".join("?" * len(uids))
    with_analysis = conn.execute(
        f"SELECT COUNT(*) FROM transcripts WHERE call_uid IN ({placeholders}) "
        f"AND analysis_json IS NOT NULL AND analysis_json != ''", uids).fetchone()[0]
    if with_analysis > 0:
        _mark_ok(conn, "stream3"); return None
    return (f"🔴 Поток 3 (прозвон): за {_fmt(since)[11:16]}–{_fmt(until)[11:16]} было "
            f"{len(uids)} звонков, у 0 из них есть разбор содержания (оценка, "
            f"рекомендации). Расшифровка может идти, а анализ — нет.")


def check_stream4_search(conn: sqlite3.Connection) -> str | None:
    since, until = _window(ОКНО_ЗАДЕРЖКИ_ЧАСОВОЙ_МИН, ОКНО_ШИРИНА_ЧАСОВАЯ_МИН)
    day_from, day_to = _recent_local_dates()
    rows = conn.execute(
        """SELECT c.uid, c.started_at FROM calls c
           JOIN numbers n ON n.phone10 = substr(c.client_phone,-10)
           WHERE c.direction='out' AND n.category IN ('исполнитель','оба')
           AND c.duration_sec>15 AND c.is_demo=0 AND c.local_date BETWEEN ? AND ?""",
        (day_from, day_to)).fetchall()
    calls = [r for r in rows if since <= _parse_started(r[1]) <= until]
    if not calls:
        _mark_ok(conn, "stream4"); return None
    uids = [r[0] for r in calls]
    placeholders = ",".join("?" * len(uids))
    checked = conn.execute(
        f"SELECT COUNT(*) FROM search_checks WHERE call_uid IN ({placeholders})",
        uids).fetchone()[0]
    if checked > 0:
        _mark_ok(conn, "stream4"); return None
    return (f"🔴 Поток 4 (поиск техники): за {_fmt(since)[11:16]}–{_fmt(until)[11:16]} "
            f"было {len(calls)} звонков поставщикам, разобрано — 0.")


def send_telegram(token: str, chat_id: int, text: str) -> None:
    r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   data={"chat_id": chat_id, "text": text}, timeout=15)
    r.raise_for_status()


def _heartbeat_due(conn: sqlite3.Connection, now: datetime) -> bool:
    """Раз в сутки, не чаще. Час проверяем прямо здесь — таймер срабатывает
    каждые 10 минут, и в нужный час это сработает один раз, дата в
    watchdog_state не даст повторить."""
    if now.hour != HEARTBEAT_ЧАС_UTC:
        return False
    row = conn.execute(
        "SELECT last_alert_at FROM watchdog_state WHERE check_key='heartbeat'").fetchone()
    sent_today = row and row[0] and row[0][:10] == now.date().isoformat()
    return not sent_today


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
        ("stream2", lambda c: check_stream2_inbound(c, settings.inbound_min_duration_sec)),
        ("stream3", check_stream3_prozvon),
        ("stream4", check_stream4_search),
    ]

    problems = []   # все найденные проблемы — и для контрольного сообщения тоже
    alerts = []     # то, что реально шлём сейчас — антиспам мог часть придержать
    for key, fn in checks:
        msg = fn(conn)
        if msg is None:
            continue
        problems.append((key, msg))
        if not _should_alert(conn, key):
            print(f"[{key}] проблема есть, но алерт недавно уже был — молчу")
            continue
        alerts.append((key, msg))

    if not alerts:
        print("всё в порядке, алертов нет" if not problems else
              "проблемы есть, но уже сообщены — молчу")

    bot_ready = bool(settings.watchdog_bot_token and settings.watchdog_chat_id)

    for key, msg in alerts:
        print(msg)
        if args.apply:
            if not bot_ready:
                logger.error("нет токена/chat_id сторожа — .env читается только под claude")
                return 1
            try:
                send_telegram(settings.watchdog_bot_token, settings.watchdog_chat_id, msg)
            except httpx.HTTPError as exc:
                logger.error("не отправилось: %s", exc)
                continue
            _mark_alerted(conn, key)

    now = datetime.now(timezone.utc)
    if _heartbeat_due(conn, now):
        text = ("✅ Сторож на связи, все потоки в порядке." if not problems else
                "🟡 Сторож на связи. Есть нерешённое:\n" + "\n".join(m for _, m in problems))
        print("heartbeat:", text.splitlines()[0])
        if args.apply and bot_ready:
            try:
                send_telegram(settings.watchdog_bot_token, settings.watchdog_chat_id, text)
                _mark_alerted(conn, "heartbeat")
            except httpx.HTTPError as exc:
                logger.error("контрольное сообщение не отправилось: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
