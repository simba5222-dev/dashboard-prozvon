"""Отчёт «кто не проставил наработку техники за день» — контроль дебиторки.

Сделки (`deals`, отдельный от `orders` ресурс Synergy — не путать, у заявок
другие id полей) трекают ежедневную наработку в 31 слоте подряд
(`custom-478`..`custom-508`), каждый — строка вида
«05.11.2026 / 9 / 9» (дата / часы заказчика / часы исполнителя).
Заполняются по порядку внесения, не по номеру дня месяца — поэтому дата
конкретного дня ищется по всей цепочке, а не по фиксированному слоту.

Обнаружено 05.11.2026 на тестовой сделке id 3642282 («тест отчет
наработка»), заведённой владельцем специально для проверки: в ней видна
ровно дыра за 04.11 между соседними записями 03.11 и 05.11.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.collector import SynergyClient

NARABOTKA_SLOTS = tuple(f"custom-{478 + i}" for i in range(31))
FIELD_START = "custom-11912"    # «Начало работ» — у deals; у orders то же название лежит в custom-812
FIELD_BALANCE = "custom-12012"  # «Остаток руб З»

# Сделку, которую не трогали дольше этого срока, не проверяем — трекинг
# наработки имеет смысл только пока техника реально на объекте.
SCAN_WINDOW_DAYS = 30


def _parse_date_ru(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except (ValueError, AttributeError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _entry_dates(customs: dict[str, Any]) -> set[date]:
    out: set[date] = set()
    for slot in NARABOTKA_SLOTS:
        raw = customs.get(slot)
        if not raw:
            continue
        head = str(raw).split("/")[0].strip()
        d = _parse_date_ru(head)
        if d:
            out.add(d)
    return out


def find_missing(client: SynergyClient, check_date: date, *, max_pages: int = 80) -> list[dict]:
    """Сделки, где работа уже шла на `check_date`, а запись наработки за
    этот день не внесена. Листаем `/deals` по `-updated-at` с ранним стопом
    (приём как в `collector.orders_by_sort`) — фильтры на корневом списке
    Synergy тихо игнорирует, поэтому отбор только через сортировку."""
    boundary = datetime.now().astimezone() - timedelta(days=SCAN_WINDOW_DAYS)
    out: list[dict] = []
    page = 1
    while page <= max_pages:
        payload = client.get("deals", sort="-updated-at", **{"page[number]": page})
        rows = payload.get("data") or []
        if not rows:
            break
        stop = False
        for row in rows:
            attrs = row.get("attributes") or {}
            updated = _parse_dt(attrs.get("updated-at"))
            if updated and updated < boundary:
                stop = True
                break
            if attrs.get("archived-at") or attrs.get("discarded-at") or attrs.get("finished-at"):
                continue
            customs = attrs.get("customs") or {}
            start_dt = _parse_dt(customs.get(FIELD_START))
            if not start_dt or start_dt.date() > check_date:
                continue  # работа ещё не началась на эту дату
            entries = _entry_dates(customs)
            if not entries:
                continue  # наработку по этой сделке вообще не ведут — не наш случай
            if check_date in entries:
                continue  # день внесён
            out.append({
                "id": str(row["id"]),
                "number": attrs.get("number"),
                "name": attrs.get("name") or "",
                "updated_at": str(attrs.get("updated-at") or ""),
                "balance": customs.get(FIELD_BALANCE),
            })
        if stop:
            break
        page += 1
    return out


def enrich(client: SynergyClient, deal_id: str) -> dict[str, str]:
    """Тип техники и заказчик — списочная ручка `/deals` их не резолвит
    верно (та же грабля, что с `responsible` у orders), читаем по одной.
    Вызывается только для уже отфильтрованных (обычно немногих) сделок."""
    payload = client.get(f"deals/{deal_id}", include="contact,company,transport-type")
    data = payload.get("data") or {}
    rels = data.get("relationships") or {}
    included = {(r["type"], str(r["id"])): r for r in payload.get("included") or []}

    def linked(name: str) -> dict | None:
        ref = (rels.get(name) or {}).get("data")
        if not ref:
            return None
        return included.get((ref["type"], str(ref["id"])))

    customer = ""
    contact = linked("contact")
    if contact:
        a = contact.get("attributes") or {}
        customer = str(a.get("as-string") or a.get("name") or "").strip()
    if not customer:
        company = linked("company")
        if company:
            customer = str((company.get("attributes") or {}).get("name") or "").strip()
    transport = ""
    tt = linked("transport-type")
    if tt:
        transport = str((tt.get("attributes") or {}).get("name") or "").strip()
    return {"customer": customer or "—", "transport": transport or "—"}


def build_report(client: SynergyClient, check_date: date) -> str:
    missing = find_missing(client, check_date)
    header = f"Наработка техники — не внесено за {check_date:%d.%m.%Y}"
    if not missing:
        return f"{header}: все активные сделки заполнены."
    lines = [f"{header}: {len(missing)}", ""]
    for m in missing:
        extra = enrich(client, m["id"])
        try:
            balance_txt = f"{float(m['balance']):,.0f} ₽".replace(",", " ")
        except (TypeError, ValueError):
            balance_txt = "—"
        lines.append(
            f"№{m['number']} {m['name']} — {extra['transport']}, {extra['customer']}, "
            f"остаток {balance_txt}"
        )
    return "\n".join(lines)
