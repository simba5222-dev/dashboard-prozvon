"""Клиент API Авито (только чтение) — звонки по объявлениям и чаты.

Описание методов и оговорки — `docs/avito-api.md`. Доступ — client_credentials по ключам профиля «СК Техноресурс»
(`DASH_AVITO_CLIENT_ID`, `DASH_AVITO_CLIENT_SECRET`, `DASH_AVITO_USER_ID` в .env).

Ограничение частоты у Авито жёсткое: на звонках 429 прилетал уже на втором-третьем запросе подряд (17.11.2026).
Поэтому между запросами пауза `min_interval_sec`, а на 429 ждём и повторяем (уважаем Retry-After, иначе 30/60/120 с).
Пишущих методов (отправка сообщений, «прочитано») здесь нет намеренно — только по отдельному слову владельца.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

logger = logging.getLogger(__name__)
BASE = "https://api.avito.ru"
PAGE = 100  # максимум звонков за запрос
MAX_WINDOW_DAYS = 30  # API разрешает до 3 месяцев; берём месяц, чтобы окно не упиралось в предел


class AvitoError(RuntimeError):
    pass


class AvitoClient:
    def __init__(self, client_id: str, client_secret: str, user_id: str | None = None, *,
                 min_interval_sec: float = 2.0, timeout_sec: float = 60.0) -> None:
        if not client_id or not client_secret:
            raise AvitoError("не заданы DASH_AVITO_CLIENT_ID / DASH_AVITO_CLIENT_SECRET")
        self._id, self._secret, self.user_id = client_id, client_secret, user_id
        self._pause, self._timeout = min_interval_sec, timeout_sec
        self._token: str | None = None
        self._token_until = 0.0
        self._last = 0.0

    # ---- токен: живёт сутки, берём заново только перед истечением ----
    def _auth(self) -> str:
        if self._token and time.time() < self._token_until - 300:
            return self._token
        r = httpx.post(f"{BASE}/token", data={"grant_type": "client_credentials", "client_id": self._id,
                                              "client_secret": self._secret}, timeout=self._timeout)
        if r.status_code != 200:
            raise AvitoError(f"токен не выдан: HTTP {r.status_code} {r.text[:120]}")
        data = r.json()
        self._token = data["access_token"]
        self._token_until = time.time() + int(data.get("expires_in") or 86400)
        return self._token

    def _request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None, server_retries: int = 4) -> Any:
        waits = (30, 60, 120, 180)
        for attempt in range(len(waits) + 1):
            gap = self._pause - (time.time() - self._last)
            if gap > 0:
                time.sleep(gap)
            r = httpx.request(method, BASE + path, json=json_body, params=params, timeout=self._timeout,
                              headers={"Authorization": f"Bearer {self._auth()}"})
            self._last = time.time()
            if r.status_code == 429 and attempt < len(waits):
                wait = int(r.headers.get("Retry-After") or waits[attempt])
                logger.info("Авито 429 на %s, жду %d с", path, wait)
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < server_retries:
                # сбой на стороне Авито (шлюз не успел ответить): повторяем с короткой паузой (10/20/40/60 с)
                wait = (10, 20, 40, 60)[attempt]
                logger.info("Авито %d на %s, повторяю через %d с", r.status_code, path, wait)
                time.sleep(wait)
                continue
            if r.status_code == 401 and attempt < len(waits):
                self._token = None   # токен мог протухнуть раньше срока
                continue
            if r.status_code >= 400:
                raise AvitoError(f"{method} {path}: HTTP {r.status_code} {r.text[:160]}")
            return r.json() if r.content else {}
        raise AvitoError(f"{method} {path}: не удалось из-за ограничения частоты")

    # ---- методы (только чтение) ----
    def self_account(self) -> dict:
        return self._request("GET", "/core/v1/accounts/self")

    def balance(self) -> dict:
        return self._request("GET", f"/core/v1/accounts/{self.user_id}/balance/")

    def calls(self, start: datetime, end: datetime) -> Iterator[dict]:
        """Звонки за период (UTC). Окна по MAX_WINDOW_DAYS, внутри окна — постранично по 100."""
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(days=MAX_WINDOW_DAYS), end)
            offset = 0
            while True:
                data = self._request("POST", "/calltracking/v1/getCalls/", json_body={
                    "dateTimeFrom": cur.strftime("%Y-%m-%dT%H:%M:%SZ"), "dateTimeTo": nxt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": PAGE, "offset": offset})
                err = (data.get("error") or {})
                if err.get("code"):
                    raise AvitoError(f"getCalls: {err}")
                rows = data.get("calls") or []
                yield from rows
                if len(rows) < PAGE:
                    break
                offset += PAGE
            cur = nxt

    def items(self, status: str | None = None, per_page: int = 100, page: int = 1) -> dict:
        """Список объявлений профиля (название, цена, статус, ссылка)."""
        params: dict = {"per_page": per_page, "page": page}
        if status:
            params["status"] = status
        return self._request("GET", "/core/v1/items", params=params)

    def item(self, item_id: int) -> dict:
        return self._request("GET", f"/core/v1/accounts/{self.user_id}/items/{item_id}/")

    def stats_items(self, date_from: str, date_to: str, metrics: list[str], grouping: str = "item",
                    limit: int = 1000, offset: int = 0) -> dict:
        """Статистика по объявлениям (просмотры, контакты и т. п.) за период, группировка item/day/totals."""
        return self._request("POST", f"/stats/v2/accounts/{self.user_id}/items", json_body={
            "dateFrom": date_from, "dateTo": date_to, "grouping": grouping, "metrics": metrics, "limit": limit, "offset": offset})

    def spendings(self, date_from: str, date_to: str, grouping: str = "month", item_ids: list[int] | None = None,
                  types: tuple[str, ...] = ("all", "promotion", "presence", "commission", "rest")) -> dict:
        """Расходы профиля, руб. Не чаще одного запроса в минуту; глубина данных 270 дней."""
        body: dict = {"dateFrom": date_from, "dateTo": date_to, "grouping": grouping, "spendingTypes": list(types)}
        if item_ids:
            body["filter"] = {"itemIDs": item_ids}
        return self._request("POST", f"/stats/v2/accounts/{self.user_id}/spendings", json_body=body)

    def calls_stats(self, date_from: str, date_to: str, item_ids: list[int] | None = None) -> dict:
        body: dict = {"dateFrom": date_from, "dateTo": date_to}
        if item_ids:
            body["itemIds"] = item_ids
        return self._request("POST", f"/core/v1/accounts/{self.user_id}/calls/stats/", json_body=body)

    def subscriptions(self) -> dict:
        """Какие вебхуки уже подписаны на приложение (чтение; пишущие подписка/отписка — отдельно, только по слову владельца)."""
        return self._request("POST", "/messenger/v1/subscriptions")

    def messages(self, chat_id: str, limit: int = 100, offset: int = 0) -> dict:
        """Сообщения чата (v3), от новых к старым."""
        # один повтор при ошибке сервера: часть чатов стабильно отвечает 500, долго ждать на каждом не нужно
        return self._request("GET", f"/messenger/v3/accounts/{self.user_id}/chats/{chat_id}/messages/",
                             params={"limit": limit, "offset": offset}, server_retries=1)

    def chats(self, limit: int = 100, offset: int = 0, unread_only: bool = False, item_ids: list[int] | None = None) -> dict:
        """Чаты по объявлениям (u2i), от свежих к старым. Глубина списка — смещение не больше 1000 (≈1 100 чатов);
        чтобы достать более старые, запрашиваем по объявлениям (`item_ids`)."""
        params: dict = {"limit": limit, "offset": offset, "unread_only": str(unread_only).lower(), "chat_types": "u2i"}
        if item_ids:
            params["item_ids"] = ",".join(str(i) for i in item_ids)
        return self._request("GET", f"/messenger/v2/accounts/{self.user_id}/chats", params=params)


def to_msk_date(iso_time: str) -> str:
    msk = timezone(timedelta(hours=3))
    return datetime.fromisoformat(iso_time.replace("Z", "+00:00")).astimezone(msk).date().isoformat()
