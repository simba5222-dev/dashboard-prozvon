#!/usr/bin/env python
"""Ежедневная диагностика обработки звонков: что не доехало и где встало.

    sudo -u claude .venv/bin/python scripts/selfcheck.py            за вчера
    ... --day 2026-09-17     проверить конкретный день
    ... --quiet              печатать только при проблемах

Запускается таймером `dashboard-selfcheck` каждый день в 06:00 по Москве.
Отчёт пишется в `data/selfcheck.log`, последнее состояние — в
`data/selfcheck.json`, откуда его берёт дашборд и начало рабочей сессии.

Проверяется весь путь звонка, потому что вставать он может на любом шаге:
собран → запись скачана → распознан → разобран. 17.09.2026 день остался без
разбора, и заметили это к вечеру: разбор упал в девять утра с «database is
locked», а сторожа над ним не было.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402

TIMERS = ("dashboard-collect", "dashboard-records", "dashboard-leads")

# Слова, по которым в логах видно обрыв. «locked» отдельно: эта ошибка
# выглядит безобидной строкой в конце файла, а означает потерянный день.
FAILURE_MARKERS = ("Traceback", "database is locked", "CRITICAL")

# Разбираем разговоры от сорока секунд — решение владельца от 17.09.2026.
# `talk_threshold_sec` (15 с) — про другое: по нему считается «состоялся ли
# разговор» на экранах, и диагностике он дал бы втрое больше строк.
ANALYZE_MIN_SEC = 40


class Report:
    def __init__(self, day: str) -> None:
        self.day = day
        self.problems: list[str] = []
        self.facts: list[str] = []

    def problem(self, text: str) -> None:
        self.problems.append(text)

    def fact(self, text: str) -> None:
        self.facts.append(text)

    @property
    def status(self) -> str:
        return "ВНИМАНИЕ" if self.problems else "в порядке"


def check_timers(rep: Report) -> None:
    """Таймеры включены и отработали недавно."""
    for unit in TIMERS:
        active = subprocess.run(
            ["systemctl", "is-active", f"{unit}.timer"],
            capture_output=True, text=True,
        ).stdout.strip()
        if active != "active":
            rep.problem(f"таймер {unit} не активен ({active})")
            continue
        out = subprocess.run(
            ["systemctl", "show", f"{unit}.service",
             "--property=ExecMainExitTimestamp,ExecMainStatus"],
            capture_output=True, text=True,
        ).stdout
        code = re.search(r"ExecMainStatus=(\d+)", out)
        if code and code.group(1) != "0":
            rep.problem(f"последний запуск {unit} завершился с кодом {code.group(1)}")


def check_pipeline(conn, rep: Report, day: str, threshold: int, records: Path) -> dict:
    """Путь звонка за день: собран → запись → распознан → разобран."""
    calls = conn.execute(
        """SELECT uid, duration_sec FROM calls
           WHERE local_date = ? AND direction = 'out' AND in_group = 1
             AND duration_sec >= ?""",
        (day, threshold),
    ).fetchall()
    uids = [r["uid"] for r in calls]

    done = set()
    if uids:
        marks = ",".join("?" * len(uids))
        done = {
            r["call_uid"] for r in conn.execute(
                f"""SELECT call_uid FROM transcripts
                    WHERE call_uid IN ({marks})
                      AND analysis_json IS NOT NULL AND analysis_json != ''""",
                uids,
            )
        }
    have_record = {u for u in uids if (records / f"{u}.mp3").exists()}

    stats = {
        "разговоров": len(uids),
        "записей скачано": len(have_record),
        "разобрано": len(done),
    }
    rep.fact(
        f"прозвон за {day}: разговоров дольше {threshold} с — {len(uids)}, "
        f"записей скачано {len(have_record)}, разобрано {len(done)}"
    )

    if uids and not done:
        rep.problem(f"за {day} не разобрано ни одного разговора из {len(uids)}")
    elif uids and len(done) < len(uids) * 0.8:
        rep.problem(
            f"за {day} разобрано {len(done)} из {len(uids)} — меньше четырёх пятых"
        )
    if len(have_record) < len(uids):
        rep.problem(
            f"за {day} не скачано записей: {len(uids) - len(have_record)} "
            f"(без записи разбор невозможен)"
        )

    # Входящие отдела продаж: ловля потерянных заявок.
    screened = conn.execute(
        """SELECT COUNT(*) n FROM screens s JOIN calls c ON c.uid = s.call_uid
           WHERE c.local_date = ?""", (day,),
    ).fetchone()["n"]
    inbound = conn.execute(
        """SELECT COUNT(*) n FROM calls
           WHERE local_date = ? AND direction = 'in' AND duration_sec >= ?""",
        (day, threshold),
    ).fetchone()["n"]
    waiting = conn.execute(
        """SELECT COUNT(*) n FROM screens
           WHERE is_request = 1 AND created_order_id IS NULL
             AND (approved IS NULL OR approved = 0)"""
    ).fetchone()["n"]
    stats.update({"входящих просеяно": screened, "ждут подтверждения": waiting})
    rep.fact(
        f"входящие за {day}: дольше {threshold} с — {inbound}, просеяно {screened}; "
        f"находок ждёт подтверждения — {waiting}"
    )
    if inbound and not screened:
        rep.problem(f"за {day} не просеяно ни одного входящего из {inbound}")
    return stats


def check_logs(rep: Report, log_dir: Path, hours: int = 26) -> None:
    """Обрывы в логах за последние сутки."""
    cutoff = datetime.now().timestamp() - hours * 3600
    for path in sorted(log_dir.glob("*.log")):
        # Свой же отчёт пропускаем: иначе вчерашние тревоги читаются как
        # сегодняшние и множатся с каждым запуском.
        if path.name == "selfcheck.log" or path.stat().st_mtime < cutoff:
            continue
        try:
            tail = path.read_text(errors="replace").splitlines()[-400:]
        except OSError:
            continue
        hits = [l for l in tail if any(m in l for m in FAILURE_MARKERS)]
        if hits:
            rep.problem(f"{path.name}: следы обрыва в хвосте ({len(hits)} строк), последняя — {hits[-1][:110]}")
        skipped = [l for l in tail if re.search(r"не вышло [1-9]", l)]
        if skipped:
            rep.fact(f"{path.name}: {skipped[-1].strip()[:120]}")


def check_running(rep: Report) -> None:
    """Идёт ли разбор прямо сейчас — чтобы утренний отчёт не путал работу с простоем."""
    out = subprocess.run(["pgrep", "-af", "scripts/analyze_calls.py"],
                         capture_output=True, text=True).stdout.strip()
    if out:
        rep.fact(f"разбор сейчас идёт: {out.splitlines()[0][:100]}")


def check_asr(rep: Report, url: str) -> None:
    """Сервис распознавания отвечает."""
    import httpx

    try:
        r = httpx.get(f"{url.rstrip('/')}/health", timeout=30)
        r.raise_for_status()
        body = r.json()
    except httpx.TimeoutException:
        # Сервис обрабатывает записи по одной и на /health отвечает, только
        # освободившись. Молчание под нагрузкой — признак работы, а не отказа.
        rep.fact("распознавание: занято обработкой, health не ответил за 30 с")
        return
    except Exception as exc:  # noqa: BLE001 — остальные отказы равнозначны
        rep.problem(f"сервис распознавания не отвечает: {type(exc).__name__}")
        return
    rep.fact(f"распознавание: модель {'загружена' if body.get('model_loaded') else 'ещё не загружена'}")


def check_backup(rep: Report) -> None:
    """Свежесть резервной копии. Бэкап без строки завершения не считается."""
    log = Path("/var/backups/projects/backup.log")
    db_dir = Path("/var/backups/projects/db")
    if not log.exists():
        rep.problem("резервного копирования нет вовсе")
        return
    dumps = sorted(db_dir.glob("*.db.gz"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not dumps:
        rep.problem("копий базы нет в /var/backups/projects/db")
        return
    age_h = (datetime.now().timestamp() - dumps[0].stat().st_mtime) / 3600
    # «ГОТОВО» пишется последней строкой: упавший на середине бэкап оставляет
    # файлы, которые выглядят целыми, и без этой проверки читается как успех.
    tail = log.read_text(errors="replace").splitlines()[-25:]
    finished = any(l.startswith("ГОТОВО") for l in tail)
    text = f"последняя копия {age_h:.0f} ч назад ({len(dumps)} шт), {dumps[0].stat().st_size // 1024} КБ"
    if age_h > 36 or not finished:
        rep.problem(f"резервное копирование: {text}"
                    + ("" if finished else ", в логе нет строки завершения"))
    else:
        rep.fact(f"резервное копирование: {text}")


def check_disk(rep: Report, db_path: Path, records: Path) -> None:
    """Место на диске и размеры того, что растёт."""
    out = subprocess.run(["df", "-BG", "--output=avail,pcent", str(db_path.parent)],
                         capture_output=True, text=True).stdout.splitlines()
    if len(out) > 1:
        avail, pcent = out[1].split()
        rep.fact(f"диск: свободно {avail}, занято {pcent}")
        if int(avail.rstrip("G")) < 5:
            rep.problem(f"на диске осталось {avail} — записи и база расти не смогут")
    if records.exists():
        size_mb = sum(f.stat().st_size for f in records.glob("*.mp3")) / 1024 / 1024
        rep.fact(f"записей: {len(list(records.glob('*.mp3')))} шт, {size_mb:.0f} МБ")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="какой день проверять, ГГГГ-ММ-ДД (по умолчанию вчера)")
    ap.add_argument("--min-sec", type=int, default=ANALYZE_MIN_SEC,
                    help=f"с какой длительности разговор попадает в разбор (по умолчанию {ANALYZE_MIN_SEC})")
    ap.add_argument("--quiet", action="store_true", help="печатать только при проблемах")
    args = ap.parse_args()

    settings = get_settings()
    day = args.day or (date.today() - timedelta(days=1)).isoformat()
    rep = Report(day)

    conn = connect(settings.db_path)
    records = Path(settings.records_dir)
    db_path = Path(settings.db_path)

    check_timers(rep)
    stats = check_pipeline(conn, rep, day, args.min_sec, records)
    check_logs(rep, db_path.parent)
    check_running(rep)
    check_asr(rep, settings.asr_url)
    check_backup(rep)
    check_disk(rep, db_path, records)

    now = datetime.now(timezone.utc)
    lines = [f"=== диагностика {now.isoformat(timespec='seconds')} — {rep.status}"]
    lines += [f"  · {f}" for f in rep.facts]
    lines += [f"  ! {p}" for p in rep.problems]
    text = "\n".join(lines)

    log = db_path.parent / "selfcheck.log"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")

    state = {
        "checked_at": now.isoformat(timespec="seconds"),
        "day": day,
        "status": rep.status,
        "problems": rep.problems,
        "facts": rep.facts,
        "stats": stats,
    }
    (db_path.parent / "selfcheck.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if rep.problems or not args.quiet:
        print(text)
    # Код возврата отражает итог: systemd пометит запуск красным, и это видно
    # в `systemctl list-timers` без чтения логов.
    return 1 if rep.problems else 0


if __name__ == "__main__":
    sys.exit(main())
