"""Распознавание коротких разговоров через whisper-1, по дорожкам.

Зачем отдельно от местной модели. Разговоры поисковика короткие — средний
состоявшийся 23.09.2026 длился 17 секунд — и на них местная `small` рвёт
телефонный звук до неузнаваемости: «На сколько семена?» вместо «На сколько
смена?». Сверка техники по такому тексту бессмысленна.

Тот же разговор через `whisper-1`:

    Скажите, пожалуйста, экскаватор погрузчика с гидромолотом есть у вас?
    Нет, нет.

Цена вопроса — $0.006 за минуту звука. У отдела поиска это около десяти
минут разговоров в день на человека, то есть примерно два доллара в месяц
за дорожку. Считаем две дорожки отдельно, значит вдвое.

**Почему по дорожкам, а не файлом целиком.** Запись ВАТС стерео: слева
менеджер, справа собеседник. Отправив файл целиком, получаем сплошной текст
без ролей — а вся задача сверки держится ровно на том, кто сказал «у меня
есть самосвал»: менеджер, перечисляя, что ищет, или поставщик. Поэтому
дорожки режутся и распознаются порознь, а потом сшиваются по времени.
"""

from __future__ import annotations

import logging
import tempfile
import wave
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Левый канал — менеджер, правый — собеседник. Договорённость та же, что в
# сервисе распознавания (`left_role` / `right_role` в его настройках):
# разойдутся — и роли в разборе перевернутся молча.
ROLES = ("operator", "client")

RATE = 16000


def split_channels(path: Path, target: Path) -> list[Path]:
    """Разложить запись на моно-дорожки. Одна дорожка — один файл WAV.

    Читаем через PyAV: он несёт свой FFmpeg внутри, системный не нужен.
    """
    import av
    import numpy as np

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        channels = stream.channels or 1
        resampler = av.AudioResampler(format="s16", layout="stereo" if channels > 1 else "mono",
                                      rate=RATE)
        chunks: list[Any] = []
        for frame in container.decode(stream):
            for piece in resampler.resample(frame):
                data = piece.to_ndarray()
                # PyAV отдаёт упакованный кадр одной строкой: каналы чередуются.
                if data.shape[0] == 1 and channels > 1:
                    data = data.reshape(-1, channels).T
                chunks.append(data)
    if not chunks:
        return []
    matrix = np.concatenate(chunks, axis=1)
    if matrix.shape[0] == 1:
        channels = 1

    out: list[Path] = []
    for index in range(min(channels, 2)):
        track = target / f"{path.stem}-{ROLES[index]}.wav"
        with wave.open(str(track), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(RATE)
            handle.writeframes(matrix[index].astype("<i2").tobytes())
        out.append(track)
    return out


def transcribe(path: Path, *, api_key: str, model: str = "whisper-1",
               language: str = "ru", timeout_sec: float = 120.0) -> list[dict[str, Any]]:
    """Разговор в виде реплик с ролями: [{role, text, start}, …].

    Каждая дорожка распознаётся отдельно, реплики сшиваются по времени.
    Пустые куски отбрасываем: на молчащей дорожке whisper охотно выдумывает
    «Субтитры сделал DimaTorzok» и прочий мусор из обучающих данных.
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=timeout_sec)
    turns: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="voice-") as tmp:
        tracks = split_channels(path, Path(tmp))
        if not tracks:
            return []
        for index, track in enumerate(tracks):
            role = ROLES[index] if index < len(ROLES) else "speaker"
            with track.open("rb") as handle:
                result = client.audio.transcriptions.create(
                    model=model, file=handle, language=language,
                    response_format="verbose_json",
                )
            for segment in getattr(result, "segments", None) or []:
                text = (segment.text or "").strip()
                if not text or _is_noise(text):
                    continue
                turns.append({"role": role, "text": text,
                              "start": float(getattr(segment, "start", 0.0))})
    turns.sort(key=lambda item: item["start"])
    return turns


# Фразы, которые whisper дописывает на пустой дорожке. Взяты с живых записей:
# на молчании модель выдаёт титры из обучающего набора, и в расшифровке
# появляется собеседник, который ничего не говорил.
NOISE = (
    "субтитры",
    "продолжение следует",
    "редактор субтитров",
    "dimatorzok",
    "корректор",
)


def _is_noise(text: str) -> bool:
    low = text.casefold()
    return any(mark in low for mark in NOISE)
