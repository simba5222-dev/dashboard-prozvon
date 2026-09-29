#!/usr/bin/env python
"""Во что обошлось бы распознавание — по отделам и сценариям.

Вопрос «сколько стоит обработать все звонки» без разбивки бесполезен: ответ
на него всегда «дорого», а решать надо не про всё сразу, а про каждый поток
отдельно. Здесь тот же месяц разложен по тем, кто говорил, и по тому, зачем
звонили.

    ./scripts/cost_by_dept.py --fetch --since 2026-09-01 --until 2026-10-01
    ./scripts/cost_by_dept.py --json data/cost-sept.json

**В два шага, как и справочник линий.** ВАТС отвечает только питерскому
серверу, ключ к нему читает лишь `agent`; разбор идёт под `claude`, которому
принадлежит база. Поэтому `--fetch` кладёт выгрузку в файл, а счёт считается
отдельным запуском.

**Отдел определяется по добавочному номеру, а не по должности.** В ВАТС у
пятидесяти учёток стоит просто «Менеджер» — это и продажи, и прозвон, и
поиск техники. Добавочный есть и в справочнике учёток, и в нашей таблице
сотрудников, и только он их различает.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import connect  # noqa: E402

КЕШ = Path("data/vats-period.json.gz")

# Цена отложенного распознавания SpeechKit: ₽ за 15 секунд одного канала.
ЦЕНА_15С = 0.0381
# Цена одного разбора разговора в gpt-4o — по нашему же учёту за сентябрь.
ЦЕНА_РАЗБОРА = 0.0083
КУРС = 95.0

# Должность в ВАТС → отдел, для тех, кого нет в нашей таблице сотрудников.
ПО_ДОЛЖНОСТИ = {
    "hr": "кадры", "hh": "кадры",
    "механик": "механизация", "помощник главного механика": "механизация",
    "начальник автоколонны": "механизация",
    "менеджер по снабжению": "снабжение",
    "менеджер теплый прозвон": "прозвон",
    "роп": "руководство продаж",
    "admin": "администрирование",
    "усть-луга": "Усть-Луга",
}

УДАЛЁННО = r'''
import datetime, json
import httpx
from dotenv import dotenv_values
env = dotenv_values("/opt/asr/.env")
base = env["ASR_MEGAFON_VATS_URL"].rstrip("/")
key = env["ASR_MEGAFON_API_TOKEN"]
строки = []
д = datetime.date.fromisoformat("ОТ")
конец = datetime.date.fromisoformat("ДО")
while д < конец:
    к = min(д + datetime.timedelta(days=5), конец)
    r = httpx.get(base + "/history/json",
                  params={"start": д.strftime("%Y-%m-%dT00:00:00"),
                          "end": к.strftime("%Y-%m-%dT00:00:00")},
                  headers={"X-API-KEY": key}, timeout=300)
    r.raise_for_status()
    строки.extend(r.json())
    д = к
print(json.dumps(строки, ensure_ascii=False))
'''


def настройки() -> Settings:
    """`.env` читает только claude, а ходить в ВАТС может только agent.

    Шагу `--fetch` из `.env` ничего не нужно: адрес питерского сервера и путь
    к ключу лежат в значениях по умолчанию. Тот же приём, что в
    `fetch_records.py` и `sync_lines.py`.
    """
    try:
        return Settings()
    except PermissionError:
        return Settings(_env_file=None)


def цифры(v: object) -> str:
    return re.sub(r"\D", "", str(v or ""))[-10:]


def сходить(settings: Settings, с: str, по: str) -> int:
    if not Path(settings.record_ssh_key).exists():
        print(f"ключ {settings.record_ssh_key} недоступен — запускайте под agent")
        return 1
    тело = УДАЛЁННО.replace("ОТ", с).replace("ДО", по)
    try:
        готово = subprocess.run(
            ["ssh", "-i", settings.record_ssh_key, "-o", "ConnectTimeout=15",
             "-o", "BatchMode=yes", settings.record_ssh_host,
             "cd /opt/asr && .venv/bin/python -"],
            input=тело, capture_output=True, text=True, timeout=600, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"история ВАТС не получена: {(getattr(exc, 'stderr', '') or str(exc))[:300]}")
        return 1
    строки = json.loads(готово.stdout.strip().splitlines()[-1])
    КЕШ.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(КЕШ, "wt", encoding="utf-8") as fh:
        json.dump(строки, fh, ensure_ascii=False)
    print(f"выгружено записей: {len(строки)} → {КЕШ}")
    print("теперь под claude: ./scripts/cost_by_dept.py")
    return 0


def отделы(conn, учётки: dict) -> dict[str, tuple[str, str]]:
    """Добавочный → (отдел, имя). Наша таблица сотрудников главнее ВАТС."""
    из_базы = {}
    for r in conn.execute("SELECT ext, dept, display_name FROM managers "
                          "WHERE ext IS NOT NULL AND ext <> ''"):
        из_базы.setdefault(str(r["ext"]).strip(), (r["dept"], r["display_name"]))
    итог = {}
    for логин, u in учётки.items():
        ext = str(u.get("ext") or "").strip()
        if not ext:
            continue
        имя = str(u.get("name") or логин)
        if ext in из_базы:
            итог[ext] = из_базы[ext]
            continue
        должность = str(u.get("position") or "").strip().casefold()
        отдел = ПО_ДОЛЖНОСТИ.get(должность)
        if отдел is None and "hr" in имя.casefold():
            отдел = "кадры"
        итог[ext] = (отдел or "вне отделов", имя)
    return итог


def главное(args) -> int:
    settings = настройки()
    if args.fetch:
        return сходить(settings, args.since, args.until)
    if not КЕШ.exists():
        print(f"нет {КЕШ} — сначала под agent: ./scripts/cost_by_dept.py --fetch")
        return 1
    with gzip.open(КЕШ, "rt", encoding="utf-8") as fh:
        строки = json.load(fh)

    conn = connect(settings.db_path)
    учётки = {}
    путь = Path("data/vats-users.json")
    if путь.exists():
        учётки = {u["login"]: u for u in json.loads(путь.read_text(encoding="utf-8"))}
    по_ext = отделы(conn, учётки)
    линии = {r["phone10"]: (r["name"], r["kind"])
             for r in conn.execute("SELECT phone10, name, kind FROM lines")}
    conn.close()

    по_отделам: dict[str, dict] = {}
    по_сценариям: dict[str, dict] = {}

    def копить(куда: dict, ключ: str, секунд: int) -> None:
        строка = куда.setdefault(ключ, {"звонков": 0, "секунд": 0})
        строка["звонков"] += 1
        строка["секунд"] += секунд

    for x in строки:
        секунд = int(x.get("duration") or 0)
        if секунд < args.min_sec:
            continue
        учётка = учётки.get(str(x.get("user") or ""))
        ext = str((учётка or {}).get("ext") or "").strip()
        отдел = по_ext.get(ext, ("вне отделов", ""))[0]
        копить(по_отделам, отдел, секунд)

        входящий = x.get("type") == "in"
        имя_линии, вид = линии.get(цифры(x.get("diversion")), ("", ""))
        if входящий and вид == "рекламная":
            сценарий = f"входящий с рекламы · {имя_линии}"
        elif входящий and отдел == "продажи":
            сценарий = "входящий на прямой номер продаж"
        elif входящий:
            сценарий = f"входящий · {отдел}"
        elif отдел == "прозвон":
            сценарий = "исходящий · тёплый прозвон"
        elif отдел == "поиск":
            сценарий = "исходящий · поиск техники"
        else:
            сценарий = f"исходящий · {отдел}"
        копить(по_сценариям, сценарий, секунд)

    def напечатать(заголовок: str, данные: dict) -> None:
        print(f"\n{заголовок}")
        print(f"  {'':<38}{'звонков':>9}{'минут':>8}{'₽ Яндекс':>11}{'$ разбор':>10}")
        всего = {"звонков": 0, "секунд": 0}
        for ключ, v in sorted(данные.items(), key=lambda p: -p[1]["секунд"]):
            минут = v["секунд"] / 60
            руб = минут * 2 * 60 / 15 * ЦЕНА_15С
            дол = v["звонков"] * ЦЕНА_РАЗБОРА
            print(f"  {ключ[:38]:<38}{v['звонков']:>9}{минут:>8.0f}{руб:>11,.0f}{дол:>10.0f}"
                  .replace(",", " "))
            всего["звонков"] += v["звонков"]; всего["секунд"] += v["секунд"]
        минут = всего["секунд"] / 60
        print(f"  {'ИТОГО':<38}{всего['звонков']:>9}{минут:>8.0f}"
              f"{минут*2*60/15*ЦЕНА_15С:>11,.0f}{всего['звонков']*ЦЕНА_РАЗБОРА:>10.0f}"
              .replace(",", " "))

    print(f"разговоров длиннее {args.min_sec} с в выгрузке: "
          f"{sum(v['звонков'] for v in по_отделам.values())}")
    напечатать("ПО ОТДЕЛАМ", по_отделам)
    напечатать("ПО СЦЕНАРИЯМ", по_сценариям)

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"порог_секунд": args.min_sec, "по отделам": по_отделам,
             "по сценариям": по_сценариям, "цена_15с": ЦЕНА_15С,
             "цена_разбора": ЦЕНА_РАЗБОРА}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nсохранено: {args.json}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fetch", action="store_true", help="Сходить в ВАТС (под agent).")
    p.add_argument("--since", default="2026-09-01")
    p.add_argument("--until", default="2026-10-01")
    p.add_argument("--min-sec", type=int, default=40)
    p.add_argument("--json", default="")
    raise SystemExit(главное(p.parse_args()))
