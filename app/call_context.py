"""Кто кому звонил и зачем — по метаданным, до всякой модели.

Владелец назвал это главной ошибкой прошедшего периода: сервис читал
расшифровку и **угадывал** роли и повод звонка, хотя то и другое уже лежит
у нас в базе. Угадывание ошибается там, где ошибаться нельзя: слова клиента
приписываются менеджеру, звонок по действующей заявке считается новой
заявкой, перезвон исполнителя — обращением заказчика.

Поэтому контекст считается здесь, из фактов, и дальше передаётся модели как
данность, а не как загадка:

    роли дорожек   — левый канал оператор, правый клиент (проверено, см. ниже)
    направление    — кто набрал номер
    линия          — на какой номер звонили: прямой менеджера или общий
    собеседник     — заказчик, исполнитель, оба, или незнакомый номер
    перезвон       — звонил ли этому номеру кто-то из наших в последние сутки
    сценарий       — что из этого следует

**Про роли дорожек.** Считалось, что у входящих каналы разложены как попало,
и `SCREEN_PROMPT` честно просил модель определить роли самой. 28.09.2026
проверили по звуку на ста записях: кто заговорил первым, тот и снял трубку.
У исходящих первым звучит правый канал в 44 случаях из 50 — трубку берёт
клиент. У входящих первым звучит левый в 39 из 50 — трубку берёт наш
менеджер. То есть **левый канал — наш, правый — собеседник, в обе стороны**.
Это не догадка, а измерение; при смене телефонии проверить заново.

Остающийся разнобой (шесть и одиннадцать записей) — это перебитые начала и
записи, где первым звучит шум линии. На таких сценарий всё равно верен:
он считается не по звуку, а по метаданным звонка.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Сколько часов назад наш исходящий считается поводом для перезвона. Сутки:
# исполнитель, которому звонили вчера вечером, перезванивает утром, и это
# тот же разговор, а не новое обращение.
CALLBACK_WINDOW_HOURS = 24

LEFT_ROLE = "operator"
RIGHT_ROLE = "client"


def digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))[-10:]


@dataclass
class CallContext:
    """Что известно о звонке до того, как кто-либо его послушал."""

    direction: str = "in"
    line: str = "неизвестная"            # прямой | общий | неизвестная
    manager: str = ""
    manager_dept: str = ""
    counterpart: str = "незнакомый"      # заказчик | исполнитель | оба | незнакомый
    counterpart_name: str = ""
    open_orders: int = 0
    transport_cards: int = 0
    called_back: bool = False            # мы сами звонили ему в последние сутки
    scenario: str = "неясный"
    roles: dict[str, str] = field(default_factory=lambda: {"left": LEFT_ROLE, "right": RIGHT_ROLE})

    def as_dict(self) -> dict[str, Any]:
        return {
            "направление": "входящий" if self.direction == "in" else "исходящий",
            "линия": self.line,
            "менеджер": self.manager,
            "отдел": self.manager_dept,
            "собеседник": self.counterpart,
            "заявок у него": self.open_orders,
            "карточек техники": self.transport_cards,
            "перезвон на наш звонок": self.called_back,
            "сценарий": self.scenario,
        }

    def hint(self) -> str:
        """Человеческая фраза для задания модели.

        Пишем как факт и сразу говорим, что из него следует: модель не должна
        выводить смысл сценария заново на каждом звонке.
        """
        return SCENARIOS.get(self.scenario, SCENARIOS["неясный"]).format(
            менеджер=self.manager or "менеджер",
            собеседник=self.counterpart_name or "собеседник",
            заявок=self.open_orders,
        )


# Что означает каждый сценарий — словами, которые уйдут в задание модели.
# Формулировки владельца, 28.09.2026, почти дословно: это его правила, а не
# наша реконструкция.
SCENARIOS: dict[str, str] = {
    "входящий_на_общий":
        "Входящий на общий (многоканальный или рекламный) номер. На такие номера "
        "звонят по объявлению, то есть это почти всегда **новое обращение за "
        "техникой**. Исключение одно — спам и ошиблись номером.",
    "входящий_от_заказчика":
        "Входящий на прямой номер менеджера {менеджер} от заказчика, с которым мы "
        "уже работали (заявок у него: {заявок}). Это может быть как новая просьба "
        "о технике, так и разговор по действующей заявке — различай по содержанию. "
        "Новую просьбу старого клиента пропустить нельзя.",
    "входящий_от_исполнителя":
        "Входящий на прямой номер менеджера {менеджер} от **исполнителя** — это тот, "
        "у кого мы сами арендуем технику. Заявки от него не бывает: он не заказчик. "
        "Обычно разговор либо по действующей заявке, либо он перезванивает узнать, "
        "зачем ему звонили.",
    "входящий_перезвон_исполнителя":
        "Входящий от **исполнителя**, которому мы сами звонили меньше суток назад. "
        "Почти наверняка он перезванивает узнать, зачем его искали, — либо отвечает "
        "по подбору техники. Это **не новое обращение** и не заявка.",
    "входящий_перезвон":
        "Входящий с номера, которому мы сами звонили меньше суток назад. Скорее "
        "всего это ответ на наш звонок, а не самостоятельное обращение.",
    "исходящий_подбор":
        "Исходящий звонок менеджера по поиску техники. Он обзванивает базу "
        "исполнителей и спрашивает свободные машины — значит, работает **по уже "
        "созданной заявке**, а не ищет нового клиента. Здесь важно, какую технику "
        "собеседник называет сверх той, о которой спрашивали.",
    "исходящий_прозвон":
        "Исходящий звонок тёплого прозвона: менеджер {менеджер} обзванивает тех, кто "
        "обращался раньше, и выясняет потребность.",
    "исходящий_исполнителю":
        "Исходящий звонок **исполнителю** — тому, у кого мы арендуем технику. "
        "Заявки здесь не возникает: мы ищем машину под свою заявку.",
    "исходящий_заказчику":
        "Исходящий звонок заказчику (заявок у него: {заявок}). Это работа по "
        "клиенту, а не поиск техники.",
    "неясный":
        "Повод звонка по метаданным не определён: номер незнакомый. Разбирай по "
        "содержанию и не приписывай ролей, которых в разговоре не слышно.",
}


def _number_profile(conn: sqlite3.Connection, phone10: str) -> dict[str, Any]:
    """Что местный справочник знает про номер. Пусто — номер незнакомый."""
    if not phone10:
        return {}
    row = conn.execute(
        "SELECT name, category, cards, orders FROM numbers WHERE phone10 = ?", (phone10,)
    ).fetchone()
    return dict(row) if row else {}


def _called_recently(conn: sqlite3.Connection, phone10: str, before: str) -> bool:
    """Звонил ли кто-то из наших на этот номер в последние сутки до этого звонка.

    Нужно ради одного правила владельца: исполнитель, которому мы звонили,
    перезванивает узнать зачем. Без этого его перезвон читается как
    самостоятельное обращение, и разбор придумывает несуществующую заявку.
    """
    if not (phone10 and before):
        return False
    try:
        moment = datetime.fromisoformat(str(before).replace("Z", "+00:00"))
    except ValueError:
        return False
    since = (moment - timedelta(hours=CALLBACK_WINDOW_HOURS)).isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM calls
        WHERE direction = 'out' AND started_at BETWEEN ? AND ?
          AND REPLACE(REPLACE(REPLACE(client_phone,'+',''),' ',''),'-','') LIKE ?
        LIMIT 1
        """,
        (since, before, f"%{phone10}"),
    ).fetchone()
    return row is not None


