#!/usr/bin/env python
"""Разобрать звонки с рекламных линий — российскими ушами и головой.

Эти звонки приходят по объявлению, то есть почти всегда содержат запрос на
технику. Заявку по ним CRM заводит сама, ещё до разбора; здесь мы смотрим на
**содержание** — что именно просили, что менеджер выяснил и чего не выяснил.

Распознаёт SpeechKit, разбирает ассистент Яндекса с доступом к нашей базе
знаний. Всё внутри России, за границу не уходит ничего.

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

from app import yandex  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, save_ad_call  # noqa: E402

logger = logging.getLogger("ad_calls")

ЧТО_ЗА_ТЕХНИКА = """Прочитай расшифровку телефонного разговора и назови, какая техника
нужна позвонившему. Ответь одним-двумя словами, как технику называют в жизни.
Если о технике речи не было — ответь словом «нет».

{текст}"""

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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    if not (settings.yandex_stt_configured and settings.yandex_assistant_id):
        print("нет ключа Яндекса или не задан DASH_YANDEX_ASSISTANT_ID")
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
        # Два хода. Сначала коротким вопросом узнаём технику и спрашиваем
        # память, что по ней положено выяснить; затем разбираем разговор
        # прямым вызовом модели. Одним ходом нельзя: ассистент ищет в памяти
        # по всему сообщению, и расшифровка целиком роняет поиск.
        техника = yandex.complete(
            ЧТО_ЗА_ТЕХНИКА.format(текст=текст[:4000]),
            api_key=settings.yandex_api_key, folder=settings.yandex_folder,
            max_tokens=20).strip().strip(".!»«\"").lower()
        чеклист = ""
        if техника and техника != "нет":
            чеклист = yandex.ask(
                settings.yandex_assistant_id,
                f"Какие обязательные вопросы надо задать клиенту про «{техника}»? "
                f"Если в материалах их нет — так и скажи.",
                api_key=settings.yandex_api_key, folder=settings.yandex_folder)
        ответ = yandex.complete(
            ЗАДАНИЕ.format(линия=r["line"], текст=текст[:12000],
                           чеклист=чеклист or "(в базе знаний по этой технике ничего нет)"),
            api_key=settings.yandex_api_key, folder=settings.yandex_folder,
            max_tokens=1500)
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
