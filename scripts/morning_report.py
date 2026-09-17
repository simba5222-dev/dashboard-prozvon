#!/usr/bin/env python
"""Утренний отчёт по всем процессам: что работает, что встало, где дыры.

    /home/claude/dashboard/.venv/bin/python scripts/morning_report.py
    ... --no-publish     не выкладывать страницу, только посчитать

Запускается таймером `dashboard-morning` в 07:00 по Москве — после ночной
диагностики в 06:00, чтобы к восьми утра отчёт был готов.

Работает под `agent`, а не под `claude`: нужен ssh-ключ к боевому серверу в
России. Поэтому настройки проекта (`.env`, права 600) не читаются — база
дашборда открыта на чтение всем, а остальное берётся из системы.

Страница выкладывается рядом с картой: https://72-56-25-105.nip.io/karta/morning.html
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DASH = Path("/home/claude/dashboard")
DB = DASH / "data/dashboard.db"
RECORDS = DASH / "data/records"
PROD_HOST = "root@91.220.109.49"
PROD_KEY = "/home/agent/.ssh/ru-proxy_ed25519"
PUBLISH_TO = Path("/var/www/karta/morning.html")
PROJECTS = ("/home/claude/dashboard", "/home/claude/karta",
            "/home/claude/asr-vats-megafon", "/home/claude/meh-techno-res")

# Разбираем разговоры от сорока секунд — решение владельца от 17.09.2026.
ANALYZE_MIN_SEC = 40


def sh(*args: str, timeout: int = 20) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def ssh_prod(script: str, timeout: int = 25) -> str:
    return sh("ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
              "-i", PROD_KEY, PROD_HOST, script, timeout=timeout)


class Section:
    def __init__(self, title: str) -> None:
        self.title = title
        self.rows: list[tuple[str, str, str]] = []  # (что, значение, уровень)

    def add(self, what: str, value: str, level: str = "ok") -> None:
        self.rows.append((what, value, level))

    @property
    def worst(self) -> str:
        levels = [r[2] for r in self.rows]
        for level in ("beda", "vnimanie"):
            if level in levels:
                return level
        return "ok"


def calls_section(day: str) -> Section:
    s = Section("Обработка звонков")
    if not DB.exists():
        s.add("база дашборда", "не найдена", "beda")
        return s
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    out = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(duration_sec), 0) / 60 m FROM calls
           WHERE local_date = ? AND direction = 'out' AND in_group = 1
             AND duration_sec >= ?""", (day, ANALYZE_MIN_SEC)).fetchone()
    done = conn.execute(
        """SELECT COUNT(*) n FROM transcripts t JOIN calls c ON c.uid = t.call_uid
           WHERE c.local_date = ? AND c.direction = 'out'
             AND t.analysis_json IS NOT NULL AND t.analysis_json != ''""",
        (day,)).fetchone()["n"]
    share = done / out["n"] if out["n"] else 1
    s.add(f"прозвон за {day}", f"{done} из {out['n']} разобрано ({out['m']} мин звука)",
          "ok" if share >= 0.8 else ("vnimanie" if share > 0 else "beda"))

    screened = conn.execute(
        """SELECT COUNT(*) n FROM screens s JOIN calls c ON c.uid = s.call_uid
           WHERE c.local_date = ?""", (day,)).fetchone()["n"]
    inbound = conn.execute(
        """SELECT COUNT(*) n FROM calls WHERE local_date = ? AND direction = 'in'
             AND duration_sec >= ?""", (day, ANALYZE_MIN_SEC)).fetchone()["n"]
    s.add(f"входящие за {day}", f"просеяно {screened} из {inbound}",
          "ok" if screened else "vnimanie")

    waiting = conn.execute(
        """SELECT COUNT(*) n FROM screens WHERE is_request = 1
             AND created_order_id IS NULL AND (approved IS NULL OR approved = 0)"""
    ).fetchone()["n"]
    s.add("находок ждёт вашего решения", str(waiting),
          "vnimanie" if waiting else "ok")

    queue = conn.execute(
        """SELECT COUNT(*) n FROM calls k LEFT JOIN transcripts t ON t.call_uid = k.uid
           WHERE k.duration_sec >= ? AND k.direction = 'out' AND k.in_group = 1
             AND (t.analysis_json IS NULL OR t.analysis_json = '')""",
        (ANALYZE_MIN_SEC,)).fetchone()["n"]
    s.add("неразобранный хвост за всё время", f"{queue} разговоров",
          "vnimanie" if queue > 200 else "ok")
    return s


