#!/usr/bin/env python
"""Справочник наших линий из истории ВАТС.

На какой номер позвонил клиент — единственный надёжный признак, что звонок
пришёл по объявлению, а не менеджеру лично. ВАТС отдаёт его полем
`diversion` у каждого входящего и **сама подписывает рекламные линии**:
«Авито СПБ», «Сайт МСК», «Яндекс директ Москва», «Виджет Реклама». Имя есть
только у них — прямые номера сотрудников приходят безымянными.

Раньше линию пытались определить по списку менеджеров: номера, которого нет
в списке, считали чужим. За трое суток так вышло 257 «чужих» номеров из 372
звонков — почти все из них на самом деле прямые номера сотрудников, просто
не записанные у нас. Имя линии решает это без догадок.

    ./scripts/sync_lines.py --fetch --days 14   под agent: сходить в ВАТС
    ./scripts/sync_lines.py                     под claude: посмотреть
    ./scripts/sync_lines.py --apply             под claude: записать

**В два шага, и это не придирка.** ВАТС не пускает зарубежные адреса, ходить
туда можно только с питерского сервера, а ключ к нему читает лишь `agent`.
База же принадлежит `claude`: запиши в неё из-под `agent` — и служебные файлы
SQLite сменят владельца, а служба потеряет доступ. Поэтому `--fetch` кладёт
выгрузку в файл, а запись идёт отдельным запуском. Так же устроен
`sync_vats_users.py`.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, save_line  # noqa: E402

logger = logging.getLogger("sync_lines")


def настройки() -> Settings:
    """`.env` читает только claude, а ходить в ВАТС может только agent.

    Шагу `--fetch` из `.env` ничего не нужно: адрес питерского сервера и путь
    к ключу лежат в значениях по умолчанию. Тот же приём, что в
    `fetch_records.py`.
    """
    try:
        return Settings()
    except PermissionError:
        return Settings(_env_file=None)

# Тело выполняется на питерском сервере его же питоном и его же ключом.
УДАЛЁННО = r'''
import collections, datetime, json
import httpx
from dotenv import dotenv_values
env = dotenv_values("/opt/asr/.env")
base = env["ASR_MEGAFON_VATS_URL"].rstrip("/")
key = env["ASR_MEGAFON_API_TOKEN"]
end = datetime.datetime.now(datetime.timezone.utc)
start = end - datetime.timedelta(days=ДНЕЙ)
rows = httpx.get(
    base + "/history/json",
    params={"start": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%S")},
    headers={"X-API-KEY": key}, timeout=180,
).json()
имена, счёт = {}, collections.Counter()
исходящие = set()
покаждому = []
for x in rows:
    номер = str(x.get("diversion") or "").lstrip("+")[-10:]
    if not номер:
        continue
    if x.get("type") == "in":
        счёт[номер] += 1
        покаждому.append({"uid": str(x.get("uid") or ""),
                          "client": str(x.get("client") or ""),
                          "start": str(x.get("start") or ""),
                          "номер": номер})
        имя = str(x.get("telnum_name") or "").strip()
        if имя:
            имена[номер] = имя
    elif x.get("type") == "out":
        # С рекламной линии наружу не звонят: если с номера уходили звонки,
        # это рабочий номер сотрудника.
        исходящие.add(номер)
print(json.dumps({"имена": имена, "счёт": счёт, "исходящие": sorted(исходящие),
                  "покаждому": покаждому}, ensure_ascii=False))
'''


def вид(имя: str, звонил_наружу: bool) -> str:
    if имя:
        return "рекламная"
    return "прямой" if звонил_наружу else "неизвестная"


КЕШ = Path("data/vats-lines.json")


def сходить(settings: Settings, days: int) -> int:
    """Спросить историю у ВАТС через питерский сервер и сложить в файл."""
    if not Path(settings.record_ssh_key).exists():
        print(f"ключ {settings.record_ssh_key} недоступен — запускайте под agent")
        return 1
    try:
        готово = subprocess.run(
            ["ssh", "-i", settings.record_ssh_key, "-o", "ConnectTimeout=15",
             "-o", "BatchMode=yes", settings.record_ssh_host,
             "cd /opt/asr && .venv/bin/python -"],
            input=УДАЛЁННО.replace("ДНЕЙ", str(days)),
            capture_output=True, text=True, timeout=300, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        print(f"история ВАТС не получена: {stderr.strip()[:300] or exc}")
        return 1
    try:
        данные = json.loads(готово.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"ответ не разобран: {готово.stdout[:300]}")
        return 1
    КЕШ.parent.mkdir(parents=True, exist_ok=True)
    КЕШ.write_text(json.dumps(данные, ensure_ascii=False), encoding="utf-8")
    print(f"выгрузка сохранена: {КЕШ}. Теперь под claude: ./scripts/sync_lines.py --apply")
    return 0


def момент(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def добрать_номера(conn, записи: list[dict]) -> int:
    """Проставить набранный номер у звонков, где он пуст.

    **Сшивать приходится по телефону и времени, а не по идентификатору.**
    Один и тот же разговор приходит к нам двумя путями, и опознаётся
    по-разному: через вебхук с питерского сервера — родным кодом ВАТС
    (`ML31O71NH0000044`), через опрос Synergy — её собственным номером строки
    (`8230168`). Пересекается меньше десятой части. Зато телефон собеседника
    и минута начала совпадают всегда.

    Окно в две минуты: у Synergy и ВАТС время начала расходится на секунды,
    а два разных звонка с одного номера в одну минуту — случай, которого в
    этих данных не встречается.
    """
    свежие: dict[str, list[tuple[float, str]]] = {}
    прямо: dict[str, str] = {}
    for r in записи:
        номер = r.get("номер") or ""
        if not номер:
            continue
        if r.get("uid"):
            прямо[r["uid"]] = номер
        когда = момент(r.get("start") or "")
        телефон = re.sub(r"\D", "", str(r.get("client") or ""))[-10:]
        if когда is not None and телефон:
            свежие.setdefault(телефон, []).append((когда, номер))

    добрано = 0
    for uid, номер in прямо.items():
        добрано += conn.execute(
            "UPDATE calls SET diversion = ? WHERE uid = ? AND COALESCE(diversion,'') = ''",
            (номер, uid)).rowcount

    пустые = conn.execute(
        """SELECT uid, client_phone, started_at FROM calls
           WHERE direction = 'in' AND COALESCE(diversion,'') = ''"""
    ).fetchall()
    for row in пустые:
        телефон = re.sub(r"\D", "", str(row["client_phone"] or ""))[-10:]
        когда = момент(row["started_at"])
        if когда is None or телефон not in свежие:
            continue
        близкие = [n for t, n in свежие[телефон] if abs(t - когда) <= 120]
        if len(set(близкие)) != 1:
            # Ноль — этого звонка в выгрузке нет; больше одного — сшивка
            # неоднозначна. В обоих случаях лучше пусто, чем наугад.
            continue
        добрано += conn.execute(
            "UPDATE calls SET diversion = ? WHERE uid = ?", (близкие[0], row["uid"])).rowcount
    return добрано


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--fetch", action="store_true",
                        help="Сходить в ВАТС через питерский сервер (под agent).")
    parser.add_argument("--apply", action="store_true", help="Записать в справочник.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = настройки()
    if args.fetch:
        return сходить(settings, args.days)
    if not КЕШ.exists():
        print(f"нет {КЕШ} — сначала под agent: ./scripts/sync_lines.py --fetch")
        return 1
    данные = json.loads(КЕШ.read_text(encoding="utf-8"))

    имена = данные["имена"]
    счёт = данные["счёт"]
    исходящие = set(данные["исходящие"])
    now = datetime.now(timezone.utc).isoformat()

    строки = []
    for номер, n in sorted(счёт.items(), key=lambda p: -p[1]):
        имя = имена.get(номер, "")
        строки.append({"phone10": номер, "name": имя,
                       "kind": вид(имя, номер in исходящие),
                       "calls_in": n, "seen_at": now})

    рекламных = sum(1 for r in строки if r["kind"] == "рекламная")
    звонков = sum(r["calls_in"] for r in строки if r["kind"] == "рекламная")
    print(f"линий за {args.days} суток: {len(строки)}, из них рекламных: {рекламных}")
    print(f"входящих на рекламные линии: {звонков} из {sum(счёт.values())}")
    for r in строки:
        if r["kind"] == "рекламная":
            print(f"  {r['name']:<24} {r['phone10']}  входящих {r['calls_in']}")

    if not args.apply:
        print("\nничего не записано. Для записи: --apply")
        return 0

    conn = connect(settings.db_path)
    init_schema(conn)
    for r in строки:
        save_line(conn, **r)

    добрано = добрать_номера(conn, данные.get("покаждому") or [])
    conn.commit()
    conn.close()
    print(f"\nзаписано линий: {len(строки)}, набранный номер добран у звонков: {добрано}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
