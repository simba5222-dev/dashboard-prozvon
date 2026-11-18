#!/usr/bin/env python
"""Синхронизация чатов Авито (Messenger API) в нашу базу. Только чтение из Авито.

    sudo -u claude .venv/bin/python scripts/avito_chats_sync.py            свежее (для таймера): чаты, менявшиеся за --days
    sudo -u claude .venv/bin/python scripts/avito_chats_sync.py --full     вся доступная история: свежие ~1 100 чатов
                                                                          и по каждому объявлению (старше апреля)

Идемпотентно: чат и сообщение обновляются по своему id. Сообщения читаются только у чатов, которые изменились с прошлого
чтения (`updated` чата больше сохранённого `msgs_fetched_updated`). Отправки сообщений и отметки «прочитано» здесь нет.
Пишет в базу дашборда — запускать от claude.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.avito import AvitoClient, AvitoError  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_schema, save_avito_chat, save_avito_message  # noqa: E402

PAGE = 100
MAX_OFFSET = 1000


def chat_fields(ch: dict, my_id: str) -> dict:
    ctx = (ch.get("context") or {}).get("value") or {}
    other = next((u for u in ch.get("users") or [] if str(u.get("id")) != my_id), {})
    last = ch.get("last_message") or {}
    return dict(chat_id=str(ch["id"]), item_id=ctx.get("id"), item_title=str(ctx.get("title") or ""), item_url=str(ctx.get("url") or ""),
                client_id=str(other.get("id") or ""), client_name=str(other.get("name") or ""), created=int(ch.get("created") or 0),
                updated=int(ch.get("updated") or 0), last_direction=str(last.get("direction") or ""), last_type=str(last.get("type") or ""))


def iter_chats(cl: AvitoClient, since_ts: int, item_ids: list[int] | None = None):
    """Чаты от свежих к старым, пока `updated` не станет меньше since_ts или список не кончится (offset ≤ 1000)."""
    offset = 0
    while offset <= MAX_OFFSET:
        try:
            d = cl.chats(limit=PAGE, offset=offset, item_ids=item_ids)
        except AvitoError as exc:
            if "HTTP 400" in str(exc):   # дошли до предела глубины
                return
            raise
        rows = d.get("chats") or []
        for ch in rows:
            if int(ch.get("updated") or 0) < since_ts:
                return
            yield ch
        if len(rows) < PAGE:
            return
        offset += PAGE


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3, help="за сколько последних дней брать изменившиеся чаты")
    ap.add_argument("--full", action="store_true", help="вся доступная история")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    s = Settings()
    cl = AvitoClient(s.avito_client_id or "", s.avito_client_secret or "", s.avito_user_id, min_interval_sec=0.6)
    my_id = str(s.avito_user_id)
    conn = connect(s.db_path)
    init_schema(conn)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    since_ts = 0 if args.full else int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp())

    # 1. чаты
    seen: dict[str, int] = {}
    try:
        for ch in iter_chats(cl, since_ts):
            f = chat_fields(ch, my_id)
            save_avito_chat(conn, fetched_at=stamp, **f)
            seen[f["chat_id"]] = f["updated"]
        print(f"свежие чаты: {len(seen)}", flush=True)
        if args.full:   # старше предела глубины — по объявлениям
            items = [r[0] for r in conn.execute("SELECT DISTINCT item_id FROM avito_calls WHERE item_id IS NOT NULL")]
            items += [r[0] for r in conn.execute("SELECT DISTINCT item_id FROM avito_chats WHERE item_id IS NOT NULL") if r[0] not in items]
            for n, it in enumerate(items, 1):
                before = len(seen)
                for ch in iter_chats(cl, 0, item_ids=[it]):
                    f = chat_fields(ch, my_id)
                    save_avito_chat(conn, fetched_at=stamp, **f)
                    seen[f["chat_id"]] = f["updated"]
                if n % 10 == 0 or len(seen) > before + 50:
                    print(f"  объявлений обработано {n}/{len(items)}, чатов накоплено {len(seen)}", flush=True)
                conn.commit()
    except AvitoError as exc:
        conn.commit()
        print(f"остановились на ошибке Авито при чтении чатов: {exc}", flush=True)
        return 1
    conn.commit()

    # 2. сообщения — только у изменившихся чатов
    todo = [r[0] for r in conn.execute(
        "SELECT chat_id FROM avito_chats WHERE updated > msgs_fetched_updated AND updated >= ? ORDER BY updated DESC", (since_ts,))]
    print(f"чатов, у которых нужно прочитать сообщения: {len(todo)}", flush=True)
    done = msgs = 0
    failed: list[str] = []
    t0 = time.time()
    for chat_id in todo:
        upd = conn.execute("SELECT updated FROM avito_chats WHERE chat_id = ?", (chat_id,)).fetchone()[0]
        offset = 0
        try:
            while offset <= MAX_OFFSET:
                d = cl.messages(chat_id, limit=PAGE, offset=offset)
                rows = d.get("messages") or []
                for m in rows:
                    content = m.get("content") or {}
                    text = content.get("text") if isinstance(content, dict) else str(content or "")
                    save_avito_message(conn, message_id=str(m["id"]), chat_id=chat_id, created=int(m.get("created") or 0),
                                       direction=str(m.get("direction") or ""), author_id=str(m.get("author_id") or ""),
                                       type_=str(m.get("type") or ""), text=str(text or ""), is_read=bool(m.get("isRead")), fetched_at=stamp)
                    msgs += 1
                if len(rows) < PAGE:
                    break
                offset += PAGE
        except AvitoError as exc:
            if "HTTP 5" in str(exc):
                # один чат стабильно отвечает ошибкой сервера Авито (все повторы исчерпаны): не блокируем остальные;
                # помечаем прочитанным до следующего изменения чата и записываем в журнал
                failed.append(chat_id)
                print(f"  чат {chat_id} пропущен (сервер Авито отвечает ошибкой): {str(exc)[:80]}", flush=True)
                conn.execute("UPDATE avito_chats SET msgs_fetched_updated = ? WHERE chat_id = ?", (upd, chat_id))
                continue
            conn.commit()
            print(f"остановились на ошибке Авито (прочитано чатов {done}, сообщений {msgs}): {exc}", flush=True)
            return 1
        conn.execute("UPDATE avito_chats SET msgs_fetched_updated = ? WHERE chat_id = ?", (upd, chat_id))
        done += 1
        if done % 100 == 0:
            conn.commit()
            print(f"  прочитано чатов {done}/{len(todo)}, сообщений {msgs}, {(time.time() - t0) / done:.1f} с на чат", flush=True)
    conn.commit()
    tc = conn.execute("SELECT COUNT(*) FROM avito_chats").fetchone()[0]
    tm = conn.execute("SELECT COUNT(*) FROM avito_messages").fetchone()[0]
    print(f"ГОТОВО: чатов в таблице {tc}, сообщений {tm} (за этот запуск прочитано чатов {done}, сообщений {msgs}, пропущено из-за ошибок Авито {len(failed)})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
