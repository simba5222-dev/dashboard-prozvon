#!/usr/bin/env python
"""Три головы на одних расшифровках: YandexGPT Pro, Lite и gpt-4o.

Вопрос «какая модель лучше» без замера — спор о вкусах, а после 29.09.2026 к
нему добавился второй вопрос, денежный: Pro стоит 1,20 ₽ за тысячу токенов,
Lite — 0,20, gpt-4o — примерно 0,14 в пересчёте. Разница девятикратная, и
платить за неё имеет смысл, только если она что-то даёт.

    ./scripts/compare_heads.py --limit 12
    ./scripts/compare_heads.py --limit 12 --apply --out /var/www/karta/heads.html

**Расшифровки берутся готовые.** Они уже лежат в `ad_calls`, распознавание
заново не гоняем: сравниваем головы, а не уши, и деньги тратим только на
генерацию.

**Чек-лист вопросов всем трём даётся одинаковый и из нашего кода.** Так
условия честные, и заодно видно то, что стоило увидеть раньше: спрашивать
чек-лист у облачной памяти на каждом звонке — значит платить за то, что
лежит в репозитории даром. Память нужна как склад и для вопросов вроде «что
спросить про ямобур», а не как справочник внутри конвейера.

**Чего этот замер не делает.** Он не говорит, какая модель права: эталона у
нас по-прежнему нет. Он показывает, насколько они расходятся между собой и
кто чаще пишет то, чего в разговоре не было.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import knowledge, yandex  # noqa: E402
from app.config import Settings  # noqa: E402

logger = logging.getLogger("heads")

ГОЛОВЫ = {
    "Pro": {"вид": "яндекс", "модель": "yandexgpt", "цена": 1.20 / 1000},
    "Lite": {"вид": "яндекс", "модель": "yandexgpt-lite", "цена": 0.20 / 1000},
    "gpt-4o": {"вид": "openai", "модель": "gpt-4o", "цена": None},
}
# gpt-4o: $2,5 за миллион входящих и $10 за миллион исходящих, курс 95 ₽.
ЦЕНА_4O_ВХОД = 2.5 / 1_000_000 * 95
ЦЕНА_4O_ВЫХОД = 10.0 / 1_000_000 * 95

ЗАДАНИЕ = """Разбери телефонный разговор. Звонок входящий, на рекламную линию «{линия}» —
человек позвонил по объявлению, значит почти наверняка ему нужна техника.

В расшифровке `operator` — наш менеджер, `client` — позвонивший.

РАСШИФРОВКА:
{текст}

ЧТО НАДО БЫЛО СПРОСИТЬ ПО ЭТОЙ ТЕХНИКЕ:
{чеклист}

Верни только JSON, без пояснений вокруг:
{{
  "is_request": true/false — просил ли звонивший технику,
  "equipment": "какая техника нужна, словами клиента; пусто, если речи не было",
  "object": "адрес или объект — ТОЛЬКО если он дословно прозвучал в расшифровке",
  "when": "сроки — только если прозвучали",
  "summary": "1-2 предложения: о чём говорили и чем кончилось",
  "asked": ["вопросы из списка выше, которые менеджер ЗАДАЛ"],
  "missed": ["вопросы из списка выше, которые менеджер НЕ задал"],
  "next_step": "о чём договорились",
  "quality": 1-5 — насколько разговор доведён до результата,
  "quality_note": "коротко, за что такая оценка"
}}