def _line_kind(conn: sqlite3.Connection, diversion: str, direction: str) -> str:
    """Прямой номер менеджера или общий. У исходящих линия смысла не несёт."""
    if direction != "in":
        return "исходящая"
    number = digits(diversion)
    if not number:
        return "неизвестная"
    row = conn.execute(
        """SELECT 1 FROM managers WHERE phone <> ''
           AND REPLACE(REPLACE(REPLACE(phone,'+',''),' ',''),'-','') LIKE ? LIMIT 1""",
        (f"%{number}",),
    ).fetchone()
    return "прямой" if row else "общий"


def _pick_scenario(ctx: CallContext) -> str:
    if ctx.direction == "in":
        if ctx.line == "общий":
            return "входящий_на_общий"
        if ctx.counterpart == "исполнитель":
            return "входящий_перезвон_исполнителя" if ctx.called_back else "входящий_от_исполнителя"
        if ctx.called_back:
            return "входящий_перезвон"
        if ctx.counterpart in ("заказчик", "оба"):
            return "входящий_от_заказчика"
        return "неясный"
    # Исходящие: отдел менеджера говорит о поводе больше, чем номер собеседника.
    if ctx.manager_dept == "поиск":
        return "исходящий_подбор"
    if ctx.counterpart == "исполнитель":
        return "исходящий_исполнителю"
    if ctx.manager_dept == "прозвон":
        return "исходящий_прозвон"
    if ctx.counterpart in ("заказчик", "оба"):
        return "исходящий_заказчику"
    return "неясный"


