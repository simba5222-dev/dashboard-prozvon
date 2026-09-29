#!/usr/bin/env python
"""Разобрать звонки с рекламных линий — российскими ушами и головой.

Эти звонки приходят по объявлению, то есть почти всегда содержат запрос на
технику. Заявку по ним CRM заводит сама, ещё до разбора; здесь мы смотрим на
**содержание** — что именно просили, что менеджер выяснил и чего не выяснил.

Распознаёт SpeechKit, разбирает YandexGPT Lite. Всё внутри России, за
границу не уходит ничего.

    ./scripts/analyze_ad_calls.py --since 2026-09-22 --until 2026-09-28
    ./scripts/analyze_ad_calls.py --since … --until … --apply

Результат кладётся в таблицу `ad_calls` и показывается на `/ads`, где
владелец и агент помечают каждый разбор: верно, неверно или спорно. Из этих
отметок вырастает проверочный набор — без него смену модели нельзя измерить.

**Отметки человека скрипт не трогает.** Повторный запуск обновит разбор и
оставит вердикт на месте: иначе вечерний прогон стирал бы утреннюю работу.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import knowledge, yandex  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, save_ad_call  # noqa: E402

logger = logging.getLogger("ad_calls")

ЗАДАНИЕ = """Разбери телефонный разговор. Звонок входящий, на рекламную линию «{линия}» —
человек позвонил по объявлению, значит почти наверняка ему нужна техника.

В расшифровке `operator` — наш менеджер, `client` — позвонивший. Это известно
из устройства записи, определять по содержанию не нужно.

РАСШИФРОВКА:
{текст}

Верни только JSON, без пояснений вокруг:
{{
  "is_request": true/false — просил ли звонивший технику,
  "equipment": "какая техника нужна, словами клиента; пусто, если речи не было",
  "object": "адрес или объект, если прозвучал",
  "when": "сроки, если прозвучали — словами клиента, без домысливания дат",
  "summary": "1-2 предложения: о чём говорили и чем кончилось",
  "asked": ["обязательные вопросы по этой технике, которые менеджер ЗАДАЛ"],
  "missed": ["обязательные вопросы по этой технике, которые менеджер НЕ задал"],
  "next_step": "о чём договорились",
  "quality": 1-5 — насколько разговор доведён до результата,
  "quality_note": "коротко, за что такая оценка"
}}

ЧТО НАДО БЫЛО СПРОСИТЬ ПО ЭТОЙ ТЕХНИКЕ (из нашей базы знаний):
{чеклист}

Если список пуст — оставь `asked` и `missed` пустыми и скажи об этом в quality_note.

Ничего не придумывай. Дат, чисел и адресов, которых нет в расшифровке, быть
не должно. Не прозвучало — пустая строка."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--min-sec", type=int, default=40)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Записать разбор в базу.")
    parser.add_argument("--redo", action="store_true", help="Переразобрать уже разобранные.")
    parser.add_argument("--uid", default="", help="Только эти звонки, через запятую.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    if not settings.yandex_stt_configured:
        print("нет ключа Яндекса")
        return 1

    conn = connect(settings.db_path)
    init_schema(conn)
    rows = conn.execute("""
        SELECT k.uid, k.started_at, k.client_phone, k.duration_sec, l.name AS line,
               (SELECT verdict FROM ad_calls a WHERE a.call_uid = k.uid) AS verdict,
               (SELECT transcript FROM ad_calls a WHERE a.call_uid = k.uid) AS было
        FROM calls k
        JOIN lines l ON l.phone10 = substr(replace(replace(replace(
             k.diversion,'+',''),' ',''),'-',''), -10)
        WHERE l.kind = 'рекламная' AND k.direction = 'in'
          AND k.local_date BETWEEN ? AND ? AND k.duration_sec >= ?
        ORDER BY k.started_at
    """, (args.since, args.until, args.min_sec)).fetchall()

    если_эти = {x.strip() for x in args.uid.split(",") if x.strip()}
    if если_эти:
        rows = [r for r in rows if r["uid"] in если_эти]
    записи = Path(settings.records_dir)
    дела = []
    нет_записи = 0
    for r in rows:
        путь = записи / f"{r['uid']}.mp3"
        if not путь.exists():
            нет_записи += 1
            continue
        if r["было"] and not args.redo:
            continue
        дела.append((r, путь))
    if args.limit:
        дела = дела[: args.limit]

    print(f"звонков на рекламные линии за период: {len(rows)}, "
          f"без записи {нет_записи}, к разбору {len(дела)}")
    if not args.apply:
        for r, _ in дела[:5]:
            print(f"  {r['started_at'][:16]}  {r['line']:<18} {r['duration_sec']:>4} с  {r['uid']}")
        print("\nничего не записано. Для разбора: --apply")
        conn.close()
        return 0

    разобрано = пусто = 0
    for индекс, (r, путь) in enumerate(дела, 1):
        logger.info("[%s/%s] %s · %s · %s с", индекс, len(дела),
                    r["uid"], r["line"], r["duration_sec"])
        текст = yandex.transcribe_dialog(str(путь), api_key=settings.yandex_api_key,
                                         folder=settings.yandex_folder)
        if not текст.strip():
            logger.warning("  расшифровка пустая, пропускаем")
            пусто += 1
            continue
        # Чек-лист берём из своего кода, а не спрашиваем у облачной памяти.
        # 29.09.2026 посчитали по биллингу: обращение к памяти на каждом
        # звонке почти удваивало счёт за разбор — 6,9 ₽ против 1,8 ₽, — и
        # всё ради списка, который лежит в `knowledge.py` даром. Память
        # осталась складом и местом, где можно спросить «что уточнять про
        # ямобур»; внутри конвейера ей делать нечего.
        вопросы = knowledge.questions_for(текст)
        чеклист = ("\n".join(f"- {в}" for в in вопросы) if вопросы
                   else "(по этой технике списка вопросов у нас нет)")
        ответ = yandex.complete(
            ЗАДАНИЕ.format(линия=r["line"], текст=текст[:12000], чеклист=чеклист),
            api_key=settings.yandex_api_key, folder=settings.yandex_folder,
            model=settings.yandex_model, max_tokens=1500)
        разбор = yandex.parse_json(ответ)
        if разбор is None:
            logger.warning("  разбор не разобрался: %s", (ответ or "")[:120])
        save_ad_call(conn, call_uid=r["uid"], line=r["line"], engine="yandex",
                     transcript=текст,
                     analysis_json=json.dumps(разбор, ensure_ascii=False) if разбор else None,
                     made_at=datetime.now(timezone.utc).isoformat())
        conn.commit()
        разобрано += 1
        if разбор:
            logger.info("  %s · %s", "запрос" if разбор.get("is_request") else "не запрос",
                        (разбор.get("equipment") or "техника не названа")[:60])
    conn.close()
    print(f"\nразобрано: {разобрано}, пустых расшифровок: {пусто}")
    print("смотреть и помечать: /dashboard/ads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
