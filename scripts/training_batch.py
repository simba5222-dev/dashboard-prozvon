#!/usr/bin/env python
"""Порция звонков для эталонного набора обучения распознавания.

    ./scripts/training_batch.py --batch 1 --minutes 30          под agent
    sudo -u claude .venv/bin/python scripts/training_import.py --batch 1   под claude

Цель владельца (15.11.2026): 50 часов исправленных звонков, начинать по 30 минут в
день. Скрипт:
1. отбирает звонки примерно на `--minutes` минут звука, разные по источнику (входящие
   на прямые номера, разговоры с исполнителями, прозвон) и не входившие в прошлые порции;
2. копирует записи на офисный сервер и считает там черновик моделью `large-v3`
   (половинная точность, фильтр тишины, без «дрейфа» по предыдущему тексту), по
   каждой дорожке отдельно, с метками времени сегментов;
3. вычищает известные выдуманные фразы («Субтитры сделал…» и подобные);
4. пишет `data/training/batch<N>.json`. В базу его загружает `training_import.py`
   (под claude: агент базу писать не должен — см. sync_vats_users.py).

Роли дорожек: у входящих наш сотрудник чаще на левой дорожке, у исходящих — на
правой (замер 28.09.2026: 39 из 50 и 44 из 50). Это только подсказка — на странице
обучения есть отметка «роли перепутаны».
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402

REMOTE_SCRIPT = r'''
import json, os, re, subprocess, sys
from faster_whisper import WhisperModel

manifest = json.load(open("/tmp/train_in/manifest.json", encoding="utf-8"))
BAD = re.compile(r"субтитр|продолжение следует|редактор|корректор|спасибо за просмотр|dimatorzok",
                 re.IGNORECASE)
model = WhisperModel("large-v3", device="cuda", compute_type="float16")
out = []
for item in manifest:
    mp3 = f"/tmp/train_in/{item['uid']}.mp3"
    segs = []
    for channel, role in ((0, item["left"]), (1, item["right"])):
        wav = f"/tmp/train_in/{item['uid']}_{channel}.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-af",
                        f"pan=mono|c0=c{channel}", "-ar", "16000", wav], check=True)
        found, _ = model.transcribe(wav, language="ru", beam_size=5,
                                    condition_on_previous_text=False, vad_filter=True,
                                    vad_parameters=dict(min_silence_duration_ms=500))
        for s in found:
            text = s.text.strip()
            if text and not BAD.search(text):
                segs.append({"start": round(s.start, 2), "end": round(s.end, 2),
                             "role": role, "text": text})
        os.remove(wav)
    segs.sort(key=lambda x: (x["start"], x["role"]))
    out.append({"uid": item["uid"], "segments": segs})
json.dump(out, open("/tmp/train_out.json", "w", encoding="utf-8"), ensure_ascii=False)
print("готово", len(out))
'''

REMOTE_RUN = r'''#!/bin/bash
PY=/home/user/asr-office/.venv/bin/python
SITE=$($PY -c "import site; print(site.getsitepackages()[0])")
export LD_LIBRARY_PATH="$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib:$LD_LIBRARY_PATH"
exec $PY /tmp/train_draft.py
'''


def pick(conn: sqlite3.Connection, records: Path, taken: set[str], minutes: int,
         seed: int) -> list[dict]:
    groups = {
        "входящий на прямой номер": """
            SELECT k.uid FROM calls k JOIN screens s ON s.call_uid = k.uid
             WHERE s.is_request = 1 AND k.duration_sec BETWEEN 50 AND 150""",
        "разговор с исполнителем": """
            SELECT k.uid FROM calls k JOIN search_checks c ON c.call_uid = k.uid
             WHERE c.offered NOT IN ('', '[]') AND k.duration_sec BETWEEN 40 AND 150""",
        "прозвон": """
            SELECT k.uid FROM calls k JOIN transcripts t ON t.call_uid = k.uid
             WHERE k.in_group = 1 AND k.duration_sec BETWEEN 60 AND 150
               AND COALESCE(t.analysis_json, '') NOT LIKE '%"unusable": true%'""",
    }
    rnd = random.Random(seed)
    pools: dict[str, list[str]] = {}
    for name, sql in groups.items():
        uids = [r[0] for r in conn.execute(sql) if r[0] not in taken
                and (records / f"{r[0]}.mp3").exists()]
        rnd.shuffle(uids)
        pools[name] = uids
    chosen: list[dict] = []
    total, limit = 0, minutes * 60
    names = list(pools)
    while total < limit and any(pools.values()):
        for name in names:
            if not pools[name] or total >= limit:
                continue
            uid = pools[name].pop()
            row = conn.execute(
                "SELECT k.uid, k.direction, k.duration_sec, k.started_at, "
                "       COALESCE(m.display_name, k.vats_login, '') AS manager "
                "  FROM calls k LEFT JOIN managers m ON m.vats_login = k.vats_login "
                " WHERE k.uid = ?", (uid,)).fetchone()
            if row is None or total + row["duration_sec"] > limit + 60:
                continue
            chosen.append({"uid": uid, "source": name, "direction": row["direction"],
                           "duration_sec": int(row["duration_sec"]),
                           "started_at": row["started_at"], "manager": row["manager"]})
            total += int(row["duration_sec"])
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--seed", type=int, default=0, help="Другое число — другая выборка.")
    args = ap.parse_args()

    settings = Settings(_env_file=None)
    records = Path(settings.records_dir)
    out_dir = Path(settings.db_path).parent / "training"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"batch{args.batch}.json"

    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        taken = {r[0] for r in conn.execute("SELECT call_uid FROM training_items")}
    except sqlite3.OperationalError:
        taken = set()  # таблицы ещё нет: дашборд её создаёт при запуске
    for old in out_dir.glob("batch*.json"):
        if old != out_file:
            taken |= {i["uid"] for i in json.loads(old.read_text(encoding="utf-8"))["items"]}
    items = pick(conn, records, taken, args.minutes, args.seed + args.batch)
    conn.close()
    total = sum(i["duration_sec"] for i in items)
    print(f"порция {args.batch}: {len(items)} звонков, {total / 60:.1f} мин звука")
    for source in sorted({i["source"] for i in items}):
        print(f"  {source}: {sum(1 for i in items if i['source'] == source)}")

    manifest = []
    for i in items:
        outgoing = i["direction"] == "out"
        manifest.append({"uid": i["uid"],
                         "left": "клиент" if outgoing else "менеджер",
                         "right": "менеджер" if outgoing else "клиент"})

    run = lambda cmd, **kw: subprocess.run(cmd, check=True, **kw)  # noqa: E731
    run(["ssh", "office", "rm -rf /tmp/train_in && mkdir -p /tmp/train_in"])
    Path("/tmp/train_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    run(["scp", "-q", "/tmp/train_manifest.json", "office:/tmp/train_in/manifest.json"])
    Path("/tmp/train_draft.py").write_text(REMOTE_SCRIPT, encoding="utf-8")
    Path("/tmp/train_run.sh").write_text(REMOTE_RUN, encoding="utf-8")
    run(["scp", "-q", "/tmp/train_draft.py", "/tmp/train_run.sh", "office:/tmp/"])
    run(["scp", "-q", *[str(records / f"{i['uid']}.mp3") for i in items], "office:/tmp/train_in/"])
    print("записи на офисе, считаю черновики на GPU…")
    run(["ssh", "office", "bash /tmp/train_run.sh"])
    run(["scp", "-q", "office:/tmp/train_out.json", "/tmp/train_out.json"])
    drafts = {d["uid"]: d["segments"] for d in json.loads(Path("/tmp/train_out.json").read_text())}
    for i in items:
        i["segments"] = drafts.get(i["uid"], [])
    out_file.write_text(json.dumps({"batch": args.batch, "items": items}, ensure_ascii=False,
                                   indent=1), encoding="utf-8")
    run(["ssh", "office", "rm -rf /tmp/train_in /tmp/train_out.json"])
    print(f"готово: {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