def build(conn: sqlite3.Connection, call: dict[str, Any] | sqlite3.Row) -> CallContext:
    """Собрать контекст звонка. Ничего не спрашивает у CRM и у модели."""
    call = dict(call)
    phone = digits(call.get("client_phone"))
    ctx = CallContext(direction=str(call.get("direction") or "in"))
    ctx.line = _line_kind(conn, call.get("diversion") or "", ctx.direction)

    login = str(call.get("vats_login") or "")
    row = conn.execute(
        "SELECT display_name, dept FROM managers WHERE vats_login = ?", (login,)
    ).fetchone()
    if row:
        ctx.manager = row["display_name"] or login
        ctx.manager_dept = row["dept"] or ""
    else:
        ctx.manager = login

    profile = _number_profile(conn, phone)
    if profile:
        ctx.counterpart = str(profile.get("category") or "незнакомый")
        ctx.counterpart_name = str(profile.get("name") or "")
        ctx.open_orders = int(profile.get("orders") or 0)
        ctx.transport_cards = int(profile.get("cards") or 0)

    ctx.called_back = _called_recently(conn, phone, str(call.get("started_at") or ""))
    ctx.scenario = _pick_scenario(ctx)
    return ctx


def describe(ctx: CallContext) -> str:
    """Блок для задания модели: факты, роли дорожек и что из них следует."""
    кто = ("Левая дорожка (operator) — наш менеджер, правая (client) — собеседник. "
           "Это известно из устройства записи, определять по содержанию не нужно.")
    строки = [
        кто,
        "",
        f"Направление: {'входящий' if ctx.direction == 'in' else 'исходящий'}"
        + (f", на {ctx.line} номер" if ctx.direction == "in" and ctx.line != "неизвестная" else ""),
    ]
    if ctx.manager:
        строки.append(f"Наш сотрудник: {ctx.manager}"
                      + (f", отдел «{ctx.manager_dept}»" if ctx.manager_dept else ""))
    if ctx.counterpart != "незнакомый":
        сведения = f"Собеседник по нашей базе: {ctx.counterpart}"
        if ctx.counterpart_name:
            сведения += f" ({ctx.counterpart_name})"
        хвост = []
        if ctx.open_orders:
            хвост.append(f"заявок {ctx.open_orders}")
        if ctx.transport_cards:
            хвост.append(f"карточек техники {ctx.transport_cards}")
        if хвост:
            сведения += ", " + ", ".join(хвост)
        строки.append(сведения)
    else:
        строки.append("Собеседник в нашей базе не найден: номер незнакомый.")
    if ctx.called_back:
        строки.append("Мы сами звонили на этот номер меньше суток назад.")
    строки += ["", f"ПОВОД ЗВОНКА: {ctx.hint()}"]
    return "\n".join(строки)
