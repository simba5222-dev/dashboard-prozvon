"""Разбор почты info@techno-res.ru: заявки на аренду техники и поставку
нерудных материалов — прямо из письма в заявку CRM.

Почта этого ящика получилась полезнее звонков в одном смысле: клиент сам
пишет, что ему нужно, и это уже дословный текст, не рваная расшифровка.
Поэтому модель здесь работает с чистым текстом и может позволить себе модель
подешевле (см. `mail_intake_model`).

Вложения из письма в заявку **не прикладываем** — у Synergy нет рабочего
способа загрузить файл через публичный API (`POST /documents` отдаёт 404,
как и `POST /diaries` раньше). Решение владельца 15.10.2026: пока без
вложений, в заявке только упоминание, что в письме было N файлов — менеджер
смотрит оригинал письма сам.

Каждое письмо один раз: UID письма в INBOX растёт монотонно, `last_mail_uid()`
отвечает на вопрос «докуда дошли» без отдельного файла состояния.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from app import analyzer
from app.collector import SynergyClient, create_contact, fill_contact_blanks, find_contact, load_stages
from app.config import Settings
from app.crm_write import FIELD_MARK, FIELD_SUMMARY, MARK_AI, CrmWriter
from app.db import last_mail_uid, save_mail_lead

logger = logging.getLogger(__name__)

OUR_DOMAIN = "techno-res.ru"
MAX_MAIL_AGE_DAYS = 7
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s\-\(]?\d{3}[\s\-\)]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)")

PROMPT = """Ты разбираешь письмо на общую почту info@techno-res.ru. Компания
занимается арендой спецтехники (краны, экскаваторы, самосвалы и т.п.) и
поставкой нерудных материалов (песок, щебень, грунт и подобное).

Определи, является ли письмо ЗАЯВКОЙ от клиента — прямой просьбой об аренде
техники или о поставке нерудных материалов (запрос цены, наличия, сроков,
условий). Это НЕ заявка: письма от поставщиков и подрядчиков с предложением
СВОИХ услуг нам, реклама, спам, бухгалтерские документы и счета, рассылки,
служебная переписка внутри компании, вопросы не по аренде/материалам.

ПИСЬМО
От: {from_header}
Тема: {subject}
Текст:
{body}

Верни JSON:
{{"is_request": true/false,
  "phone": "<телефон для связи с клиентом, как в письме; пусто, если нет>",
  "contact_name": "<имя контактного лица, если указано; пусто, если нет>",
  "company_name": "<название компании клиента, если указано; пусто, если нет>",
  "summary": "<2-3 предложения своими словами: что именно просит клиент>"}}

