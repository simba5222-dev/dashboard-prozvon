#!/usr/bin/env python
"""Разобрать звонки с рекламных линий — российскими ушами и головой.

Эти звонки приходят по объявлению, то есть почти всегда содержат запрос на
технику. Заявку по ним CRM заводит сама, ещё до разбора; здесь мы смотрим на
**содержание** — что именно просили, что менеджер выяснил и чего не выяснил.

**Расшифровку берём готовую из CRM, а не делаем заново.** Боевой сервер
распознаёт эти же звонки своим путём и кладёт текст в поле «Транскрибация»
(`custom-30604`) карточки звонка. До 29.09.2026 мы распознавали их второй раз
и платили за это дважды — при том что оба раза получался один и тот же
разговор. Если в CRM текста нет, распознаём сами: ключ `--fresh` заставляет
делать это всегда.

Разбирает YandexGPT Lite. Всё внутри России, за границу не уходит ничего.

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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app import knowledge, yandex  # noqa: E402
from app.collector import SynergyClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, save_ad_call  # noqa: E402

logger = logging.getLogger("ad_calls")

ЗАДАНИЕ = """Разбери телефонный разговор. Звонок входящий, на рекламную линию «{линия}» —
человек позвонил по объявлению, значит почти наверняка ему нужна техника.

В расшифровке `менеджер` — наш сотрудник, `клиент` — позвонивший. Это известно
из устройства записи, определять по содержанию не нужно и нельзя. В старых
расшифровках те же роли подписаны `operator` и `client`.

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


ПОЛЕ_ТРАНСКРИПТА = "custom-30604"   # «Транскрибация» в карточке звонка


def из_crm(client: SynergyClient, uid: str) -> str:
    """Готовая расшифровка звонка из CRM, если она там есть.

    Ключ звонка у нас двух видов: цифровой — это номер строки Synergy, и её
    можно спросить напрямую; буквенный — код ВАТС, по нему строка не ищется.
    Второй случай встречается у звонков, пришедших вебхуком, и для них
    расшифровку придётся делать самим.
    """
    if not uid.isdigit():
        return ""
    try:
        данные = client.get(f"telephony-calls/{uid}")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("  расшифровка из CRM не прочитана: %s", str(exc)[:120])
        return ""
    customs = (((данные.get("data") or {}).get("attributes") or {}).get("customs") or {})
    текст = str(customs.get(ПОЛЕ_ТРАНСКРИПТА) or "")
    # В CRM текст лежит размеченным под HTML — переводы строк там тегами.
    текст = re.sub(r"<br\s*/?>", "\n", текст)
    текст = re.sub(r"<[^>]+>", "", текст)
    return текст.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--min-sec", type=int, default=40)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Записать разбор в базу.")
    parser.add_argument("--redo", action="store_true", help="Переразобрать уже разобранные.")
    parser.add_argument("--uid", default="", help="Только эти звонки, через запятую.")
    parser.add_argument("--fresh", action="store_true",
                        help="Распознавать самим, не беря готовое из CRM.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    if not settings.yandex_stt_configured:
        print("нет ключа Яндекса")
        return 1

    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        min_interval_sec=settings.synergy_min_interval_sec, retries=settings.synergy_retries)
    conn = connect(settings.db_path)
    init_schema(conn)
    rows = conn.execute("""
        SELECT k.uid, k.started_at, k.client_phone, k.duration_sec, l.name AS line,
               (SELECT verdict FROM ad_calls a WHERE a.call_uid = k.uid) AS verdict,
               (SELECT transcript FROM ad_calls a WHERE a.call_uid = k.uid) AS было
        FROM calls k
        JOIN lines l ON l.phone10 = substr(replace(replace(replace(
             k.diversion,'+',''),' ',''),'-',''), -10)
        WHERE l.in_scope = 1 AND (l.is_general = 1 OR l.kind = 'рекламная')
          AND k.direction = 'in'
          AND k.local_date BETWEEN ? AND ? AND k.duration_sec >= ?
        ORDER BY k.started_at
    """, (args.since, args.until, args.min_sec)).fetchall()

    если_эти = {x.strip() for x in args.uid.split(",") if x.strip()}
    if если_эти:
        rows = [r for r in rows if r["uid"] in если_эти]
    записи = Path(settings.records_dir)
    дела = []
    # Запись нужна только чтобы распознать самим. Если расшифровка уже лежит
    # в CRM — её туда положил боевой сервер, — разбирать можно и без звука.
    # 01.10.2026 из сорока звонков у двадцати пяти не оказалось ссылки на
    # запись в нашей базе, а текст в CRM был у всех: требование записи
    # отрезало их от разметки на пустом месте.
    нет_записи = 0
    for r in rows:
        путь = записи / f"{r['uid']}.mp3"
        есть_звук = путь.exists()
        if not есть_звук and args.fresh:
            нет_записи += 1
            continue
        if r["было"] and not args.redo:
            continue
        дела.append((r, путь if есть_звук else None))
    if args.limit:
        дела = дела[: args.limit]

    без_звука = sum(1 for _, п in дела if п is None)
    print(f"звонков с общих номеров за период: {len(rows)}, к разбору {len(дела)}"
          + (f" (из них без записи, текст из CRM: {без_звука})" if без_звука else "")
          + (f", пропущено без записи: {нет_записи}" if нет_записи else ""))
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
        текст = "" if args.fresh else из_crm(client, r["uid"])
        откуда = "CRM"
        if not текст.strip() and путь is not None:
            откуда = "распознали сами"
            текст = yandex.transcribe_dialog(str(путь), api_key=settings.yandex_api_key,
                                             folder=settings.yandex_folder)
        elif not текст.strip():
            logger.warning("  текста в CRM нет и записи нет — пропускаем")
        else:
            logger.info("  расшифровка взята из CRM, %s символов", len(текст))
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
        save_ad_call(conn, call_uid=r["uid"], line=r["line"], engine=откуда,
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
