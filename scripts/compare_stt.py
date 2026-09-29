#!/usr/bin/env python
"""Три распознавателя на одних и тех же записях ВАТС — рядом, для сравнения.

Владельца беспокоит, что записи разговоров уезжают в OpenAI. Прежде чем
переезжать на российский сервис, надо увидеть, чем платим за переезд.
Скрипт берёт настоящие записи прозвона, прогоняет каждую через три пути и
складывает результат в одну страницу:

    Яндекс SpeechKit   — российский, записи не покидают страну
    whisper-1 (OpenAI) — то, чем расшифровки сделаны сейчас
    местная small      — faster-whisper на нашем же сервере, бесплатно

    ./scripts/compare_stt.py --calls 10 --out /var/www/karta/stt-compare.html

**Яндекс идёт третьим поколением API, целой дорожкой.** 29.09.2026
выяснилось, что `recognizeFileAsync` принимает звук **прямо в теле запроса** —
бакет в Object Storage не нужен. Это сняло ограничение в 30 секунд, из-за
которого первая версия этого скрипта резала дорожку на куски по тишине.

**И знаки препинания у Яндекса есть.** Они приходят отдельным событием
`finalRefinement.normalizedText`, которое первая версия скрипта не читала, —
отсюда взялся ложный вывод, будто Яндекс отдаёт сплошной поток слов. На деле
там заглавные буквы, запятые, числа цифрами и «НДС» капсом.

**Что здесь не измеряется.** Эталона, что именно было сказано, нет, поэтому
скрипт не считает проценты ошибок. Он кладёт три текста рядом и подсвечивает
слова, в которых распознаватели разошлись, — судить человеку.
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import wave
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402

logger = logging.getLogger("compare_stt")

RATE = 16000
YANDEX_START = "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync"
YANDEX_RESULT = "https://stt.api.cloud.yandex.net/stt/v3/getRecognition"
YANDEX_OP = "https://operation.api.cloud.yandex.net/operations"
# Модель: только `general`. Проверено 29.09.2026 — `general:rc` на нашем же
# разговоре потерял «не» («у меня не стоит» стало «у меня стоит»), а
# `general:deprecated` и `deferred-general` дали побуквенно тот же текст.
YANDEX_MODEL = "general"
# Цена отложенного распознавания, ₽ за 15 секунд звука.
PRICE_PER_15S = 0.0381
ROLES = (("оператор", 0), ("клиент", 1))


# ── звук ──────────────────────────────────────────────────────────────────

def decode_track(path: Path, channel: int) -> np.ndarray:
    """Одна дорожка записи: 16 кГц, моно, int16.

    Стерео у ВАТС разложено по ролям: левый канал — оператор, правый —
    клиент. У моно-записей обе роли в одной дорожке, и разделить их нечем.
    """
    import av

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        stereo = stream.channels > 1
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        out = io.BytesIO()
        for frame in container.decode(audio=0):
            arr = frame.to_ndarray()
            mono = arr[channel: channel + 1] if stereo and arr.shape[0] > 1 else arr[:1]
            new = av.AudioFrame.from_ndarray(mono, format=frame.format.name, layout="mono")
            new.sample_rate = frame.sample_rate
            for piece in resampler.resample(new):
                out.write(bytes(piece.planes[0])[: piece.samples * 2])
    return np.frombuffer(out.getvalue(), dtype=np.int16)


def to_wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())
    return buf.getvalue()


# ── распознаватели ────────────────────────────────────────────────────────

def yandex_track(samples: np.ndarray, key: str, folder: str) -> tuple[str, float]:
    """Дорожка целиком через отложенное распознавание Яндекса.

    Порядок важен: сначала ждём, пока операция отметится завершённой в
    Operations API, и только потом просим текст. Спросить раньше — получить
    404: операции ещё нет в хранилище результатов.

    Берём **нормализованный** текст из `finalRefinement`, а не сырой из
    `final`: в нормализованном есть знаки препинания, заглавные буквы и числа
    цифрами. Сырой оставлен без внимания намеренно — читать его человеку хуже,
    а модели он ничего не добавляет.
    """
    headers = {"Authorization": f"Api-Key {key}", "x-folder-id": folder}
    body = {
        "content": base64.b64encode(samples.tobytes()).decode(),
        "recognitionModel": {
            "model": YANDEX_MODEL,
            "audioFormat": {"rawAudio": {"audioEncoding": "LINEAR16_PCM",
                                         "sampleRateHertz": RATE, "audioChannelCount": 1}},
            "textNormalization": {"textNormalization": "TEXT_NORMALIZATION_ENABLED",
                                  "literatureText": True, "profanityFilter": False},
            "audioProcessingType": "FULL_DATA",
        },
    }
    seconds = len(samples) / RATE
    started = time.time()
    try:
        response = httpx.post(YANDEX_START, headers=headers, json=body, timeout=300)
        response.raise_for_status()
        operation = response.json()["id"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("Яндекс не принял дорожку: %s", exc)
        return "", seconds

    while time.time() - started < 900:
        time.sleep(3)
        try:
            state = httpx.get(f"{YANDEX_OP}/{operation}", headers=headers, timeout=60).json()
        except (httpx.HTTPError, ValueError):
            continue
        if state.get("error"):
            logger.warning("Яндекс вернул ошибку: %s", str(state["error"])[:200])
            return "", seconds
        if state.get("done"):
            break
    else:
        logger.warning("Яндекс не ответил за 15 минут")
        return "", seconds

    try:
        page = httpx.get(YANDEX_RESULT, headers=headers,
                         params={"operationId": operation}, timeout=180)
    except httpx.HTTPError as exc:
        logger.warning("результат не забран: %s", exc)
        return "", seconds

    said: list[str] = []
    for line in page.text.splitlines():
        if not line.strip():
            continue
        try:
            result = (json.loads(line).get("result") or {})
        except ValueError:
            continue
        refined = (result.get("finalRefinement") or {}).get("normalizedText") or {}
        for alternative in refined.get("alternatives") or []:
            if alternative.get("text"):
                said.append(alternative["text"])
    return " ".join(said).strip(), seconds


def local_track(samples: np.ndarray, url: str, role: str) -> str:
    """Местная faster-whisper: тот же сервис, что стоит на боевом сервере."""
    response = httpx.post(
        f"{url}/transcribe",
        files={"file": (f"{role}.wav", to_wav(samples), "audio/wav")},
        data={"mode": "mono"}, timeout=600.0,
    )
    response.raise_for_status()
    data = response.json()
    # Отдаём дорожку уже разделённой, поэтому режим `mono`: в ответе одна
    # запись в `channels`, её готовый текст нам и нужен.
    parts = [str(ch.get("text") or "").strip() for ch in (data.get("channels") or [])]
    return " ".join(p for p in parts if p)


def whisper_from_db(text: str, role: str) -> str:
    """Готовая расшифровка whisper-1 из базы — её реплики помечены ролью."""
    want = "operator" if role == "оператор" else "client"
    said = []
    for line in (text or "").splitlines():
        head, _, body = line.partition(":")
        if head.strip() == want:
            said.append(body.strip())
    return " ".join(said)


# ── разбор расхождений ────────────────────────────────────────────────────

WORD = re.compile(r"[а-яёa-z0-9]+")


def words(text: str) -> list[str]:
    return WORD.findall((text or "").lower().replace("ё", "е"))


def disagreements(texts: dict[str, str]) -> list[tuple[str, dict[str, int]]]:
    """Слова, которые один распознаватель слышит, а другой нет.

    Грубая мера, и она такой и задумана: нам нужно не число, а зацепки —
    куда смотреть глазами. Служебные слова выкидываем, они шумят.
    """
    stop = {"и", "в", "на", "да", "не", "что", "а", "у", "с", "это", "по", "то",
            "вот", "ну", "как", "я", "мы", "вы", "он", "она", "там", "же", "бы",
            "за", "от", "до", "из", "о", "но", "ли", "так", "вас", "нас", "меня",
            "вам", "нам", "мне", "его", "их", "есть", "был", "была", "было"}
    counts = {name: Counter(w for w in words(text) if w not in stop and len(w) > 3)
              for name, text in texts.items()}
    everything = set().union(*counts.values()) if counts else set()
    out = []
    for word in everything:
        seen = {name: c[word] for name, c in counts.items()}
        if 0 in seen.values() and sum(seen.values()) > 0:
            out.append((word, seen))
    out.sort(key=lambda pair: -sum(pair[1].values()))
    return out[:40]


# ── страница ──────────────────────────────────────────────────────────────

PAGE_HEAD = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Три распознавателя на одних записях</title>
<style>
:root{--bg:#eaeeef;--surface:#fff;--surface-2:#f4f7f8;--ink:#14202a;--ink-2:#485965;
 --ink-3:#7b8d99;--line:#ccd6d9;--accent:#0b5e6e;--ok:#3d6a41;--ok-soft:#e4efe3;
 --warn:#8f4d0e;--warn-soft:#faecd9;--stop:#8f2e2d;--stop-soft:#f8e3e1}
@media (prefers-color-scheme:dark){:root{--bg:#101820;--surface:#18232b;--surface-2:#141d24;
 --ink:#e6eef1;--ink-2:#a8b9c3;--ink-3:#788a95;--line:#293640;--accent:#5ab6c6;
 --ok:#8bc28e;--ok-soft:#1a2a1d;--warn:#dfa35b;--warn-soft:#31240f;--stop:#e78a80;--stop-soft:#361d1b}}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:1600px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:clamp(24px,5vw,34px);margin:0 0 4px;letter-spacing:-.02em}
.sub{color:var(--ink-3);font-size:13px;margin:0 0 22px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:3px;margin-bottom:18px}
.card-head{padding:13px 16px;background:var(--surface-2);border-bottom:1px solid var(--line);
 display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline}
.card-head h2{margin:0;font-size:18px;flex:1 1 220px}
.card-head .sub{margin:0;font-size:12.5px}
.body{padding:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line)}
.tile{background:var(--surface);padding:14px 16px}
.tile .n{font-size:25px;line-height:1.05;font-variant-numeric:tabular-nums}
.tile .l{font-size:12px;color:var(--ink-3);margin-top:3px}
table.cmp{width:100%;border-collapse:collapse;font-size:13.5px}
table.cmp th{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);
 font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--ink-3)}
table.cmp td{padding:11px 12px;vertical-align:top;border-bottom:1px solid var(--line);line-height:1.5}
table.cmp td.role{white-space:nowrap;color:var(--ink-3);font-size:12px;width:1%}
table.cmp col.ya{width:34%}table.cmp col.wh{width:33%}table.cmp col.lo{width:33%}
.ya{background:var(--ok-soft)}.wh{background:var(--warn-soft)}.lo{background:var(--surface-2)}
.miss{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px}
.miss b{font-weight:600;font-size:12px;padding:2px 7px;border-radius:2px;background:var(--stop-soft);
 color:var(--stop);font-family:ui-monospace,Menlo,monospace}
.note{font-size:13px;color:var(--ink-2);line-height:1.6}
/* Поправка к прошлой версии страницы должна быть заметна: её смысл в том,
   чтобы читавший вчера увидел, что вывод изменился. */
.body p + p.note{margin-top:14px;padding:12px 15px;border:1px solid var(--warn);
  background:var(--warn-soft);color:var(--warn);border-radius:3px}
.body p.note b{color:inherit}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12.5px;color:var(--ink-3);margin-bottom:18px}
.legend i{font-style:normal;padding:2px 8px;border-radius:2px}
audio{width:100%;max-width:420px;margin-top:6px}
</style></head><body><div class="wrap">
"""


