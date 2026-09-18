#!/usr/bin/env python
"""Померить просев входящих на проверочном наборе.

    sudo -u claude .venv/bin/python scripts/score_screening.py
    ... --tag "после правки про роли"   подписать прогон
    ... --limit 20                       взять только часть набора
    ... --errors                         показать каждую ошибку с цитатой

Набор — `checkset/inbound-screening.json`: разговоры с известным ответом,
размеченные по полным расшифровкам. Просев видит только начало разговора,
и это правильно: меряем ровно то, что работает в бою.

Распознавание берётся готовое (`screens.head_text`), заново звук не считается —
поэтому прогон стоит только запросов к модели и занимает пару минут. Значит
правку запроса можно проверить сразу, а не «на глаз по десятку звонков».

Каждый прогон дописывается в `data/screening_runs.jsonl`: видно, какая правка
что дала. Последний прогон показывает дашборд на странице «качество».
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.inbound import CALLBACK_MIN_TRIES, callback_to_our_search  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402

logger = logging.getLogger("score")

ROOT = Path(__file__).resolve().parents[1]
CHECKSET = ROOT / "checkset" / "inbound-screening.json"

# У аккаунта 30 000 токенов в минуту на gpt-4o, а один разговор — около 2 700.
# Значит больше десяти запросов в минуту не пройдёт: первый же замер потерял
# 43 звонка из 84 на 429-х. Держим интервал между запросами и повторяем отказ,
# иначе цифра качества сама зависит от того, повезло ли с лимитом.
MIN_INTERVAL_SEC = 6.0
ATTEMPTS = 5

_pace_lock = threading.Lock()
_last_start = 0.0


def pace() -> None:
    """Не выпускать запросы чаще, чем раз в MIN_INTERVAL_SEC."""
    global _last_start
    with _pace_lock:
        wait = _last_start + MIN_INTERVAL_SEC - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_start = time.monotonic()


def run_one(item: dict, head: str, active: str, settings, callbacks: dict[str, int]) -> dict:
    """Один разговор: что ответил просев и совпало ли с известным ответом.

    Порядок тот же, что в работе: сперва дешёвые проверки по истории звонков,
    и только потом модель. Иначе замер показывал бы качество не того, что
    действительно происходит с входящими.
    """
    tries = callbacks.get(item["uid"], 0)
    if tries >= CALLBACK_MIN_TRIES:
        return {**item, "predicted": "no_request", "asked_by": "", "equipment": "",
                "confidence": 0, "quote": f"перезвон после {tries} наших недозвонов",
                "rule": "перезвон на наш поиск"}

    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        pace()
        try:
            verdict = analyzer.screen_call(
                head, active or "", api_key=settings.openai_api_key,
                model=settings.analysis_model, own_company=settings.own_company,
            )
            break
        except Exception as exc:  # noqa: BLE001 — прогон не должен падать из-за одного звонка
            last = exc
            # Лимит токенов в минуту — ждём и пробуем снова: это не ошибка
            # разбора, а очередь. Разброс, чтобы потоки не пошли разом.
            time.sleep(MIN_INTERVAL_SEC * (attempt + 1) + random.uniform(0, 2))
    else:
        logger.warning("%s: %s: %s", item["uid"], type(last).__name__, last)
        return {**item, "predicted": None, "error": f"{type(last).__name__}: {last}"}
    got = bool(verdict["is_request"] and not verdict["about_existing"])
    return {
        **item,
        "predicted": "request" if got else "no_request",
        "asked_by": verdict.get("asked_by", ""),
        "equipment": verdict.get("equipment", ""),
        "confidence": verdict.get("confidence", 0),
        "quote": (verdict.get("quote") or "")[:160],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Точность просева на проверочном наборе.")
    ap.add_argument("--tag", default="", help="подпись прогона: что меняли")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=2, help="сколько запросов разом")
    ap.add_argument("--errors", action="store_true", help="показать каждую ошибку")
    ap.add_argument("--model", default="", help="проверить другую модель, не трогая настройки")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.openai_api_key:
        print("нет ключа OpenAI — мерить нечем", file=sys.stderr)
        return 1

    if args.model:
        # Модель подменяем только на время замера: так можно сравнить цену и
        # качество, ничего не переключая в работающем конвейере.
        settings = settings.model_copy(update={"analysis_model": args.model})

    doc = json.loads(CHECKSET.read_text(encoding="utf-8"))
    items = doc["items"][: args.limit] if args.limit else doc["items"]

    conn = connect(settings.db_path)
    heads = {
        r["call_uid"]: (r["head_text"], r["active_names"])
        for r in conn.execute(
            "SELECT s.call_uid, s.head_text, ic.active_names FROM screens s "
            "LEFT JOIN inbound_checks ic ON ic.call_uid = s.call_uid"
        )
    }
    conn.close()

    # Расшифровки в `screens` разного возраста: у части звонков стороны не
    # разделены — их считали до починки разделения дорожек. Мерить по ним
    # значит мерить вчерашнее распознавание. Поэтому если есть свежий пересчёт
    # (refresh_checkset_heads.py), берём его.
    fresh_path = Path(settings.db_path).parent / "checkset_heads.json"
    fresh = json.loads(fresh_path.read_text(encoding="utf-8")) if fresh_path.exists() else {}
    for uid, text in fresh.items():
        if text:
            heads[uid] = (text, heads.get(uid, ("", ""))[1])
    if fresh:
        print(f"свежих расшифровок начала: {len(fresh)}")

    # Недозвоны считаем заранее и один раз: в потоках лишнее соединение с базой.
    callbacks: dict[str, int] = {}
    conn = connect(settings.db_path)
    for item in items:
        row = conn.execute("SELECT client_phone, started_at FROM calls WHERE uid = ?",
                           (item["uid"],)).fetchone()
        if row:
            callbacks[item["uid"]] = callback_to_our_search(
                conn, row["client_phone"], row["started_at"])
    conn.close()

    todo = [i for i in items if heads.get(i["uid"], (None, None))[0]]
    missing = len(items) - len(todo)
    print(f"набор: {len(items)} разговоров, считаю {len(todo)}"
          + (f" (без распознанного начала пропущено {missing})" if missing else ""))

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(
            lambda i: run_one(i, heads[i["uid"]][0], heads[i["uid"]][1] or "", settings, callbacks),
            todo))

    ok = [r for r in results if r.get("predicted")]
    tp = [r for r in ok if r["label"] == "request" and r["predicted"] == "request"]
    fp = [r for r in ok if r["label"] == "no_request" and r["predicted"] == "request"]
    fn = [r for r in ok if r["label"] == "request" and r["predicted"] == "no_request"]
    tn = [r for r in ok if r["label"] == "no_request" and r["predicted"] == "no_request"]
    failed = [r for r in results if not r.get("predicted")]

    precision = len(tp) / (len(tp) + len(fp)) * 100 if (tp or fp) else 0.0
    recall = len(tp) / (len(tp) + len(fn)) * 100 if (tp or fn) else 0.0

    print()
    print(f"  точность  {precision:5.1f}%   из {len(tp) + len(fp)} заведённых заявок верны {len(tp)}")
    print(f"  полнота   {recall:5.1f}%   из {len(tp) + len(fn)} настоящих запросов поймано {len(tp)}")
    print(f"  верных отказов {len(tn)}, сбоев {len(failed)}")

    by_rule = sum(1 for r in ok if r.get("rule"))
    if by_rule:
        print(f"  из них отсеяно по истории звонков, без модели: {by_rule}")

    if fp:
        by_kind: dict[str, int] = {}
        for r in fp:
            by_kind[r["kind"] or "прочее"] = by_kind.get(r["kind"] or "прочее", 0) + 1
        print("\n  лишние заявки по видам:")
        for kind, n in sorted(by_kind.items(), key=lambda x: -x[1]):
            print(f"    {kind:20} {n}")
    if fn:
        print(f"\n  пропущенные запросы: {', '.join(r['uid'] for r in fn)}")

    if args.errors:
        for title, rows in (("ЛИШНИЕ", fp), ("ПРОПУЩЕННЫЕ", fn)):
            for r in rows:
                print(f"\n  {title} {r['uid']} [{r['kind'] or '—'}] {r['note']}")
                print(f"    просев: {r['equipment']!r} asked_by={r['asked_by']} "
                      f"уверенность={r['confidence']}")
                if r["quote"]:
                    print(f"    цитата: {r['quote']}")

    prompt_hash = hashlib.sha256(analyzer.SCREEN_PROMPT.encode("utf-8")).hexdigest()[:12]
    run = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tag": args.tag, "model": settings.analysis_model, "prompt": prompt_hash,
        "n": len(ok), "precision": round(precision, 1), "recall": round(recall, 1),
        "tp": len(tp), "fp": len(fp), "fn": len(fn), "tn": len(tn), "failed": len(failed),
        "fp_uids": [r["uid"] for r in fp], "fn_uids": [r["uid"] for r in fn],
    }
    out = Path(settings.db_path).parent / "screening_runs.jsonl"
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(run, ensure_ascii=False) + "\n")

    # Подробности последнего прогона: по ним дашборд рисует страницу качества,
    # и по ним же видно каждую ошибку, не гоняя набор заново — прогон занимает
    # восемь минут, потому что упирается в лимит токенов в минуту.
    details = {**run, "items": sorted(
        results, key=lambda r: (r.get("label") == r.get("predicted"), r["uid"]))}
    (Path(settings.db_path).parent / "screening_last.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nзапрос {prompt_hash}, прогон записан в {out.name}"
          + (f" под подписью «{args.tag}»" if args.tag else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
