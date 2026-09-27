#!/usr/bin/env python
"""Объём телефонии за период: звонки, минуты, разбивка по отделам.

Нужен для одной задачи — понять, во что обойдётся разбор всех разговоров
компании. Поэтому считает не «сколько звонков», а сколько из них вообще
состоялись и сколько в них минут: платим мы за минуты звука, а не за
попытки дозвона.

    ./scripts/call_volume.py --days 92
    ./scripts/call_volume.py --days 92 --out data/call-volume.json

**Источник — Synergy, а не ВАТС.** У ВАТС выгрузка истории закрыта:
`GET /crmapi/v1/history/json` отвечает `501 Not Implemented` (проверено
24.09.2026 с боевого сервера, где ВАТС доступна). Synergy получает те же
звонки интеграцией и хранит их с длительностью, добавочным и именем
сотрудника. Оговорка: если интеграция когда-то лежала, эти звонки в
Synergy не попали, и здесь их тоже не будет.

Отдел определяется по добавочному номеру из карточки звонка: он лежит в
`custom-29843`, а имя сотрудника — в `custom-28722`. Соответствие
«добавочный → отдел» берётся из нашей таблицы сотрудников; для кого его
нет, звонки считаются отдельной строкой «вне отделов» — это склад,
бухгалтерия и прочие, кого мы не заводили.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collector import SynergyClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect  # noqa: E402

logger = logging.getLogger("call_volume")

FIELD_EXT = "custom-29843"
FIELD_WHO = "custom-28722"

# Должность в ВАТС → отдел. Сопоставление ручное и другим быть не может:
# должности там заполняли люди, единых написаний никто не гарантировал.
BY_POSITION = {
    "hr": "кадры",
    "hh": "кадры",
    "механик": "механизация",
    "помощник главного механика": "механизация",
    "начальник автоколонны": "механизация",
    "менеджер по снабжению": "снабжение",
    "менеджер теплый прозвон": "прозвон",
    "роп": "руководство продаж",
    "admin": "администрирование",
}


def load_vats_positions(path: Path) -> dict[str, tuple[str, str]]:
    """Добавочный → (отдел, имя) по справочнику учёток ВАТС."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for row in rows:
        ext = str(row.get("ext") or "").strip()
        if not ext:
            continue
        position = str(row.get("position") or "").strip()
        name = str(row.get("name") or "").strip()
        dept = BY_POSITION.get(position.casefold())
        if dept is None and "hr" in name.casefold():
            # «Авито HR» заведён менеджером, хотя занимается подбором людей.
            dept = "кадры"
        out[ext] = (dept or "вне отделов", name)
    return out


def ext_of(customs: dict) -> str:
    value = customs.get(FIELD_EXT)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=92)
    parser.add_argument("--out", default="data/call-volume.json")
    parser.add_argument("--max-pages", type=int, default=4000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries,
    )
    conn = connect(settings.db_path)
    depts: dict[str, tuple[str, str]] = {}
    for row in conn.execute(
            "SELECT ext, dept, display_name FROM managers WHERE ext IS NOT NULL AND ext <> ''"):
        depts.setdefault(str(row["ext"]).strip(), (row["dept"], row["display_name"]))
    conn.close()

    positions = load_vats_positions(Path("data/vats-users.json"))
    since = (date.today() - timedelta(days=args.days - 1)).isoformat()
    # Итоги копим по трём разрезам сразу: месяцы — для динамики, отделы —
    # для разговора о деньгах, люди — чтобы было видно, кто эти минуты даёт.
    months: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    by_dept: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    by_person: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    by_dept_month: dict[str, dict] = defaultdict(lambda: defaultdict(float))

    total = 0
    page = 1
    reached = ""
    while page <= args.max_pages:
        try:
            rows = client.get("telephony-calls", per_page=50, sort="-created-at",
                              **{"page[number]": page}).get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("страница %s не прочитана: %s", page, exc)
            break
        if not rows:
            break
        stop = False
        for item in rows:
            attrs = item.get("attributes") or {}
            started = str(attrs.get("started-at") or attrs.get("created-at") or "")
            day = started[:10]
            if day and day < since:
                stop = True
                continue
            reached = day or reached
            customs = attrs.get("customs") or {}
            ext = ext_of(customs)
            dept, who = depts.get(ext, ("", ""))
            if not dept:
                dept, who = positions.get(ext, ("вне отделов", who))
            who = who or str(customs.get(FIELD_WHO) or "").strip()
            direction = "входящие" if attrs.get("direction") == "incoming" else "исходящие"
            seconds = float(attrs.get("duration") or 0)
            answered = seconds > 0

            for bucket, key in ((months, day[:7]), (by_dept, dept),
                                (by_dept_month, f"{dept}|{day[:7]}"),
                                (by_person, f"{dept}|{ext}|{who}")):
                bucket[key][f"{direction}_всего"] += 1
                bucket[key][f"{direction}_состоялось"] += int(answered)
                bucket[key][f"{direction}_секунд"] += seconds
                bucket[key]["записей"] += int(bool(attrs.get("recording")))
            total += 1
        if stop:
            break
        if page % 100 == 0:
            logger.info("  страница %s, звонков %s, дошли до %s", page, total, reached)
        page += 1

    result = {
        "период": f"{since}…{date.today().isoformat()}",
        "звонков всего": total,
        "по месяцам": {k: dict(v) for k, v in sorted(months.items())},
        "по отделам": {k: dict(v) for k, v in sorted(by_dept.items())},
        "по отделам и месяцам": {k: dict(v) for k, v in sorted(by_dept_month.items())},
        "по людям": {k: dict(v) for k, v in sorted(by_person.items())},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"звонков разобрано: {total}, итоги в {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