def machines_section() -> Section:
    s = Section("Машины и службы")
    for unit in ("asr", "dashboard-collect.timer", "dashboard-records.timer",
                 "dashboard-leads.timer", "dashboard-selfcheck.timer", "nginx"):
        state = sh("systemctl", "is-active", unit) or "нет ответа"
        s.add(f"{unit} (Амстердам)", state, "ok" if state == "active" else "beda")

    load = (Path("/proc/loadavg").read_text().split()[:3])
    s.add("нагрузка (Амстердам)", " ".join(load))
    free = sh("df", "-h", "--output=avail", "/").splitlines()
    s.add("свободно на диске (Амстердам)", free[-1].strip() if free else "?")

    prod = ssh_prod(
        "systemctl is-active asr; df -h / | tail -1 | awk '{print $4}'; "
        "fail2ban-client status sshd 2>/dev/null | grep -c Banned; "
        "journalctl -u ssh --since '24 hours ago' | grep -c 'Failed password'"
    ).splitlines()
    if len(prod) >= 4:
        s.add("asr (Россия, боевой)", prod[0], "ok" if prod[0] == "active" else "beda")
        s.add("свободно на диске (Россия)", prod[1])
        s.add("подборов пароля за сутки (Россия)", prod[3],
              "vnimanie" if int(prod[3] or 0) > 100 else "ok")
    else:
        s.add("боевой сервер", "не ответил на проверку", "beda")
    return s


def safety_section() -> Section:
    s = Section("Уязвимые места")

    # 1. Копии данных. Записи и разбор живут в одном экземпляре на одном диске.
    size = sum(f.stat().st_size for f in RECORDS.glob("*.mp3")) / 1024 / 1024 if RECORDS.exists() else 0
    s.add("резервная копия базы и записей",
          f"нет; на диске {size:.0f} МБ записей и база с разбором", "beda")

    # 2. Незапушенные коммиты: код есть только здесь.
    for proj in PROJECTS:
        if not Path(proj, ".git").exists():
            continue
        ahead = sh("git", "-C", proj, "rev-list", "--count", "@{u}..HEAD")
        dirty = len(sh("git", "-C", proj, "status", "--short").splitlines())
        name = Path(proj).name
        if ahead and ahead != "0":
            s.add(f"{name}: не отправлено на GitHub", f"{ahead} коммитов", "vnimanie")
        if dirty:
            s.add(f"{name}: не закоммичено", f"{dirty} файлов", "vnimanie")

    # 3. Вход на боевой сервер.
    prod = ssh_prod("sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin)'; "
                    "ufw status | head -1; systemctl is-active fail2ban")
    lines = prod.splitlines()
    text = " ".join(lines)
    if "passwordauthentication yes" in text:
        s.add("боевой сервер: вход по паролю", "разрешён, порт 22 открыт наружу", "beda")
    if "permitrootlogin yes" in text:
        s.add("боевой сервер: вход под root", "разрешён", "beda")
    if "inactive" in text.lower() and "Status: inactive" in prod:
        s.add("боевой сервер: файрвол", "выключен", "beda")
    s.add("боевой сервер: fail2ban", lines[-1] if lines else "?",
          "ok" if lines and lines[-1] == "active" else "vnimanie")

    # 4. Незакрытое за владельцем.
    prod_dirty = ssh_prod("cd /opt/asr && git status --short | wc -l")
    if prod_dirty and prod_dirty != "0":
        s.add("боевой ASR: не закоммичено", f"{prod_dirty} файлов", "vnimanie")
    return s


