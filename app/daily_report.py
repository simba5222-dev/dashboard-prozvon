"""Сводка по всем четырём потокам звонков за один день — для бота в Телеграме.

Числа по потокам 2/3/4 берутся из базы дашборда. Поток 1 живёт на боевом
сервере в России и там не ведёт отдельной таблицы с состоянием разбора —
читаем его же журнал (`journalctl -u asr`) за московские сутки, по тем же
строкам, что и в ручном отчёте от 08.10.2026.

08.10.2026: владелец попросил кнопку «Отчёт» в боте-стороже.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings

# 10 с — реальный порог dashboard-prozvon-fast.service (--min-sec 10), а не
# условные 40 из morning_report.py: там это «стоит ли разбирать», здесь —
# «что на самом деле попадает в очередь разбора» сейчас.
ANALYZE_MIN_SEC = 10
SEARCH_MIN_SEC = 15           # порог потока 4, как в stream_watchdog.py

PROD_HOST = "root@91.220.109.49"
PROD_KEY = "/home/agent/.ssh/ru-proxy_ed25519"
MSK = timezone(timedelta(hours=3))


def today_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _msk_day_bounds_utc(day: str) -> tuple[str, str]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=MSK)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.astimezone(timezone.utc).strftime(fmt), end.astimezone(timezone.utc).strftime(fmt)


def _ad_line_numbers(conn) -> set[str]:
    """Список общих номеров (10 цифр) — решение владельца, живёт в `lines`.

    `kind='рекламная'` одно не годится: это вообще все линии, которые ВАТС
    когда-либо подписал как рекламные, а владелец 01.10.2026 сузил список до
    семи. Настоящий фильтр — `in_scope` (и `is_general` для линий без имени
    от ВАТС, см. `/api/line-in-scope`)."""
    rows = conn.execute(
        "SELECT phone10 FROM lines WHERE in_scope = 1 AND (is_general = 1 OR kind = 'рекламная')"
    ).fetchall()
    return {r[0] for r in rows}


def _stream1_calls(conn, day: str, settings: Settings) -> int | None:
    """Звонков на рекламных линиях за день — напрямую из истории ВАТС.

    08.10.2026: первая версия считала «звонков» = все входящие по серверу
    (373), владелец справедливо не поверил. Вторая версия вычитала по логу
    «линия не в списке», тоже неверно: причина пропуска short-circuit —
    короткие и безzаписные звонки отсеиваются раньше проверки линии и в этот
    лог не попадают, так что вычитание давало заниженный «не рекламный»
    остаток и завышало «рекламный». Верно — спросить саму историю ВАТС:
    она отдаёт номер линии (`via`) на каждую строку, сверяем со своим
    справочником. Проверено на 07.10 целиком: 26 на линиях, 16 обработано —
    обработанных не может быть больше кандидатов, сходится."""
    if not settings.prod_lookup_url or not settings.prod_lookup_token:
        return None
    numbers = _ad_line_numbers(conn)
    if not numbers:
        return None
    since, until = _msk_day_bounds_utc(day)
    since_iso = datetime.strptime(since, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc).isoformat()
    until_iso = datetime.strptime(until, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc).isoformat()
    try:
        r = httpx.get(
            f"{settings.prod_lookup_url}/lookup-records",
            params={"start": since_iso, "end": until_iso, "with_record_only": "false"},
            headers={"X-Lookup-Token": settings.prod_lookup_token}, timeout=30,
        )
        r.raise_for_status()
        rows = r.json()["rows"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    return sum(1 for row in rows if (row.get("via") or "")[-10:] in numbers)


def _stream1_processed(day: str) -> tuple[int, int] | None:
    """(транскрибировано, разобрано) потока 1 — по журналу боевого сервера.

    Эти строки пишутся уже ПОСЛЕ проверки линии (см. `pipeline.py`,
    `synergy.py`), поэтому log-based подсчёт для них надёжен — в отличие
    от «звонков», здесь не нужна история ВАТС."""
    since, until = _msk_day_bounds_utc(day)
    script = (
        f"LOG=$(journalctl -u asr --no-pager --since '{since} UTC' --until '{until} UTC' 2>/dev/null); "
        "echo \"$LOG\" | grep -c 'звонок .* обработан:'; "
        "echo \"$LOG\" | grep -cE 'заявку не трогаем|-> контакт .*, заказ'"
    )
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             "-i", PROD_KEY, PROD_HOST, script],
            capture_output=True, text=True, timeout=25,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    lines = out.stdout.strip().splitlines()
    if out.returncode != 0 or len(lines) != 2:
        return None
    try:
        return tuple(int(x) for x in lines)  # type: ignore[return-value]
    except ValueError:
        return None


def _stream2_counts(conn, day: str, settings: Settings) -> tuple[int, int]:
    """(звонков, просеяно) потока 2 — входящие на прямые номера."""
    calls = conn.execute(
        """SELECT COUNT(*) FROM calls WHERE local_date = ? AND direction = 'in'
             AND duration_sec >= ?""",
        (day, settings.inbound_min_duration_sec)).fetchone()[0]
    screened = conn.execute(
        """SELECT COUNT(*) FROM screens s JOIN calls c ON c.uid = s.call_uid
           WHERE c.local_date = ? AND c.direction = 'in'
             AND c.duration_sec >= ?""", (day, settings.inbound_min_duration_sec)).fetchone()[0]
    return calls, screened


def _stream3_counts(conn, day: str) -> tuple[int, int, int]:
    """(звонков, транскрибировано, разобрано) потока 3 — прозвон.

    Числитель и знаменатель — по одному и тому же порогу длительности,
    иначе «разобрано» может оказаться больше «звонков» (так и вышло при
    первой проверке 08.10.2026, когда порог был не тот, что у реального
    таймера)."""
    calls = conn.execute(
        """SELECT COUNT(*) FROM calls WHERE local_date = ? AND direction = 'out'
             AND in_group = 1 AND duration_sec >= ?""",
        (day, ANALYZE_MIN_SEC)).fetchone()[0]
    transcribed = conn.execute(
        """SELECT COUNT(*) FROM transcripts t JOIN calls c ON c.uid = t.call_uid
           WHERE c.local_date = ? AND c.direction = 'out' AND c.in_group = 1
             AND c.duration_sec >= ? AND t.text IS NOT NULL AND t.text != ''""",
        (day, ANALYZE_MIN_SEC)).fetchone()[0]
    analyzed = conn.execute(
        """SELECT COUNT(*) FROM transcripts t JOIN calls c ON c.uid = t.call_uid
           WHERE c.local_date = ? AND c.direction = 'out' AND c.in_group = 1
             AND c.duration_sec >= ? AND t.analysis_json IS NOT NULL AND t.analysis_json != ''""",
        (day, ANALYZE_MIN_SEC)).fetchone()[0]
    return calls, transcribed, analyzed


def _stream4_counts(conn, day: str) -> tuple[int, int]:
    """(звонков, проверено) потока 4 — поиск техники."""
    calls = conn.execute(
        """SELECT COUNT(*) FROM calls c
           JOIN numbers n ON n.phone10 = substr(c.client_phone,-10)
           WHERE c.direction='out' AND n.category IN ('исполнитель','оба')
             AND c.duration_sec > ? AND c.is_demo = 0 AND c.local_date = ?""",
        (SEARCH_MIN_SEC, day)).fetchone()[0]
    checked = conn.execute(
        """SELECT COUNT(*) FROM search_checks s JOIN calls c ON c.uid = s.call_uid
           JOIN numbers n ON n.phone10 = substr(c.client_phone,-10)
           WHERE s.local_date = ? AND c.direction='out' AND n.category IN ('исполнитель','оба')
             AND c.duration_sec > ? AND c.is_demo = 0""",
        (day, SEARCH_MIN_SEC)).fetchone()[0]
    return calls, checked


def build_report(conn, settings: Settings, day: str | None = None) -> str:
    day = day or today_msk()
    lines = [f"📊 Отчёт по звонкам за {day} (МСК)"]

    calls1 = _stream1_calls(conn, day, settings)
    processed1 = _stream1_processed(day)
    if calls1 is None or processed1 is None:
        lines.append("\nПоток 1 (реклама): боевой сервер или справочник линий не ответил, данных нет")
    else:
        transcribed, analyzed = processed1
        pending = max(calls1 - transcribed, 0)
        lines.append(
            f"\nПоток 1 (реклама):\n  звонков {calls1}, транскрибировано {transcribed}, "
            f"разобрано {analyzed}, в очереди {pending}"
        )

    calls2, screened2 = _stream2_counts(conn, day, settings)
    lines.append(
        f"\nПоток 2 (входящие, прямые номера):\n  звонков {calls2}, "
        f"транскрибировано {screened2}, разобрано {screened2}, "
        f"в очереди {max(calls2 - screened2, 0)}"
    )

    calls3, transcribed3, analyzed3 = _stream3_counts(conn, day)
    lines.append(
        f"\nПоток 3 (прозвон):\n  звонков {calls3}, транскрибировано {transcribed3}, "
        f"разобрано {analyzed3}, в очереди {max(calls3 - analyzed3, 0)}"
    )

    calls4, checked4 = _stream4_counts(conn, day)
    lines.append(
        f"\nПоток 4 (поиск техники):\n  звонков {calls4}, транскрибировано {checked4}, "
        f"разобрано {checked4}, в очереди {max(calls4 - checked4, 0)}"
    )

    return "\n".join(lines)
