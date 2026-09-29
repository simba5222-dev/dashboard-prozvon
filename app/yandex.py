"""Российский распознаватель и российская голова — SpeechKit и AI Assistants.

Здесь ровно то, что ходит в облако Яндекса. Собрано в одном месте, чтобы
переключение уха или головы было правкой одного файла, а не поиском по
всему проекту.

**Распознавание идёт третьим поколением API.** Оно принимает звук прямо в
теле запроса — бакет в Object Storage не нужен, а с ним ушло и ограничение
в 30 секунд, из-за которого первая версия резала дорожки на куски.

**Берём нормализованный текст**, а не сырой: в нём знаки препинания,
заглавные буквы и числа цифрами. Он приходит отдельным событием
`finalRefinement`, и это легко проглядеть — сырой `final` лежит рядом и
выглядит как ответ.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# Свой учёт расхода. У OpenAI он у нас был с 18.09, у Яндекса не было — и
# 29.09.2026 это стоило суток неведения: владелец увидел 360 ₽ в кабинете
# раньше, чем мы узнали, за что они. Цены — из выгрузки биллинга, ₽.
ЦЕНЫ = {
    "deferred-general": 0.152 / 60,   # ₽ за секунду дорожки, отложенный режим
    "general": 0.606 / 60,            # то же самое вчетверо дороже
    "general:rc": 0.606 / 60,
    "general:deprecated": 0.606 / 60,
    "yandexgpt": 1.20 / 1000,         # ₽ за токен, Pro 5
    "yandexgpt-lite": 0.20 / 1000,    # Lite 5
}
УЧЁТ = "yandex_usage.jsonl"


def _записать(запись: dict) -> None:
    """Дописать строку расхода рядом с базой. Учёт не должен ронять работу."""
    try:
        from app.config import get_settings

        путь = Path(get_settings().db_path).parent / УЧЁТ
        путь.parent.mkdir(parents=True, exist_ok=True)
        with путь.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(запись, ensure_ascii=False) + "\n")
    except (OSError, ValueError) as exc:
        logger.debug("учёт расхода не записан: %s", exc)

RATE = 16000
STT_START = "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync"
STT_RESULT = "https://stt.api.cloud.yandex.net/stt/v3/getRecognition"
OPERATIONS = "https://operation.api.cloud.yandex.net/operations"
ASSIST = "https://rest-assistant.api.cloud.yandex.net/assistants/v1"

# `deferred-general`, и это про деньги, а не про качество.
#
# Текст у него тот же: 29.09.2026 прогнали одну дорожку через `general`,
# `general:rc`, `general:deprecated` и `deferred-general` — первый, третий и
# четвёртый дали побуквенно одинаковый результат, а `general:rc` потерял «не»
# («у меня не стоит» стало «у меня стоит»).
#
# А в счёте разница вчетверо, и видно её только в детализации:
#     `general`          → «Асинхронное распознавание»            0,606 ₽/мин
#     `deferred-general` → «Асинхронное, отложенный режим»         0,152 ₽/мин
# Оба идут через один и тот же `recognizeFileAsync`, поэтому по коду они
# неразличимы: платишь вчетверо больше за тот же ответ и не узнаёшь об этом,
# пока не откроешь выгрузку по биллингу.
#
# Плата за отложенность — время: результат готов не мгновенно. Для разбора
# записей это безразлично, для живого разговора — нет.
STT_MODEL = "deferred-general"


def track(path: str, channel: int) -> np.ndarray:
    """Одна дорожка записи: 16 кГц, моно, int16. Левая — наш менеджер."""
    import av

    with av.open(str(path)) as container:
        stereo = container.streams.audio[0].channels > 1
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


def transcribe_track(samples: np.ndarray, *, api_key: str, folder: str,
                     timeout_sec: float = 900.0) -> str:
    """Дорожка целиком через отложенное распознавание.

    Порядок важен: сначала ждём, пока операция отметится завершённой, и только
    потом просим текст. Спросить раньше — получить 404.
    """
    headers = {"Authorization": f"Api-Key {api_key}", "x-folder-id": folder}
    body = {
        "content": base64.b64encode(samples.tobytes()).decode(),
        "recognitionModel": {
            "model": STT_MODEL,
            "audioFormat": {"rawAudio": {"audioEncoding": "LINEAR16_PCM",
                                         "sampleRateHertz": RATE, "audioChannelCount": 1}},
            "textNormalization": {"textNormalization": "TEXT_NORMALIZATION_ENABLED",
                                  "literatureText": True, "profanityFilter": False},
            "audioProcessingType": "FULL_DATA",
        },
    }
    started = time.time()
    _записать({"at": datetime.now(timezone.utc).isoformat(), "kind": "распознавание",
               "model": STT_MODEL, "seconds": round(len(samples) / RATE, 1),
               "rub": round(len(samples) / RATE * ЦЕНЫ.get(STT_MODEL, 0), 3)})
    try:
        response = httpx.post(STT_START, headers=headers, json=body, timeout=300)
        response.raise_for_status()
        operation = response.json()["id"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("Яндекс не принял дорожку: %s", exc)
        return ""

    while time.time() - started < timeout_sec:
        time.sleep(3)
        try:
            state = httpx.get(f"{OPERATIONS}/{operation}", headers=headers, timeout=60).json()
        except (httpx.HTTPError, ValueError):
            continue
        if state.get("error"):
            logger.warning("Яндекс вернул ошибку: %s", str(state["error"])[:200])
            return ""
        if state.get("done"):
            break
    else:
        logger.warning("Яндекс не ответил за %s с", int(timeout_sec))
        return ""

    try:
        page = httpx.get(STT_RESULT, headers=headers,
                         params={"operationId": operation}, timeout=180)
    except httpx.HTTPError as exc:
        logger.warning("результат не забран: %s", exc)
        return ""
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
    return " ".join(said).strip()


def transcribe_dialog(path: str, *, api_key: str, folder: str) -> str:
    """Оба канала записи в одном тексте, с пометкой роли у каждой реплики."""
    куски = []
    for роль, канал in (("operator", 0), ("client", 1)):
        текст = transcribe_track(track(path, канал), api_key=api_key, folder=folder)
        if текст:
            куски.append(f"{роль}: {текст}")
    return "\n".join(куски)


def ask(assistant_id: str, question: str, *, api_key: str, folder: str,
        timeout_sec: float = 180.0) -> str:
    """Задать вопрос ассистенту и дождаться ответа.

    Нить создаётся под один вопрос и удаляется сразу: разговоры между собой
    не связаны, а нити копятся в облаке и мешают потом разбираться.
    """
    headers = {"Authorization": f"Api-Key {api_key}"}
    try:
        thread = httpx.post(f"{ASSIST}/threads", headers=headers,
                            json={"folderId": folder}, timeout=60).json()["id"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("нить не создана: %s", exc)
        return ""
    try:
        httpx.post(f"{ASSIST}/messages", headers=headers, json={
            "threadId": thread,
            "content": {"content": [{"text": {"content": question}}]}}, timeout=60)
        run = httpx.post(f"{ASSIST}/runs", headers=headers,
                         json={"assistantId": assistant_id, "threadId": thread},
                         timeout=60).json()["id"]
        started = time.time()
        while time.time() - started < timeout_sec:
            time.sleep(3)
            state = (httpx.get(f"{ASSIST}/runs/{run}", headers=headers, timeout=60)
                     .json().get("state") or {})
            status = str(state.get("status") or "")
            if status.startswith("COMPLETED"):
                text = ""
                message = state.get("completed_message") or {}
                for part in (message.get("content") or {}).get("content", []):
                    text += (part.get("text") or {}).get("content", "")
                return text.strip()
            if status == "FAILED":
                logger.warning("разбор не вышел: %s", json.dumps(state, ensure_ascii=False)[:200])
                return ""
        logger.warning("ассистент не ответил за %s с", int(timeout_sec))
        return ""
    finally:
        httpx.delete(f"{ASSIST}/threads/{thread}", headers=headers, timeout=30)


def complete(prompt: str, *, api_key: str, folder: str, model: str = "yandexgpt",
             max_tokens: int = 1500, temperature: float = 0.0,
             timeout_sec: float = 180.0) -> str:
    """Прямой вызов модели, без памяти. Сюда идут длинные тексты.

    **Расшифровку нельзя отдавать ассистенту с индексом.** Ассистент ищет в
    памяти по всему сообщению целиком, и длинный запрос OpenSearch отклоняет:
    на 500 символах ещё работает, на 2 000 уже падает. Поэтому память
    спрашиваем коротким вопросом, а разбираем разговор прямым вызовом.
    """
    body = {
        "modelUri": f"gpt://{folder}/{model}/latest",
        "completionOptions": {"temperature": temperature, "maxTokens": max_tokens},
        "messages": [{"role": "user", "text": prompt}],
    }
    try:
        response = httpx.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={"Authorization": f"Api-Key {api_key}"}, json=body, timeout=timeout_sec)
        response.raise_for_status()
        result = response.json()["result"]
        usage = result.get("usage") or {}
        токенов = int(usage.get("totalTokens") or 0)
        _записать({"at": datetime.now(timezone.utc).isoformat(), "kind": "генерация",
                   "model": model, "tokens": токенов,
                   "in": int(usage.get("inputTextTokens") or 0),
                   "out": int(usage.get("completionTokens") or 0),
                   "rub": round(токенов * ЦЕНЫ.get(model, 0), 3)})
        return str(result["alternatives"][0]["message"]["text"]).strip()
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        logger.warning("модель не ответила: %s", exc)
        return ""


def parse_json(text: str) -> dict[str, Any] | None:
    """Достать JSON из ответа модели: она любит обрамлять его пояснениями."""
    if not text:
        return None
    чистый = text.strip()
    if чистый.startswith("```"):
        чистый = чистый.split("```")[1]
        чистый = чистый[4:] if чистый.startswith("json") else чистый
    начало, конец = чистый.find("{"), чистый.rfind("}")
    if начало < 0 or конец <= начало:
        return None
    try:
        данные = json.loads(чистый[начало: конец + 1])
    except ValueError:
        return None
    return данные if isinstance(данные, dict) else None
