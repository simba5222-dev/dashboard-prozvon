"""Показательные данные, пока нет доступа к ВАТС и Synergy.

Все строки помечены `is_demo = 1`, и дашборд говорит об этом прямо на экране.
Смысл не в красивой картинке: без данных невозможно понять, читаются ли экраны
и те ли числа на них выведены. Как только появятся настоящие источники, демо
выключается одной настройкой `DASH_DEMO_MODE=false`.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone

from app.db import save_call, save_card_check, save_transcript, upsert_manager
from app.stats import local_parts

DEMO_MANAGER = {
    "vats_login": "demo.manager",
    "display_name": "Демо-менеджер (показательные данные)",
    "synergy_user": None,
    "plan_calls": None,
    "active": 1,
    "is_demo": 1,
}

DEMO_DIALOG = """оператор [00:00]: Алло, Сергей Петрович? Добрый день, это Техноресурс.
клиент [00:03]: Да, здравствуйте.
оператор [00:05]: Мы в прошлом месяце вам автокран давали на объект в Мытищах.
     Хотел спросить, техника в ближайшее время нужна будет?
клиент [00:12]: Да, вообще да. У нас сейчас на Ленинградке площадка начинается,
     туда экскаватор потребуется, и ямобур скорее всего.
оператор [00:21]: Экскаватор гусеничный или колёсный?
клиент [00:24]: Гусеничный, там грунт тяжёлый.
оператор [00:27]: Понял. По срокам когда ориентировочно?
клиент [00:30]: Числа с двадцатого, точнее к концу недели скажу.
оператор [00:34]: Хорошо, я тогда поставлю себе напоминание и наберу вас в пятницу.
     ИНН компании подскажете? У нас в карточке не заполнено.
клиент [00:41]: Сейчас, секунду... семь семь ноль шесть, дальше я в документах гляну,
     давайте я вам на почту скину.
оператор [00:50]: Отлично, жду. Спасибо, до связи!"""

DEMO_ANALYSIS = {
    "потребность": "есть",
    "техника": ["Гусеничный Экскаватор", "Ямобур"],
    "объект": "площадка на Ленинградском шоссе",
    "сроки": "ориентировочно с 20 числа, уточнение в пятницу",
    "инн": "не получен, клиент обещал прислать на почту",
    "оценка": "разговор результативный: потребность выявлена, следующий шаг назначен",
}


def seed(conn: sqlite3.Connection, *, offset_hours: int, days: int = 5) -> int:
    """Наполнить базу показательными данными. Возвращает число созданных звонков."""
    rnd = random.Random(20260916)  # фиксируем: перезапуск не должен менять картинку
    upsert_manager(conn, **DEMO_MANAGER)

    now = datetime.now(timezone.utc)
    created = 0
    for day_back in range(days):
        day_start = (now - timedelta(days=day_back)).replace(
            hour=6, minute=0, second=0, microsecond=0
        )  # 06:00 UTC ≈ 09:00 по Москве
        # План 120, но живой человек его то недовыполняет, то перевыполняет
        total = rnd.randint(78, 126) if day_back else rnd.randint(40, 70)
        for i in range(total):
            minute_offset = rnd.randint(0, 9 * 60 - 1)
            started = day_start + timedelta(minutes=minute_offset)
            # Дозванивается примерно до 40% — остальное гудки и сбросы
            if rnd.random() < 0.40:
                duration = rnd.choice([18, 24, 35, 47, 62, 88, 115, 140, 190, 240])
            else:
                duration = rnd.randint(0, 14)
            uid = f"demo-{day_back}-{i}"
            local_date, local_hour = local_parts(started.isoformat(), offset_hours)
            if save_call(
                conn,
                uid=uid,
                vats_login=DEMO_MANAGER["vats_login"],
                client_phone=f"79{rnd.randint(100000000, 999999999)}",
                direction="out",
                status="success" if duration >= 15 else "missed",
                started_at=started.isoformat().replace("+00:00", "Z"),
                local_date=local_date,
                local_hour=local_hour,
                wait_sec=rnd.randint(2, 12),
                duration_sec=duration,
                record_url=f"https://demo/records/{uid}.mp3" if duration >= 15 else None,
                is_demo=1,
                fetched_at=now.isoformat(),
            ):
                created += 1

            if duration < 15:
                continue

            # Дисциплина заполнения: что-то менеджер вносит, что-то забывает.
            # ИНН забывают чаще всего — его надо спрашивать у клиента.
            filled = rnd.random()
            save_card_check(
                conn,
                call_uid=uid,
                contact_id=f"demo-contact-{rnd.randint(1, 60)}",
                contact_found=1,
                need_filled=int(filled > 0.20),
                frequency_filled=int(filled > 0.35),
                objects_filled=int(filled > 0.45),
                inn_filled=int(filled > 0.70),
                task_created=int(filled > 0.30),
                checked_at=now.isoformat(),
                is_demo=1,
            )

            # Расшифровку кладём только к одному звонку: показать, как выглядит
            # экран разбора. Делать её ко всем — значит соврать про нагрузку.
            if day_back == 0 and duration >= 140 and not _has_demo_transcript(conn):
                save_transcript(
                    conn,
                    call_uid=uid,
                    text=DEMO_DIALOG,
                    analysis_json=_json(DEMO_ANALYSIS),
                    created_at=now.isoformat(),
                    is_demo=1,
                )
    conn.commit()
    return created


def _has_demo_transcript(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM transcripts LIMIT 1").fetchone() is not None


def _json(data: dict) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)