Ничего не придумывай. Дат, чисел и адресов, которых нет в расшифровке, быть
не должно. Не прозвучало — пустая строка."""


def чеклист_для(текст: str) -> str:
    вопросы = knowledge.questions_for(текст)
    return "\n".join(f"- {в}" for в in вопросы) if вопросы else "(по этой технике списка нет)"


def слова(текст: str) -> set[str]:
    return {с for с in re.findall(r"[а-яёa-z0-9]+", (текст or "").lower().replace("ё", "е"))
            if len(с) > 3}


def выдумано(значение: str, расшифровка: str) -> bool:
    """Есть ли в ответе слова, которых нет в расшифровке.

    Грубая проверка, и она такой и задумана: адрес объекта модель обязана
    брать из разговора. Если больше половины значимых слов в нём не звучали —
    это сочинение, а не извлечение.
    """
    свои = слова(значение)
    if not свои:
        return False
    чужие = свои - слова(расшифровка)
    return len(чужие) > len(свои) / 2


def спросить(голова: str, задание: str, settings: Settings) -> tuple[str, float]:
    опис = ГОЛОВЫ[голова]
    if опис["вид"] == "яндекс":
        ответ = yandex.complete(задание, api_key=settings.yandex_api_key,
                                folder=settings.yandex_folder, model=опис["модель"],
                                max_tokens=1500)
        # Токены считает сам `yandex.complete` и пишет в учёт; здесь считаем
        # по длине — для сравнения голов этого хватает.
        токенов = (len(задание) + len(ответ)) / 3.5
        return ответ, токенов * опис["цена"]
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key, timeout=120.0)
    r = client.chat.completions.create(
        model=опис["модель"], temperature=0.0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": задание}])
    u = r.usage
    цена = (u.prompt_tokens * ЦЕНА_4O_ВХОД + u.completion_tokens * ЦЕНА_4O_ВЫХОД)
    return (r.choices[0].message.content or ""), цена


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--heads", default="Pro,Lite,gpt-4o")
    parser.add_argument("--json", default="data/heads.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    головы = [h.strip() for h in args.heads.split(",") if h.strip() in ГОЛОВЫ]
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT a.call_uid, a.line, a.transcript, k.duration_sec, k.started_at
        FROM ad_calls a JOIN calls k ON k.uid = a.call_uid
        WHERE a.transcript IS NOT NULL AND a.transcript <> ''
        ORDER BY k.duration_sec DESC LIMIT ?""", (args.limit,)).fetchall()
    conn.close()

    print(f"разговоров в замере: {len(rows)}, голов: {', '.join(головы)}")
    итог: dict[str, dict] = {h: {"цена": 0.0, "битых": 0, "выдумал": 0, "оценки": [],
                                 "запрос": 0, "пропущено": 0, "ответы": {}} for h in головы}
    for индекс, r in enumerate(rows, 1):
        чек = чеклист_для(r["transcript"])
        задание = ЗАДАНИЕ.format(линия=r["line"], текст=r["transcript"][:12000], чеклист=чек)
        logger.info("[%s/%s] %s · %s с", индекс, len(rows), r["call_uid"], r["duration_sec"])
        for h in головы:
            ответ, цена = спросить(h, задание, settings)
            разбор = yandex.parse_json(ответ)
            итог[h]["цена"] += цена
            if разбор is None:
                итог[h]["битых"] += 1
                логика = "не разобрался"
            else:
                итог[h]["запрос"] += int(bool(разбор.get("is_request")))
                if разбор.get("quality"):
                    try:
                        итог[h]["оценки"].append(float(разбор["quality"]))
                    except (TypeError, ValueError):
                        pass
                итог[h]["пропущено"] += len(разбор.get("missed") or [])
                if выдумано(str(разбор.get("object") or ""), r["transcript"]):
                    итог[h]["выдумал"] += 1
                логика = (разбор.get("equipment") or "—")[:34]
            итог[h]["ответы"][r["call_uid"]] = разбор
            logger.info("    %-8s %-36s %.2f ₽", h, логика, цена)
            time.sleep(0.5)

    print(f"\n{'голова':<10}{'₽ за замер':>12}{'₽ за звонок':>13}{'битых':>7}"
          f"{'выдумал адрес':>15}{'ср. оценка':>12}{'нашёл запрос':>14}")
    for h in головы:
        v = итог[h]
        ср = sum(v["оценки"]) / len(v["оценки"]) if v["оценки"] else 0
        print(f"{h:<10}{v['цена']:>12.2f}{v['цена']/len(rows):>13.2f}{v['битых']:>7}"
              f"{v['выдумал']:>15}{ср:>12.1f}{v['запрос']:>10}/{len(rows)}")

    Path(args.json).write_text(json.dumps(
        {"разговоров": len(rows), "итог": {h: {k: v for k, v in итог[h].items() if k != "ответы"}
                                           for h in головы},
         "ответы": {h: итог[h]["ответы"] for h in головы},
         "звонки": [dict(r) for r in rows]}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nподробности: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
