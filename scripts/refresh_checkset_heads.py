#!/usr/bin/env python
"""Пересчитать начала записей проверочного набора текущим распознаванием.

    sudo -u claude .venv/bin/python scripts/refresh_checkset_heads.py

Зачем отдельно от просева. В таблице `screens` лежат расшифровки, сделанные в
разное время, и у 84 из 146 звонков стороны не разделены: они считались до
того, как разделение дорожек починили. Мерить качество запроса по ним нельзя —
получится оценка вчерашнего распознавания, а не сегодняшнего.

Результат — `data/checkset_heads.json`, его берёт `score_screening.py`.
Считается локальной моделью, денег не стоит, идёт около получаса на 84 записи.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analyzer  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402

logger = logging.getLogger("heads")

ROOT = Path(__file__).resolve().parents[1]
CHECKSET = ROOT / "checkset" / "inbound-screening.json"
SECONDS = 75  # столько же, сколько берёт боевой просев


def head_of_record(path: Path, seconds: int, duration_sec: int) -> bytes:
    """Начало записи по длине файла — так же, как в screen_inbound.py."""
    data = path.read_bytes()
    if duration_sec <= seconds or duration_sec <= 0:
        return data
    return data[: max(int(len(data) * seconds / duration_sec), 16384)]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    doc = json.loads(CHECKSET.read_text(encoding="utf-8"))
    uids = [i["uid"] for i in doc["items"]]

    conn = connect(settings.db_path)
    durations = {
        r["uid"]: r["duration_sec"]
        for r in conn.execute("SELECT uid, duration_sec FROM calls")
    }
    conn.close()

    out_path = Path(settings.db_path).parent / "checkset_heads.json"
    heads: dict[str, str] = {}
    if out_path.exists():
        heads = json.loads(out_path.read_text(encoding="utf-8"))

    records = Path(settings.records_dir)
    started = time.monotonic()
    done = split = 0
    for n, uid in enumerate(uids, 1):
        if uid in heads:
            continue
        path = records / f"{uid}.mp3"
        if not path.exists():
            logger.warning("%s: записи нет", uid)
            continue
        try:
            audio = head_of_record(path, SECONDS, durations.get(uid, 0))
            response = httpx.post(
                f"{settings.asr_url.rstrip('/')}/transcribe",
                files={"file": (f"{uid}.mp3", audio, "audio/mpeg")},
                data={"mode": "split"}, timeout=settings.asr_timeout_sec,
            )
            response.raise_for_status()
            text = analyzer.dialog_text(response.json().get("dialog") or [])
            # Обезличиваем стороны ровно как боевой просев: роль по номеру
            # канала у входящих ненадёжна, кто есть кто — решает модель.
            text = text.replace("operator:", "сторона A:").replace("client:", "сторона B:")
            heads[uid] = text
            done += 1
            if "сторона A" in text:
                split += 1
            out_path.write_text(json.dumps(heads, ensure_ascii=False, indent=1), encoding="utf-8")
            logger.info("%s/%s %s готово (%.0f мин)", n, len(uids), uid,
                        (time.monotonic() - started) / 60)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("%s: %s: %s", uid, type(exc).__name__, exc)

    print(f"пересчитано {done}, со сторонами {split}, всего в кеше {len(heads)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
