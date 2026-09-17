"""Запись в Synergy: завести заявку по пойманному звонку и описать её.

Единственное место во всём дашборде, которое **меняет** данные в CRM. Всё
остальное только читает, и это сделано нарочно: ошибка чтения даёт неверную
строку на экране, ошибка записи — мусор в рабочей базе отдела продаж.

Поэтому здесь три правила:

1. **Ничего не создаётся без явного `apply=True`.** По умолчанию — сухой
   прогон: показываем, что завели бы, и ничего не пишем.
2. **Повтор не создаёт дубль.** Заведённая заявка запоминается у нас
   (`screens.created_order_id`), а перед созданием проверяется, не появилась
   ли заявка по этому контакту после звонка — в том числе руками менеджера.
3. **Заявка честно помечена.** Название говорит, откуда она взялась, а в
   комментарии стоят цитата из разговора и время звонка — чтобы любой человек
   мог проверить, не выдумал ли её робот.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Поля заявки, те же, что заполняет разбор звонков с общих номеров: так
# пойманная заявка выглядит в CRM ровно как заведённая обычным путём.
FIELD_TRANSPORT = "custom-18621"
FIELD_ADDRESS = "custom-255"
FIELD_TRANSCRIPT = "custom-30599"
FIELD_ROP_RECOMMENDATIONS = "custom-30600"
FIELD_MANAGER_RECOMMENDATIONS = "custom-30601"
FIELD_CALL_SCORE = "custom-30602"
# «Выжимка» — короткий человеческий вывод по звонку: что просил клиент и что с
# этим делать. Владелец завёл поле специально под разбор, и заполнять его надо
# по каждому звонку, который мы отработали.
FIELD_SUMMARY = "custom-30609"


class CrmWriter:
    """Тонкая надстройка над клиентом Synergy: только то, что пишет."""

    def __init__(self, client: Any, *, apply: bool = False) -> None:
        self._client = client
        self._apply = apply

    @property
    def dry_run(self) -> bool:
        return not self._apply

    def create_order(
        self, *, contact_id: str, name: str, stage_id: str | None,
        responsible_id: str | None, customs: dict[str, Any] | None = None,
        comment: str = "",
    ) -> str | None:
        """Завести заявку и вернуть её идентификатор. В сухом прогоне — None."""
        attributes: dict[str, Any] = {"name": name}
        if comment:
            attributes["comment"] = comment
        if customs:
            attributes["customs"] = customs

        relationships: dict[str, Any] = {
            "contact": {"data": {"type": "contacts", "id": str(contact_id)}},
        }
        if stage_id:
            relationships["stage"] = {"data": {"type": "order-stages", "id": str(stage_id)}}
        if responsible_id:
            relationships["responsible"] = {"data": {"type": "users", "id": str(responsible_id)}}

        payload = {"data": {"type": "orders", "attributes": attributes,
                            "relationships": relationships}}
        if self.dry_run:
            logger.info("сухой прогон: завели бы заявку «%s» по контакту %s", name, contact_id)
            return None
        data = self._client.post("orders", payload)
        order_id = str(((data or {}).get("data") or {}).get("id") or "")
        logger.info("заявка %s заведена по контакту %s", order_id or "?", contact_id)
        return order_id or None

    def add_performer(self, order_id: str, user_id: str) -> bool:
        """Добавить соисполнителя — менеджера, который говорил с клиентом.

        Заменить список целиком Synergy не даёт («Complete replacement
        forbidden»), поэтому добавляем участника, а не переписываем связь.
        """
        if self.dry_run or not user_id:
            return False
        try:
            # Уже добавленного участника Synergy добавить второй раз не даёт —
            # отвечает 400. Поэтому сначала смотрим, кто там есть.
            data = self._client.get(f"orders/{order_id}", include="performers")
            present = {str(item.get("id")) for item in data.get("included") or []
                       if item.get("type") == "users"}
            if str(user_id) in present:
                return True
            self._client.post(f"orders/{order_id}/relationships/performers",
                              {"data": [{"type": "users", "id": str(user_id)}]})
            return True
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("соисполнитель у заявки %s не поставлен: %s", order_id, exc)
            return False

    def set_responsible(self, order_id: str, user_id: str) -> bool:
        """Поставить ответственного и убедиться, что он там и остался.

        При создании Synergy назначает ответственным владельца контакта и нашу
        связь игнорирует молча, а правка полей иногда возвращает его обратно.
        Поэтому ставим последним шагом и проверяем результат: иначе пойманная
        заявка достаётся ровно тому менеджеру, который её не завёл.
        """
        if self.dry_run or not user_id:
            return False
        for attempt in (1, 2):
            try:
                self._client.patch(f"orders/{order_id}/relationships/responsible",
                                   {"data": {"type": "users", "id": str(user_id)}})
                data = self._client.get(f"orders/{order_id}", include="responsible")
                actual = [str(item.get("id")) for item in data.get("included") or []
                          if item.get("type") == "users"]
                if str(user_id) in actual:
                    return True
                logger.info("ответственный у %s сбросился, ставлю заново (попытка %s)",
                            order_id, attempt)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("ответственный у заявки %s не поставлен: %s", order_id, exc)
                return False
        return False

    def set_summary(self, order_id: str, text: str) -> bool:
        """Записать «Выжимку» — наш вывод по звонку."""
        if self.dry_run or not text.strip():
            return False
        try:
            self._client.patch(f"orders/{order_id}", {"data": {
                "type": "orders", "id": str(order_id),
                "attributes": {"customs": {FIELD_SUMMARY: text}},
            }})
            return True
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("выжимка у заявки %s не записана: %s", order_id, exc)
            return False

    def post_comment(self, commentable_id: str, text: str, *, kind: str = "Order") -> bool:
        """Комментарий к заявке или контакту. Переводы строк — в <br>."""
        if self.dry_run:
            logger.info("сухой прогон: комментарий к %s %s не отправлен", kind, commentable_id)
            return False
        payload = {"data": {"type": "comments", "attributes": {
            "commentable-id": int(commentable_id),
            "commentable-type": kind,
            "body": text.replace("\n", "<br>"),
        }}}
        try:
            self._client.post("comments", payload)
            return True
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("комментарий к %s %s не ушёл: %s", kind, commentable_id, exc)
            return False


def order_customs(analysis: dict[str, Any], transcript: str, summary: str = "") -> dict[str, Any]:
    """Поля заявки из разбора — те же, что у звонков с общих номеров."""
    customs: dict[str, Any] = {}
    if summary.strip():
        customs[FIELD_SUMMARY] = summary
    if analysis.get("transport_type"):
        customs[FIELD_TRANSPORT] = [analysis["transport_type"]]
    if analysis.get("object_address"):
        customs[FIELD_ADDRESS] = analysis["object_address"]
    if transcript.strip():
        customs[FIELD_TRANSCRIPT] = transcript
    if analysis.get("rop_recommendations"):
        customs[FIELD_ROP_RECOMMENDATIONS] = analysis["rop_recommendations"]
    if analysis.get("manager_recommendations"):
        customs[FIELD_MANAGER_RECOMMENDATIONS] = analysis["manager_recommendations"]
    if analysis.get("call_score") is not None:
        customs[FIELD_CALL_SCORE] = analysis["call_score"]
    return customs


def lead_summary(call: dict[str, Any], screen: dict[str, Any], analysis: dict[str, Any]) -> str:
    """«Выжимка»: что просил клиент и что с этим делать — в несколько строк.

    Комментарий объясняет происхождение заявки, а выжимка отвечает на вопрос
    «что тут по делу»: её читают, когда разбирают очередь заявок.
    """
    lines = []
    request = screen.get("request") or analysis.get("summary") or ""
    if request:
        lines.append(f"Запрос: {request}")
    if analysis.get("transport_type"):
        lines.append(f"Техника: {analysis['transport_type']}")
    if analysis.get("object_address"):
        lines.append(f"Объект: {analysis['object_address']}")
    if analysis.get("client_price") is not None:
        lines.append(f"Цена клиента: {analysis['client_price']}")
    if analysis.get("our_price") is not None:
        lines.append(f"Назвали клиенту: {analysis['our_price']}")
    lines.append(
        f"Источник: входящий звонок {(call.get('started_at') or '')[:16].replace('T', ' ')}, "
        f"принял {call.get('display_name') or call.get('vats_login') or 'неизвестно'}; "
        "заявка в CRM не была заведена."
    )
    if analysis.get("manager_recommendations"):
        lines.append(f"Что сделать: {analysis['manager_recommendations']}")
    return "\n".join(lines)


def lead_comment(call: dict[str, Any], screen: dict[str, Any], analysis: dict[str, Any]) -> str:
    """Комментарий к заявке: откуда она взялась и на чём основана.

    Человек, открывший заявку, должен за пять секунд понять: кто звонил, когда,
    кому, что просил — и увидеть цитату, по которой это решено.
    """
    when = (call.get("started_at") or "")[:16].replace("T", " ")
    lines = [
        "Заявка заведена разбором записи входящего звонка — менеджер её не оформил.",
        f"Звонок: {when}, {call.get('duration_sec', 0)} с, принял "
        f"{call.get('display_name') or call.get('vats_login') or 'неизвестно'}.",
        f"Клиент: {call.get('client_phone') or ''}.",
    ]
    if screen.get("request"):
        lines.append(f"Просьба клиента: {screen['request']}")
    if screen.get("quote"):
        lines.append(f"Цитата из записи: «{screen['quote']}»")
    if analysis.get("summary"):
        lines.append("")
        lines.append(analysis["summary"])
    return "\n".join(lines)