def render(results: list[dict], totals: dict) -> str:
    out = [PAGE_HEAD]
    out.append('<h1>Три распознавателя на одних записях</h1>')
    out.append(f'<p class="sub">Записи разговоров ВАТС, {totals["calls"]} шт. · '
               f'собрано {html.escape(totals["made_at"])}</p>')
    out.append('<div class="legend">'
               '<i class="ya">Яндекс SpeechKit — Россия</i>'
               '<i class="wh">whisper-1 — OpenAI, чем пользуемся сейчас</i>'
               '<i class="lo">местная small — наш сервер, даром</i></div>')

    out.append('<div class="card"><div class="tiles">')
    for value, label in (
        (totals["calls"], "разговоров"),
        (f'{totals["minutes"]:.0f} мин', "звука через Яндекс"),
        (f'{totals["rub"]:.2f} ₽', "стоил этот прогон у Яндекса"),
        (f'{totals["rub_month"]:,.0f} ₽'.replace(",", " "), "если так весь поток, в месяц"),
        (f'{totals["usd_month"]:.0f} $', "столько же через whisper-1"),
    ):
        out.append(f'<div class="tile"><div class="n">{value}</div><div class="l">{label}</div></div>')
    out.append('</div></div>')

    out.append('<div class="card"><div class="card-head"><h2>Как это читать</h2></div>'
               '<div class="body"><p class="note">'
               'Три колонки — один и тот же разговор, услышанный тремя способами. '
               'Эталона, что было сказано на самом деле, нет, поэтому процентов ошибок здесь нет тоже: '
               'судить глазами. Смотрите на узнаваемое — название фирмы, названия техники, города, числа. '
               'Под каждым разговором — слова, которые один распознаватель услышал, а другой пропустил.'
               '</p><p class="note"><b>Поправка к первой версии этой страницы.</b> 28 сентября здесь '
               'стояло, что Яндекс отдаёт текст без знаков препинания и заглавных букв. Это было неверно: '
               'так ведёт себя только старое синхронное API, которым делался первый прогон. Знаки '
               'препинания приходят отдельным событием, которое скрипт тогда не читал. Страница '
               'пересобрана 29 сентября через третье поколение API — с нормализацией, целыми дорожками '
               'и моделью <code>general</code>.</p></div></div>')

    for item in results:
        out.append('<div class="card"><div class="card-head">')
        out.append(f'<h2>{html.escape(item["who"])} → {html.escape(item["phone"])}</h2>')
        out.append(f'<span class="sub">{html.escape(item["when"])} · '
                   f'{item["duration"]} с · запись {html.escape(item["uid"])}</span>')
        out.append('</div><div class="body">')
        out.append('<table class="cmp"><colgroup><col style="width:1%"><col class="ya">'
                   '<col class="wh"><col class="lo"></colgroup>')
        out.append('<tr><th></th><th>Яндекс SpeechKit</th><th>whisper-1 (OpenAI)</th>'
                   '<th>местная small</th></tr>')
        for role in ("оператор", "клиент"):
            texts = item["roles"].get(role, {})
            out.append(f'<tr><td class="role">{role}</td>')
            for key in ("yandex", "whisper", "local"):
                css = {"yandex": "ya", "whisper": "wh", "local": "lo"}[key]
                body = html.escape(texts.get(key, "") or "—")
                out.append(f'<td class="{css}">{body}</td>')
            out.append('</tr>')
        out.append('</table>')
        if item["diff"]:
            out.append('<p class="note" style="margin:14px 0 0">'
                       'Разошлись на словах (кто услышал, тот и назван):</p><div class="miss">')
            for word, seen in item["diff"][:24]:
                heard = ", ".join(n for n, c in seen.items() if c) or "никто"
                out.append(f'<b title="услышали: {html.escape(heard)}">{html.escape(word)}</b>')
            out.append('</div>')
        out.append('</div></div>')

    out.append('</div></body></html>')
    return "".join(out)