Ответ — только JSON, без пояснений и без markdown-обрамления."""


def decode_words(value: str | None) -> str:
    if not value:
        return ""
    out = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def extract_plain_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    parts.append(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"))
                except (LookupError, UnicodeDecodeError, TypeError):
                    continue
        if parts:
            return "\n".join(parts)
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except (LookupError, UnicodeDecodeError, TypeError):
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError, TypeError, AttributeError):
        return str(msg.get_payload())


def attachment_names(msg: email.message.Message) -> list[str]:
    names = []
    if msg.is_multipart():
        for part in msg.walk():
            filename = part.get_filename()
            if filename and part.get_content_type() not in ("text/plain", "text/html"):
                names.append(decode_words(filename))
    return names


def guess_phone(text: str) -> str:
    m = PHONE_RE.search(text or "")
    return m.group(0) if m else ""


def imap_connect(settings: Settings) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(settings.mail_intake_host, 993)
    conn.login(settings.mail_intake_user, settings.mail_intake_password)
    return conn


def fetch_new(imap_conn: imaplib.IMAP4_SSL, since_uid: int, limit: int) -> list[tuple[int, bytes]]:
    """UID и сырой текст писем новее since_uid, не больше limit штук."""
    imap_conn.select("INBOX", readonly=True)
    typ, data = imap_conn.uid("search", None, f"UID {since_uid + 1}:*")
    if typ != "OK" or not data or not data[0]:
        return []
    uids = [int(u) for u in data[0].split() if int(u) > since_uid]
    uids.sort()
    out = []
    for uid in uids[:limit]:
        typ, fdata = imap_conn.uid("fetch", str(uid), "(RFC822)")
        if typ == "OK" and fdata and fdata[0]:
            out.append((uid, fdata[0][1]))
    return out


def classify(settings: Settings, from_header: str, subject: str, body: str) -> dict[str, Any] | None:
    prompt = PROMPT.format(from_header=from_header, subject=subject, body=body[:6000])
    yandex_engine = settings.mail_intake_engine == "yandex"
    api_key = (settings.yandex_api_key if yandex_engine else settings.openai_api_key) or ""
    return analyzer._спросить_голову(
        prompt, engine=settings.mail_intake_engine, api_key=api_key,
        model=settings.mail_intake_model, folder=settings.yandex_folder or "",
        kind="разбор письма info@",
    )


def process_one(
    settings: Settings, client: SynergyClient, writer: CrmWriter, stage_id: str | None,
    uid: int, raw: bytes,
) -> dict[str, Any]:
    msg = email.message_from_bytes(raw)
    from_name, from_addr = parseaddr(decode_words(msg.get("From")))
    subject = decode_words(msg.get("Subject"))
    date_str = msg.get("Date")
    try:
        received_at = parsedate_to_datetime(date_str).isoformat() if date_str else None
    except (TypeError, ValueError):
        received_at = None
    body = extract_plain_text(msg)
    attachments = attachment_names(msg)

    row: dict[str, Any] = {
        "uid": uid, "message_id": msg.get("Message-ID"), "received_at": received_at,
        "from_addr": from_addr, "subject": subject, "is_request": 0,
        "phone": None, "contact_id": None, "contact_created": 0, "order_id": None,
        "attachments_count": len(attachments), "note": "",
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    if OUR_DOMAIN in (from_addr or "").lower():
        row["note"] = "письмо от своего адреса — пропущено"
        return row

    # Письма перенесённого архива (imapsync) приходят с новыми UID, но старой датой:
    # 20.11.2026 из писем 2017–2018 годов создались две ложные заявки. Старше недели не берём.
    if received_at:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(received_at)
        except (TypeError, ValueError):
            age = None
        if age is not None and age.days > MAX_MAIL_AGE_DAYS:
            row["note"] = f"старое письмо ({received_at[:10]}) — перенесённый архив, пропущено"
            return row

    verdict = classify(settings, f"{from_name} <{from_addr}>", subject, body)
    if verdict is None:
        row["note"] = "модель не ответила"
        return row
    if not verdict.get("is_request"):
        row["note"] = "не заявка: " + str(verdict.get("summary") or "")[:200]
        return row

    row["is_request"] = 1
    phone = str(verdict.get("phone") or "").strip() or guess_phone(body) or guess_phone(from_addr)
    row["phone"] = phone or None
    contact_name = str(verdict.get("contact_name") or from_name or "").strip()
    company_name = str(verdict.get("company_name") or "").strip()
    summary = str(verdict.get("summary") or "").strip()

    if not phone:
        row["note"] = "заявка без телефона — заявку не завожу, нужен ручной разбор"
        return row

    contact = find_contact(client, phone)
    if contact is None:
        if writer.dry_run:
            row["note"] = f"сухой прогон: завели бы контакт по телефону {phone}"
            return row
        contact_id = create_contact(
            client, phone=phone, name=contact_name or company_name, email=from_addr)
        row["contact_created"] = 1 if contact_id else 0
        if not contact_id:
            row["note"] = "не удалось завести контакт"
            return row
    else:
        contact_id = str(contact["id"])
        if not writer.dry_run:
            fill_contact_blanks(client, contact, email=from_addr, name=contact_name or company_name)
    row["contact_id"] = contact_id

    comment_parts = [
        f"Письмо от {from_addr}" + (f" ({from_name})" if from_name else ""),
        f"Тема: {subject}" if subject else "",
        f"Компания клиента (из письма): {company_name}" if company_name else "",
        "",
        summary or body[:1000],
    ]
    if attachments:
        comment_parts.append("")
        comment_parts.append(
            f"В письме {len(attachments)} вложений ({', '.join(attachments[:10])}) — "
            "не прикреплены к заявке (Synergy не даёт загрузить файл через API), "
            "смотреть оригинал письма в info@.")
    comment = "\n".join(p for p in comment_parts if p is not None)

    name = settings.mail_intake_order_name
    if company_name:
        name = f"{name}: {company_name}"
    elif contact_name:
        name = f"{name}: {contact_name}"

    order_id = writer.create_order(
        contact_id=contact_id, name=name, stage_id=stage_id,
        responsible_id=settings.mail_intake_responsible_id,
        customs={FIELD_MARK: [MARK_AI], FIELD_SUMMARY: summary} if summary else {FIELD_MARK: [MARK_AI]},
        comment=comment,
    )
    row["order_id"] = order_id
    if order_id:
        row["note"] = "заявка заведена"
    elif writer.dry_run:
        row["note"] = f"сухой прогон: завели бы заявку «{name}»"
    else:
        row["note"] = "заявка не создана (см. журнал)"
    return row


def run(conn, settings: Settings, *, apply: bool = False, limit: int = 20) -> dict[str, Any]:
    """Разобрать новые письма info@. Без apply — только посмотреть, что бы сделали."""
    if not settings.mail_intake_configured:
        return {"error": "почта info@ не настроена (DASH_MAIL_INTAKE_USER/PASSWORD)"}
    если_yandex = settings.mail_intake_engine == "yandex"
    if если_yandex and not settings.yandex_api_key:
        return {"error": "ключ Яндекса не задан"}
    if not если_yandex and not settings.analysis_configured:
        return {"error": "ключ OpenAI не задан"}

    since_uid = last_mail_uid(conn)
    imap_conn = imap_connect(settings)
    try:
        messages = fetch_new(imap_conn, since_uid, limit)
    finally:
        try:
            imap_conn.logout()
        except Exception:  # noqa: BLE001
            pass

    if not messages:
        return {"checked": 0, "requests": 0, "orders": 0}

    client = SynergyClient(base_url=settings.synergy_url, token=settings.synergy_api_token)
    writer = CrmWriter(client, apply=apply)
    stages = load_stages(client)
    stage_id = next((sid for sid, (name, _kind) in stages.items()
                     if name.strip().lower() == settings.crm_lead_stage.lower()), None)

    requests_found = 0
    orders_created = 0
    for uid, raw in messages:
        try:
            row = process_one(settings, client, writer, stage_id, uid, raw)
        except Exception as exc:  # noqa: BLE001 — одно плохое письмо не рушит проход
            logger.exception("письмо info@ uid=%s не разобралось", uid)
            row = {
                "uid": uid, "message_id": None, "received_at": None, "from_addr": None,
                "subject": None, "is_request": 0, "phone": None, "contact_id": None,
                "contact_created": 0, "order_id": None, "attachments_count": 0,
                "note": f"ошибка разбора: {exc}",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
        if row.get("is_request"):
            requests_found += 1
        if row.get("order_id"):
            orders_created += 1
        if apply:
            save_mail_lead(conn, **row)
            conn.commit()
        else:
            logger.info("сухой прогон, письмо %s: %s", uid, row.get("note"))
    return {"checked": len(messages), "requests": requests_found, "orders": orders_created}
