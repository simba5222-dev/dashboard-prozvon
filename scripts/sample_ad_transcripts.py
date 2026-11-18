"""Выборочная проверка качества распознавания потока 1 после перехода на
GPU-сервер «офис» (22.10.2026) — решение владельца проверять вручную, не
автоматическим сравнением с Yandex.

Берёт несколько случайных заявок потока 1 за вчера (у которых есть наша
«Выжимка», custom-30609 -- значит разбор дошёл до конца) и шлёт текст
расшифровки (custom-30599) в канал сторожа для ручного прочтения -- странно
звучащие фразы, перепутанные термины техники и т.п. будут видны сразу.

    sudo -u claude .venv/bin/python scripts/sample_ad_transcripts.py
    ... --n 8               сколько заявок показать (по умолчанию 5)
    ... --date 2026-10-22   за конкретный день, не за вчера
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.collector import SynergyClient  # noqa: E402
from app.config import get_settings  # noqa: E402

SYNERGY_ORDER_URL = "https://app.synergycrm.ru/orders/"


def send_telegram(token: str, chat_id: int, text: str) -> None:
    r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat_id, "text": text}, timeout=15)
    r.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, по умолчанию вчера")
    args = ap.parse_args()

    settings = get_settings()
    day = args.date or (date.today() - timedelta(days=1)).isoformat()

    client = SynergyClient(
        base_url=settings.synergy_url, token=settings.synergy_api_token,
        timeout_sec=30.0, min_interval_sec=settings.synergy_min_interval_sec,
        retries=settings.synergy_retries,
    )

    candidates = []
    page = 1
    while page <= 5:  # заявок в день немного, дальше первых страниц за вчера не бывает
        payload = client.get("orders", sort="-created-at", **{"page[number]": page})
        rows = payload.get("data") or []
        if not rows:
            break
        stop = False
        for row in rows:
            a = row["attributes"]
            created = str(a.get("created-at") or "")[:10]
            if created < day:
                stop = True
                break
            if created != day:
                continue
            cu = a.get("customs") or {}
            text = str(cu.get("custom-30599") or "").strip()
            if text:
                candidates.append({
                    "id": row["id"], "number": a.get("number"),
                    "name": a.get("name") or "", "text": text,
                })
        if stop:
            break
        page += 1

    print(f"заявок потока 1 за {day} с расшифровкой: {len(candidates)}")
    if not candidates:
        print("показывать нечего")
        return 0

    sample = random.sample(candidates, min(args.n, len(candidates)))

    lines = [f"🎙 Выборка потока 1 за {day} ({len(sample)} из {len(candidates)}) — проверка после перехода на GPU:\n"]
    for c in sample:
        text = c["text"][:350] + ("…" if len(c["text"]) > 350 else "")
        lines.append(
            f"№{c['number'] or c['id']} — {c['name']}\n"
            f"{SYNERGY_ORDER_URL}{c['id']}\n"
            f"{text}\n"
        )
    message = "\n".join(lines)

    if settings.watchdog_bot_token and settings.watchdog_chat_id:
        # Telegram режет сообщение на ~4096 символах -- шлём частями, если не влезло
        chunk = ""
        for part in message.split("\n\n"):
            if len(chunk) + len(part) > 3500:
                send_telegram(settings.watchdog_bot_token, settings.watchdog_chat_id, chunk)
                chunk = ""
            chunk += part + "\n\n"
        if chunk.strip():
            send_telegram(settings.watchdog_bot_token, settings.watchdog_chat_id, chunk)
        print("отправлено в канал сторожа")
    else:
        print("нет токена/chat_id сторожа — вывожу в консоль:\n")
        print(message)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
