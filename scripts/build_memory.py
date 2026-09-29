#!/usr/bin/env python
"""Собрать базу знаний в облаке Яндекса — «выделенную память» разбора.

Модель ничего не помнит между звонками: каждый разговор она видит первый раз.
То, что выглядит как обучение, — это знания, которые мы кладём ей в руки.
Раньше они ехали в каждый запрос текстом; теперь лежат в поисковом индексе
внутри Яндекса, и модель берёт оттуда только нужный кусок.

    ./scripts/build_memory.py            показать, что будет собрано
    ./scripts/build_memory.py --apply    загрузить и собрать индекс
    ./scripts/build_memory.py --drop     снести старое (файлы и индексы)

**Источник правды — код, а не облако.** Всё, что здесь загружается,
собирается из `app/knowledge.py`, `app/call_context.py` и наших справочников.
Править надо их, а потом пересобрать память; править в консоли Яндекса
бессмысленно — следующий прогон затрёт.

**Даты и конкретные примеры в текст не кладём.** 29.09.2026 проверили чужой
ассистент в этом же каталоге: внутри его инструкции лежал пример с датой
`2026-08-13`, и модель вписала эту дату в разбор сентябрьского разговора,
где никакой даты не звучало. Пример, который модель принимает за факт, —
это не иллюстрация, а ловушка.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import call_context, knowledge  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect  # noqa: E402

logger = logging.getLogger("build_memory")

ФАЙЛЫ = "https://rest-assistant.api.cloud.yandex.net/files/v1/files"
ИНДЕКС = "https://rest-assistant.api.cloud.yandex.net/assistants/v1/searchIndex"
ОПЕРАЦИИ = "https://operation.api.cloud.yandex.net/operations"
МЕТКА = {"проект": "techno-resurs", "назначение": "разбор-звонков"}


def учебник() -> str:
    части = ["# Специфика аренды спецтехники", "", knowledge.DOMAIN, "",
             "# Обязательные вопросы по видам техники", ""]
    for вид, вопросы in sorted(knowledge.REQUIRED_QUESTIONS.items()):
        части.append(f"## {вид}")
        части += [f"- {в}" for в in вопросы]
        части.append("")
    допродажа = getattr(knowledge, "UPSELL", None) or {}
    if допродажа:
        части += ["# Что предложить в дополнение", ""]
        for вид, что in sorted(допродажа.items()):
            части.append(f"- **{вид}**: {', '.join(что) if isinstance(что, (list, tuple)) else что}")
    return "\n".join(части)


def правила() -> str:
    части = ["# Поводы звонка и что из них следует", "",
             "Повод считается из метаданных звонка до всякого разбора: направление,",
             "линия, тип собеседника, был ли наш звонок ему раньше. Это факт, а не догадка.",
             ""]
    for ключ, текст in call_context.SCENARIOS.items():
        части.append(f"## {ключ}")
        части.append(текст.replace("{менеджер}", "менеджер")
                     .replace("{собеседник}", "собеседник")
                     .replace("{заявок}", "N").replace("{линия}", "название линии"))
        части.append("")
    части += ["# Роли дорожек записи", "",
              "Левая дорожка (operator) — наш менеджер, правая (client) — собеседник,",
              "в обе стороны. Измерено по звуку на ста записях: кто заговорил первым,",
              "тот и снял трубку. В одном случае из пяти начало записи перебито, поэтому",
              "содержание разговора остаётся главнее этого правила."]
    return "\n".join(части)


def справочники(conn) -> str:
    типы = [r[0] for r in conn.execute(
        "SELECT DISTINCT type_name FROM transport_cards WHERE type_name <> '' ORDER BY type_name")]
    линии = [(r["name"], r["calls_in"]) for r in conn.execute(
        "SELECT name, calls_in FROM lines WHERE kind = 'рекламная' ORDER BY calls_in DESC")]
    части = ["# Типы техники, которыми компания работает", "",
             "Названия — как они заведены в CRM. Разбор должен выбирать из этого списка,",
             "а не придумывать свои формулировки.", ""]
    части += [f"- {т}" for т in типы]
    части += ["", "# Рекламные линии", "",
              "Звонок на такую линию — обращение по объявлению, то есть почти всегда",
              "новый запрос на технику. Исключение — спам и ошиблись номером.", ""]
    части += [f"- {имя}" for имя, _ in линии]
    return "\n".join(части)


def что_в_crm() -> str:
    return "\n".join([
        "# Что разбор записывает в CRM", "",
        "- **Транскрипт звонка** — разговор целиком, с разделением на менеджера и клиента",
        "- **Выжимка** — что просил клиент и что с этим делать, несколько строк",
        "- **Рекомендации РОПу** — отдельным текстом",
        "- **Рекомендации менеджеру** — отдельным текстом",
        "- **Оценка звонка** — цифрой",
        "- **Тип техники** и **адрес объекта** — только если прозвучали явно", "",
        "# Чего делать нельзя", "",
        "- Додумывать то, чего в разговоре не было. Не прозвучало — оставить пустым.",
        "- Писать в «упущено» то, что на самом деле заполнено в карточке.",
        "- Приводить цитату, которой нет в расшифровке дословно.",
        "- Брать даты и числа из примеров: в разборе допустимы только те, что звучали.",
    ])


def залить(headers: dict, folder: str, имя: str, текст: str) -> str:
    тело = {"folderId": folder, "name": имя, "mimeType": "text/markdown",
            "content": base64.b64encode(текст.encode("utf-8")).decode(),
            "labels": МЕТКА}
    r = httpx.post(ФАЙЛЫ, headers=headers, json=тело, timeout=120)
    r.raise_for_status()
    return r.json()["id"]


def снести(headers: dict, folder: str) -> None:
    for адрес, ключ in ((ИНДЕКС, "indices"), (ФАЙЛЫ, "files")):
        данные = httpx.get(адрес, headers=headers, params={"folderId": folder}, timeout=60).json()
        for x in данные.get(ключ) or []:
            if (x.get("labels") or {}).get("проект") != МЕТКА["проект"]:
                continue
            httpx.delete(f"{адрес}/{x['id']}", headers=headers, timeout=60)
            print(f"  удалено: {x.get('name') or x['id']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Загрузить в облако.")
    parser.add_argument("--drop", action="store_true", help="Снести наши файлы и индексы.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    if not settings.yandex_stt_configured:
        print("нет ключа Яндекса")
        return 1
    headers = {"Authorization": f"Api-Key {settings.yandex_api_key}"}
    folder = settings.yandex_folder

    conn = connect(settings.db_path)
    куски = {
        "учебник-спецтехника.md": учебник(),
        "правила-поводов-звонка.md": правила(),
        "справочники-техника-и-линии.md": справочники(conn),
        "что-писать-в-crm.md": что_в_crm(),
    }
    conn.close()

    if args.drop:
        снести(headers, folder)
        return 0

    print("что пойдёт в память:")
    for имя, текст in куски.items():
        print(f"  {имя:<34} {len(текст):>6} символов, {len(текст.splitlines()):>4} строк")
    if not args.apply:
        print("\nничего не загружено. Для загрузки: --apply")
        return 0

    снести(headers, folder)
    ids = []
    for имя, текст in куски.items():
        fid = залить(headers, folder, имя, текст)
        ids.append(fid)
        print(f"  загружено: {имя} → {fid}")

    r = httpx.post(ИНДЕКС, headers=headers, json={
        "folderId": folder, "name": "Память разбора звонков", "fileIds": ids,
        "labels": МЕТКА,
        # Гибридный поиск: по словам и по смыслу сразу. Наши тексты полны
        # терминов, которые надо находить дословно («вылет стрелы», «пухто»),
        # и вопросов, которые звучат в разговоре своими словами.
        "hybridSearchIndex": {},
    }, timeout=180)
    if r.status_code != 200:
        print(f"индекс не создан: {r.status_code} {r.text[:300]}")
        return 1
    операция = r.json()["id"]
    for _ in range(120):
        time.sleep(3)
        st = httpx.get(f"{ОПЕРАЦИИ}/{операция}", headers=headers, timeout=60).json()
        if st.get("error"):
            print(f"индекс не собрался: {str(st['error'])[:300]}")
            return 1
        if st.get("done"):
            иид = (st.get("response") or {}).get("id") or операция
            print(f"\nиндекс собран: {иид}")
            return 0
    print("индекс собирается дольше шести минут — проверьте в консоли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