# ── сборка ────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=10)
    parser.add_argument("--out", default="/var/www/karta/stt-compare.html")
    parser.add_argument("--json", default="data/stt-compare.json")
    parser.add_argument("--no-local", action="store_true",
                        help="Не гонять местную модель: она считает дольше всех.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    key = os.environ.get("DASH_YANDEX_API_KEY", "") or getattr(settings, "yandex_api_key", "")
    folder = os.environ.get("DASH_YANDEX_FOLDER", "") or getattr(settings, "yandex_folder", "")
    if not (key and folder):
        print("нет ключа Яндекса: DASH_YANDEX_API_KEY и DASH_YANDEX_FOLDER")
        return 1

    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Берём только расшифровки, где реплики помечены ролью (`operator:` и
    # `client:`). У просева входящих роли не размечены — там «сторона A» и
    # «сторона B», и класть их рядом с дорожками было бы враньём: неизвестно,
    # кто из них кто.
    rows = conn.execute("""
        SELECT t.call_uid, t.text, k.duration_sec, k.started_at, k.client_phone,
               COALESCE(m.display_name, k.vats_login) AS who
        FROM transcripts t
        JOIN calls k ON k.uid = t.call_uid
        LEFT JOIN managers m ON m.vats_login = k.vats_login
        WHERE t.text LIKE '%operator:%' AND k.duration_sec BETWEEN 60 AND 300
        ORDER BY t.created_at DESC LIMIT 200
    """).fetchall()
    conn.close()

    records = Path("data/records")
    picked = [r for r in rows if (records / f"{r['call_uid']}.mp3").exists()][: args.calls]
    if not picked:
        print("нет записей, у которых есть и расшифровка, и файл")
        return 1

    results: list[dict] = []
    seconds_sent = 0.0
    for index, row in enumerate(picked, 1):
        path = records / f"{row['call_uid']}.mp3"
        logger.info("[%s/%s] %s, %s с", index, len(picked), row["call_uid"], row["duration_sec"])
        roles: dict[str, dict[str, str]] = {}
        for role, channel in ROLES:
            samples = decode_track(path, channel)
            yandex, spent = yandex_track(samples, key, folder)
            seconds_sent += spent
            local = ""
            if not args.no_local:
                try:
                    local = local_track(samples, settings.asr_url, role)
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("  местная модель не ответила: %s", exc)
            roles[role] = {
                "yandex": yandex,
                "whisper": whisper_from_db(row["text"], role),
                "local": local,
            }
            logger.info("  %s: Яндекс %s симв, whisper %s, местная %s",
                        role, len(yandex), len(roles[role]["whisper"]), len(local))
        flat = {name: " ".join(roles[r][name] for r in roles) for name in ("yandex", "whisper", "local")}
        results.append({
            "uid": row["call_uid"],
            "who": row["who"] or "—",
            "phone": row["client_phone"] or "—",
            "when": (row["started_at"] or "")[:16].replace("T", " "),
            "duration": row["duration_sec"],
            "roles": roles,
            "diff": disagreements({k: v for k, v in flat.items() if v}),
        })

    minutes = seconds_sent / 60
    rub = seconds_sent / 15 * PRICE_PER_15S
    # Весь поток компании — около 2 300 разговоров длиннее 15 с в месяц,
    # каждый считается дважды: оператор и клиент отдельными дорожками.
    month_minutes = 2300 * 2.5 * 2
    totals = {
        "calls": len(results),
        "minutes": minutes,
        "rub": rub,
        "rub_month": month_minutes * 60 / 15 * PRICE_PER_15S,
        "usd_month": month_minutes * 0.006,
        "made_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }

    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(
        {"итоги": totals, "разговоры": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    page = render(results, totals)
    out = Path(args.out)
    try:
        out.write_text(page, encoding="utf-8")
    except PermissionError:
        tmp = Path("data/stt-compare.html")
        tmp.write_text(page, encoding="utf-8")
        print(f"страница собрана в {tmp} — выложить: sudo cp {tmp} {out}")
    else:
        print(f"страница собрана: {out}")
    print(f"звука через Яндекс: {minutes:.1f} мин, это {rub:.2f} ₽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