def selfcheck_section() -> Section:
    s = Section("Ночная диагностика")
    path = DASH / "data/selfcheck.json"
    if not path.exists():
        s.add("диагностика", "ещё не запускалась", "vnimanie")
        return s
    data = json.loads(path.read_text())
    s.add("проверено", data.get("checked_at", "?"),
          "ok" if data.get("status") == "в порядке" else "vnimanie")
    # Текст диагностики кладём в левую колонку целиком: он и есть содержание.
    for line in data.get("facts", []):
        s.add(line, "")
    for line in data.get("problems", []):
        s.add(line, "требует внимания", "vnimanie")
    return s


MARK = {"ok": "·", "vnimanie": "!", "beda": "!!"}
COLOR = {"ok": "var(--ok)", "vnimanie": "var(--warn)", "beda": "var(--bad)"}


def render_html(sections: list[Section], day: str) -> str:
    now = datetime.now(timezone(timedelta(hours=3)))
    parts = [f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Утренний отчёт</title>
<style>
:root {{
  --bg: #fbfaf8; --fg: #23201d; --muted: #6b645d; --line: #e4ded6;
  --ok: #4a7c59; --warn: #b8860b; --bad: #a8433a; --card: #ffffff;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #1a1816; --fg: #ece7e1; --muted: #9a938b; --line: #332f2b;
    --ok: #7fb08a; --warn: #d9a441; --bad: #d97c72; --card: #221f1c;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.55
  -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
.wrap {{ max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }}
h1 {{ font-size: 1.6rem; margin: 0 0 4px; }}
.sub {{ color: var(--muted); margin-bottom: 28px; }}
section {{ background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; }}
h2 {{ font-size: 1.05rem; margin: 0 0 12px; display: flex; gap: 8px; align-items: baseline; }}
.row {{ display: flex; gap: 12px; padding: 6px 0; border-top: 1px solid var(--line); }}
.row:first-of-type {{ border-top: 0; }}
.what {{ flex: 1 1 55%; color: var(--muted); }}
.val {{ flex: 1 1 45%; font-weight: 500; }}
.beda .val {{ color: var(--bad); }}
.vnimanie .val {{ color: var(--warn); }}
.dot {{ width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; margin-top: 7px; }}
footer {{ color: var(--muted); font-size: .9rem; margin-top: 24px; }}
</style></head><body><div class="wrap">
<h1>Утренний отчёт</h1>
<div class="sub">{now.strftime('%d.%m.%Y, %H:%M')} по Москве · данные за {day}</div>"""]

    for sec in sections:
        dot = COLOR[sec.worst]
        parts.append(f'<section><h2><span class="dot" style="background:{dot}"></span>'
                     f'{html.escape(sec.title)}</h2>')
        for what, value, level in sec.rows:
            parts.append(f'<div class="row {level}"><div class="what">{html.escape(what)}</div>'
                         f'<div class="val">{html.escape(value)}</div></div>')
        parts.append("</section>")

    parts.append('<footer>Собирается таймером <code>dashboard-morning</code> '
                 'в 07:00 по Москве. Ночная диагностика — в 06:00.</footer>'
                 "</div></body></html>")
    return "\n".join(parts)


def render_text(sections: list[Section], day: str) -> str:
    out = [f"УТРЕННИЙ ОТЧЁТ — данные за {day}", ""]
    for sec in sections:
        out.append(f"## {sec.title}")
        for what, value, level in sec.rows:
            out.append(f"  {MARK[level]} {what}: {value}")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="за какой день считать (по умолчанию вчера)")
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args()

    day = args.day or (date.today() - timedelta(days=1)).isoformat()
    sections = [calls_section(day), selfcheck_section(), machines_section(),
                safety_section()]

    text = render_text(sections, day)
    (DASH / "data/morning.md").write_text(text, encoding="utf-8")
    print(text)

    if not args.no_publish:
        page = render_html(sections, day)
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(page)
            tmp = fh.name
        # Каталог принадлежит root — кладём через sudo, как это делает карта.
        subprocess.run(["sudo", "cp", tmp, str(PUBLISH_TO)], check=False)
        subprocess.run(["sudo", "chmod", "644", str(PUBLISH_TO)], check=False)
        Path(tmp).unlink(missing_ok=True)
        print(f"страница: https://72-56-25-105.nip.io/karta/morning.html")

    worst = [s.worst for s in sections]
    return 1 if "beda" in worst else 0


if __name__ == "__main__":
    sys.exit(main())
