"""Дашборд прозвона: экраны и служебные ручки.

Доступ закрыт basic-авторизацией на уровне nginx — дашборд показывает работу
конкретных людей. Отдельных учёток внутри приложения пока нет: когда менеджерам
понадобится видеть свою статистику, это надо будет делать по-настоящему, а не
раздачей общего пароля.
"""

from __future__ import annotations

import re
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import Settings, get_settings
from app.db import (
    CARD_FIELDS, approve_screen, connect, dismiss_inbound, has_any_data, init_schema,
    judge_screen, save_ad_verdict, save_order_comment, save_training_correction,
    training_rows,
)
from fastapi.responses import FileResponse, RedirectResponse

from app.stats import _parsed_analysis as parsed_analysis  # noqa: E402
from app.transport_report import added_cards as transport_added_cards  # noqa: E402
from app.stats import (  # noqa: F401
    search_day, search_feed, search_tasks, search_by_type, heard_checks, heard_summary,
    heard_problems, search_cold_suppliers,
    sales_search_rows, sales_search_by_order, sales_search_by_manager,
    ai_transport_rows, link_comparison,
    call_detail,
    calls_of_day,
    day_summary,
    inbound_rows,
    inbound_totals,
    local_now,
    managers,
    order_detail,
    caught_rows,
    trial_rows,
    orders_of_period,
    period_summary,
    report_rows,
    report_totals,
    digest_rows,
    coaching_picks,
    messenger_sends_by_day,
    messenger_sends_overview,
)

logger = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    conn = connect(settings.db_path)
    init_schema(conn)
    app.state.settings = settings
    app.state.db = conn

    if settings.demo_mode and not has_any_data(conn):
        from app.demo import seed

        n = seed(conn, offset_hours=settings.timezone_offset_hours)
        logger.warning("демо-режим: создано %s показательных звонков", n)

    if not settings.vats_configured:
        logger.warning("ВАТС не настроена: звонки собирать неоткуда")
    if not settings.synergy_configured:
        logger.warning("Synergy не настроена: карточки проверять нечем")
    logger.info("дашборд запущен")
    yield
    conn.close()


app = FastAPI(title="Дашборд прозвона", version=__version__, lifespan=lifespan)

from app.payments_ui import router as payments_router  # noqa: E402  страница «Финансы → Платежи» (разнос платежей по сделкам)
app.include_router(payments_router)


def as_int(value: Any, default: int = 0) -> int:
    """Число из параметра адреса.

    Форма отчёта отправляет все свои поля, даже пустые: «дольше, с» без
    значения приезжает как `min_sec=`. Строгий разбор отвечал на это 422, и
    отбор по датам «не работал» — на самом деле падала вся страница.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_date(value: Any) -> str:
    """Дата из параметра адреса или пустая строка, если её нет или она кривая."""
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _with_contact_links(rows: list[dict[str, Any]], url_template: str) -> list[dict[str, Any]]:
    """Дописать ссылку на карточку контакта в Synergy — решение владельца 05.10.2026.

    Формат ссылки в `.env` (`SYNERGY_CONTACT_URL`), не в коде: угадывать чужой
    URL нельзя, а владелец однажды его сменит. Без настройки или без
    `contact_id` у звонка ссылки просто не будет — имя останется текстом.
    """
    if url_template:
        for row in rows:
            contact_id = row.get("contact_id")
            row["contact_url"] = url_template.format(id=contact_id) if contact_id else ""
    return rows


def _base_context(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return {
        "request": request,
        "version": __version__,
        # Снаружи дашборд живёт под /dashboard/, а nginx отдаёт приложению путь
        # уже без этой приставки. Ссылки в шаблонах строятся от неё, иначе
        # переход внутрь страницы уводит мимо дашборда и получается 404.
        "base": request.headers.get("x-forwarded-prefix", "").rstrip("/"),
        "settings": settings,
        "demo": settings.demo_mode,
        "sources_ready": settings.vats_configured and settings.synergy_configured,
        # Личный вход менеджера — 05.10.2026: отдельный nginx-location с его
        # собственным паролем, видит только свою страницу. Заголовок ставит
        # только этот location, поэтому шаблоны по нему прячут ссылки на
        # остальной дашборд — туда у менеджера всё равно нет пароля.
        "manager_view": request.headers.get("x-dash-scope") == "manager",
    }


@app.post("/api/inbound-call")
async def inbound_call(
    background: BackgroundTasks,
    call: str = Form(...),
    record: UploadFile = File(...),
    request: Request = None,  # type: ignore[assignment]
) -> Any:
    """Звонок из ВАТС: метаданные и запись, присланные российским сервером.

    ВАТС не пускает зарубежные адреса, поэтому вебхук и запись достаются
    российскому серверу, а он пересылает их сюда — в секунду окончания
    разговора. До этого звонки приходили опросом CRM раз в десять минут,
    и у части из них CRM отдавала нулевую длительность: разговор на три
    минуты выглядел сброшенным и в просев не попадал.

    Отвечаем сразу, работу делаем в фоне: распознавание занимает минуту,
    столько держать чужой запрос нельзя.
    """
    settings: Settings = request.app.state.settings
    expected = settings.inbound_hook_token
    if not expected:
        return JSONResponse({"error": "приёмник выключен"}, status_code=503)
    if request.headers.get("X-Vats-Token", "") != expected:
        logger.warning("приёмник звонков: неверный ключ")
        return JSONResponse({"error": "неверный ключ"}, status_code=403)

    try:
        meta = json.loads(call)
    except ValueError:
        return JSONResponse({"error": "метаданные не разобрались"}, status_code=400)
    audio = await record.read()
    if not audio:
        return JSONResponse({"error": "пустая запись"}, status_code=400)

    background.add_task(_handle_inbound, request.app, meta, audio)
    return {"accepted": meta.get("uid")}


@app.post("/api/call-context")
async def call_context_api(request: Request) -> Any:
    """Повод звонка по метаданным — для тех, у кого нет нашей базы.

    Правила «кто кому звонил и что из этого следует» живут в одном месте —
    `app/call_context.py`. Российскому серверу они нужны для разбора звонков
    с рекламных линий, но справочников линий, номеров и менеджеров у него
    нет: они собираются здесь. Поэтому он спрашивает готовый блок, а не
    заводит вторую копию правил — две копии разъезжаются молча.

    Ключ тот же, что у приёмника звонков: стучится тот же сервер.
    """
    settings: Settings = request.app.state.settings
    expected = settings.inbound_hook_token
    if not expected:
        return JSONResponse({"error": "приёмник выключен"}, status_code=503)
    if request.headers.get("X-Vats-Token", "") != expected:
        logger.warning("контекст звонка: неверный ключ")
        return JSONResponse({"error": "неверный ключ"}, status_code=403)

    call = await request.json()
    if not isinstance(call, dict):
        return JSONResponse({"error": "ожидается объект"}, status_code=400)

    from app import call_context

    conn = request.app.state.db
    ctx = call_context.build(conn, {
        "client_phone": call.get("client_phone") or call.get("phone") or "",
        "direction": call.get("direction") or ("in" if call.get("type") == "in" else "out"),
        "diversion": call.get("diversion") or "",
        "vats_login": call.get("vats_login") or call.get("user") or "",
        "started_at": call.get("started_at") or call.get("start") or "",
    })
    return {"context": call_context.describe(ctx), "scenario": ctx.scenario,
            "facts": ctx.as_dict()}


@app.post("/api/line-in-scope")
async def line_in_scope(request: Request) -> Any:
    """Разбираем ли звонки на эту линию. Спрашивает питерский сервер.

    Он решает это **до** скачивания записи, поэтому ручка должна быть дешёвой:
    один запрос к справочнику линий, никакой модели и никакой CRM.

    Ответ из трёх значений, и третье здесь главное:

    - `разбирать` — это общий номер компании из утверждённого списка;
    - `не разбирать` — линия убрана владельцем, либо это прямой номер
      сотрудника: такие звонки живут в потоке 2, а не в первом;
    - `не знаю` — номера нет в справочнике или ВАТС не дала ему имени.
      Тогда спрашивающий решает сам, как раньше.

    Третье значение нужно ради живого случая: номер +7 921 565-32-76 даёт
    два десятка входящих, CRM считает их рекламными, а имени от ВАТС у него
    нет. Отвечать «не разбирать» на такое — значит молча терять заявки.
    """
    settings: Settings = request.app.state.settings
    expected = settings.inbound_hook_token
    if not expected:
        return JSONResponse({"error": "приёмник выключен"}, status_code=503)
    if request.headers.get("X-Vats-Token", "") != expected:
        logger.warning("проверка линии: неверный ключ")
        return JSONResponse({"error": "неверный ключ"}, status_code=403)

    body = await request.json()
    номер = re.sub(r"\D", "", str((body or {}).get("diversion") or ""))[-10:]
    if not номер:
        return {"verdict": "не знаю", "name": "", "note": "номер линии не передан"}

    row = request.app.state.db.execute(
        "SELECT name, kind, in_scope, scope_note, is_general, owner_name "
        "FROM lines WHERE phone10 = ?", (номер,)
    ).fetchone()
    if row is None:
        return {"verdict": "не знаю", "name": "", "note": "линии нет в справочнике"}

    вид = str(row["kind"] or "")
    имя = str(row["owner_name"] or row["name"] or "")
    if not row["in_scope"]:
        вердикт = "не разбирать"
    elif row["is_general"]:
        вердикт = "разбирать"          # слово владельца сильнее данных ВАТС
    elif вид == "рекламная":
        вердикт = "разбирать"
    elif вид == "прямой":
        вердикт = "не разбирать"       # поток 2, его ведёт дашборд
    else:
        вердикт = "не знаю"
    return {"verdict": вердикт, "name": имя, "kind": вид,
            "note": row["scope_note"] or ""}


@app.post("/api/synergy-subor-hook")
async def synergy_subor_hook(background: BackgroundTasks, request: Request) -> Any:
    """Вебхук из Synergy (Автоматизации → Сценарии → «Вызвать вебхук»):
    условие «заявка изменена» — общее, шлёт на любое изменение, не только
    на смену этапа. Поэтому тело запроса используем только чтобы достать id
    заявки, а актуальный этап перепроверяем сами через API (app/subor_hook.py)
    — телу чужого вебхука верить нельзя.

    Секретность — случайный путь в nginx, не токен здесь: в поле URL
    настроек Synergy нет места для заголовков.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    logger.info("вебхук подбора техники: %s", json.dumps(payload)[:500])

    from app import subor_hook

    background.add_task(subor_hook.handle_webhook_sync, payload)
    return {"accepted": True}


@app.post("/api/synergy-deal-company-hook")
async def synergy_deal_company_hook(background: BackgroundTasks, request: Request) -> Any:
    """Вебхук из Synergy (Автоматизации → Сценарии → «Вызвать вебхук»):
    условие «сделка изменена» — общее, не по конкретному полю (ни «Начало
    работ», ни «Транспорт» не выбираются явным триггером в конструкторе
    сценариев — там только кастом-поля самой сделки). Разбирает цепочку
    сделка → транспорт → телефон исполнителя → контакт → его компания и
    пишет её в «Компания И» (custom-98), если она разошлась с текущей —
    см. app/deal_company_sync.py.

    Секретность — случайный путь в nginx, не токен здесь: в поле URL
    настроек Synergy нет места для заголовков (как и у /api/synergy-subor-hook).
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    logger.info("вебхук «Компания И»: %s", json.dumps(payload)[:500])

    from app import deal_company_sync

    background.add_task(deal_company_sync.handle_webhook_sync, payload)
    return {"accepted": True}


def _handle_inbound(app_ref: FastAPI, meta: dict[str, Any], audio: bytes) -> None:
    """Фоновая обработка звонка: своё соединение с базой, свои ошибки."""
    from app import inbound

    settings: Settings = app_ref.state.settings
    conn = connect(settings.db_path)
    try:
        result = inbound.process(conn, settings, meta, audio)
        logger.info("звонок %s: %s", meta.get("uid"), result)
    except Exception:  # noqa: BLE001 — фоновая задача обязана дожить до лога
        logger.exception("звонок %s: обработка упала", meta.get("uid"))
    finally:
        conn.close()



@app.get("/health")
async def health() -> dict[str, Any]:
    settings: Settings = app.state.settings
    return {
        "status": "ok",
        "version": __version__,
        "demo_mode": settings.demo_mode,
        "vats_configured": settings.vats_configured,
        "synergy_configured": settings.synergy_configured,
        "card_fields_configured": settings.card_fields_configured,
        "transcribe_enabled": settings.transcribe_enabled,
    }


@app.get("/", response_class=HTMLResponse)
async def today(request: Request, day: str | None = None) -> Any:
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    current = day or local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")

    rows = day_summary(
        conn, current,
        default_plan=settings.plan_calls_per_day,
        threshold_sec=settings.talk_threshold_sec,
    )
    ctx = _base_context(request)
    ctx.update({
        "day": current,
        "rows": rows,
        "card_fields": CARD_FIELDS,
        "hours": range(settings.workday_start_hour, settings.workday_end_hour + 1),
        "period": period_summary(
            conn, 7,
            default_plan=settings.plan_calls_per_day,
            threshold_sec=settings.talk_threshold_sec,
            today=current,
        ),
    })
    return TEMPLATES.TemplateResponse("today.html", ctx)


@app.get("/manager/{vats_login}", response_class=HTMLResponse)
async def manager_day(request: Request, vats_login: str, day: str | None = None) -> Any:
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    current = day or local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")

    who = next((m for m in managers(conn) if m["vats_login"] == vats_login), None)
    ctx = _base_context(request)
    ctx.update({
        "day": current,
        "manager": who,
        "vats_login": vats_login,
        "calls": calls_of_day(conn, current, vats_login),
        "card_fields": CARD_FIELDS,
        "threshold": settings.talk_threshold_sec,
    })
    return TEMPLATES.TemplateResponse("manager.html", ctx)


@app.get("/messenger-sends", response_class=HTMLResponse)
async def messenger_sends_page(request: Request) -> Any:
    """Все менеджеры с разобранной перепиской — точка входа из общего меню.

    Раньше ссылка была только на персональной странице менеджера, и найти
    отчёт можно было только зная прямой адрес — владелец его не нашёл ни
    на «сегодня», ни в развёрнутом отчёте (12.10.2026). Этот экран и общая
    ссылка в меню — исправление именно этого.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    today = local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    ctx = _base_context(request)
    ctx.update({"rows": messenger_sends_overview(conn, today), "today": today})
    return TEMPLATES.TemplateResponse("messenger_sends_overview.html", ctx)


@app.get("/manager/{vats_login}/messenger-sends", response_class=HTMLResponse)
async def manager_messenger_sends(
    request: Request, vats_login: str, since: str = "", until: str = "",
) -> Any:
    """Отправки презентации/списка техники через личные Telegram/MAX менеджера.

    Не часть звонкового конвейера — разовый разбор переписки владельцем
    (см. scripts/import_messenger_sends_tkachevin.py). Страница просто
    показывает то, что уже разобрано и лежит в messenger_file_sends;
    сама она ничего не досчитывает и не обновляет.

    Фильтр по датам (12.10.2026, по просьбе владельца) — без since/until
    отдаёт весь период разбора: в отличие от /report, «сегодня» здесь почти
    всегда было бы пустым экраном, данные растянуты на месяцы в прошлом.
    Быстрые периоды поэтому отталкиваются не от сегодняшней даты, а от
    последнего дня, где вообще есть данные.
    """
    conn = request.app.state.db
    who = next((m for m in managers(conn) if m["vats_login"] == vats_login), None)
    report = messenger_sends_by_day(conn, vats_login, since or None, until or None)

    quick: list[dict[str, str]] = []
    if report["available_first"] and report["available_last"]:
        last = date.fromisoformat(report["available_last"])
        quick = [
            {"name": "весь период", "since": report["available_first"], "until": report["available_last"]},
            {"name": "неделя", "since": (last - timedelta(days=6)).isoformat(), "until": report["available_last"]},
            {"name": "месяц", "since": (last - timedelta(days=29)).isoformat(), "until": report["available_last"]},
        ]

    base = request.headers.get("x-forwarded-prefix", "").rstrip("/")

    def link(**over: Any) -> str:
        params = {"since": report["since"], "until": report["until"], **over}
        clean = {k: v for k, v in params.items() if v}
        return f"{base}/manager/{vats_login}/messenger-sends?{urlencode(clean)}"

    ctx = _base_context(request)
    ctx.update({
        "manager": who,
        "vats_login": vats_login,
        "report": report,
        "quick": quick,
        "link": link,
    })
    return TEMPLATES.TemplateResponse("messenger_sends.html", ctx)


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, day: str | None = None,
                      since: str = "", until: str = "",
                      who: str | None = None) -> Any:
    """Период отдела поиска техники: звонки поставщикам и правки в карточках.

    Отдельный экран, а не строка в прозвоне: работа другая и меряется другим.
    Прозвон меряется планом звонков и дисциплиной карточки клиента, поиск —
    обзвоном поставщиков и тем, что после него поменялось в транспорте.

    **Фильтр по датам** — решение владельца 05.10.2026: до этого экран
    показывал только сегодня, и посмотреть прошлую неделю было нельзя никак.
    Параметры `since`/`until` и готовые периоды — те же, что в `/report`.
    `day` оставлен для старых ссылок на один день.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    today = local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    until = as_date(until) or as_date(day) or today
    since = as_date(since) or as_date(day) or today
    if since > until:
        since, until = until, since
    ctx = _base_context(request)
    heard = _with_contact_links(heard_checks(conn, since, until), settings.synergy_contact_url)
    ctx.update({
        "since": since,
        "until": until,
        "quick": quick_periods(today),
        "people": search_day(conn, since, until, threshold_sec=settings.talk_threshold_sec),
        "feed": search_feed(conn, since, until, who),
        "by_type": search_by_type(conn, since, until),
        "tasks": search_tasks(conn),
        "heard": heard,
        "heard_total": heard_summary(heard),
        "who": who,
    })
    return TEMPLATES.TemplateResponse("search.html", ctx)


@app.get("/search/sales", response_class=HTMLResponse)
async def search_sales_page(request: Request, since: str = "", until: str = "") -> Any:
    """Поиск техники менеджерами продаж: звонки исполнителям по заявкам.

    Для каждой заявки: сколько звонков сделали, у скольких поставщиков, какие
    цены назвали, занята ли техника и спросил ли менеджер дату освобождения.
    Плюс что ИИ предложил занести в базу «Транспорт» и как наша привязка звонков
    к заявкам сходится с тем, что проставил помощник. Цены исполнителей живут
    только здесь: в поля заявки заказчика их писать нельзя — чужая ставка.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    today = local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    until = as_date(until) or today
    since = as_date(since) or (date.fromisoformat(until) - timedelta(days=6)).isoformat()
    if since > until:
        since, until = until, since
    rows = sales_search_rows(conn, since, until, settings.search_sales_dept)
    ctx = _base_context(request)
    ctx.update({
        "since": since, "until": until, "quick": quick_periods(today),
        "by_order": sales_search_by_order(rows),
        "by_manager": sales_search_by_manager(rows),
        "adds": ai_transport_rows(conn, since, until),
        "compare": link_comparison(conn, since),
        "total_calls": len(rows),
        "transport_mode": settings.transport_ai_mode,
        "link_mode": settings.link_ai_mode,
        "order_url": settings.synergy_url.split("/api/")[0].rstrip("/") + "/orders/",
    })
    return TEMPLATES.TemplateResponse("search_sales.html", ctx)


@app.get("/search/manager/{vats_login}", response_class=HTMLResponse)
async def search_manager_page(request: Request, vats_login: str, day: str | None = None,
                              since: str = "", until: str = "") -> Any:
    """Личная страница поисковика — решение владельца 05.10.2026.

    Чтобы человек мог сам себя контролировать, а не узнавать о проблемах от
    руководителя: та же сверка, что на `/search`, но по одному человеку и с
    отдельной подсветкой того, что ещё не доделано (`heard_problems`).
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    today = local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    until = as_date(until) or as_date(day) or today
    since = as_date(since) or as_date(day) or today
    if since > until:
        since, until = until, since

    who = next((m for m in managers(conn, dept="поиск") if m["vats_login"] == vats_login), None)
    ctx = _base_context(request)
    heard = _with_contact_links(
        heard_checks(conn, since, until, vats_login=vats_login), settings.synergy_contact_url)
    people = search_day(conn, since, until, threshold_sec=settings.talk_threshold_sec)
    ctx.update({
        "since": since,
        "until": until,
        "quick": quick_periods(today),
        "manager": who,
        "vats_login": vats_login,
        "me": next((p for p in people if p["vats_login"] == vats_login), None),
        "by_type": search_by_type(conn, since, until, vats_login=vats_login),
        "feed": search_feed(conn, since, until, vats_login),
        "heard": heard,
        "heard_total": heard_summary(heard),
        "problems": heard_problems(heard),
        "added": transport_added_cards(conn, since, until, who["display_name"] if who else None),
    })
    return TEMPLATES.TemplateResponse("search_manager.html", ctx)


@app.get("/search/plan", response_class=HTMLResponse)
async def search_plan_page(request: Request, per_day: int = 120) -> Any:
    """План обзвона поставщиков, которых ещё ни разу не прозванивали.

    Решение владельца 05.10.2026: 120 звонков в день, уже отработанные
    (у кого хоть одна карточка со статусом) в план не идут. Список живой —
    разбивка по дням пересчитывается из текущего состояния карточек при
    каждом открытии, а не хранится отдельной выгрузкой. Логика отбора —
    `search_cold_suppliers`.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    ctx = _base_context(request)
    per_day = per_day if per_day > 0 else 120
    pool = _with_contact_links(search_cold_suppliers(conn), settings.synergy_contact_url)
    days = [pool[i:i + per_day] for i in range(0, len(pool), per_day)]
    ctx.update({
        "pool_total": len(pool),
        "per_day": per_day,
        "days": days,
    })
    return TEMPLATES.TemplateResponse("search_plan.html", ctx)


def split_turns(transcript: str | None) -> list[tuple[str, str]]:
    """Расшифровку — репликами с именем стороны, а не сплошным полотном.

    Распознавание отдаёт по одной строке на дорожку: `operator: …` и
    `client: …`. Читать это подряд невозможно, а слушая запись, глазами
    ищешь именно «кто что сказал». Поэтому строка режется на предложения
    и показывается с подписью стороны.
    """
    имена = {"operator": "менеджер", "client": "клиент", "speaker": "сторона"}
    куски: list[tuple[str, str]] = []
    for line in (transcript or "").splitlines():
        голова, _, тело = line.partition(":")
        кто = имена.get(голова.strip(), голова.strip() or "сторона")
        тело = тело.strip()
        if тело:
            куски.append((кто, тело))
    return куски


@app.get("/ads", response_class=HTMLResponse)
async def ads(request: Request, since: str = "", until: str = "",
              verdict: str = "", min_sec: str = "") -> Any:
    """Звонки с рекламных линий: разбор и отметки человека.

    Заявку по таким звонкам CRM заводит сама, ещё до всякого разбора. Здесь
    другое — смотрим содержание и помечаем, верно ли разобрано. Отметки и
    есть смысл экрана: из них растёт проверочный набор, без которого смену
    модели нельзя измерить, можно только поверить.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    today = local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    until = as_date(until) or today
    since = as_date(since) or (date.fromisoformat(until) - timedelta(days=6)).isoformat()
    if since > until:
        since, until = until, since
    порог = as_int(min_sec) or settings.inbound_min_duration_sec

    где = ["k.local_date BETWEEN ? AND ?", "k.duration_sec >= ?"]
    параметры: list[Any] = [since, until, порог]
    if verdict == "none":
        где.append("COALESCE(a.verdict, '') = ''")
    elif verdict:
        где.append("a.verdict = ?")
        параметры.append(verdict)

    rows = [dict(r) for r in conn.execute(f"""
        SELECT a.call_uid, a.line, a.transcript, a.analysis_json, a.verdict,
               a.verdict_note, a.verdict_at, k.started_at, k.duration_sec, k.client_phone
        FROM ad_calls a JOIN calls k ON k.uid = a.call_uid
        WHERE {' AND '.join(где)}
        ORDER BY k.started_at DESC
    """, параметры)]
    for row in rows:
        row["analysis"] = parsed_analysis(row.get("analysis_json"))
        row["turns"] = split_turns(row.get("transcript"))

    оценки = [r["analysis"]["quality"] for r in rows
              if (r["analysis"] or {}).get("quality") not in (None, "")]
    totals = {
        "all": len(rows),
        "requests": sum(1 for r in rows if (r["analysis"] or {}).get("is_request")),
        "equipment": sum(1 for r in rows if (r["analysis"] or {}).get("equipment")),
        "avg_quality": f"{sum(оценки) / len(оценки):.1f}".replace(".", ",") if оценки else "—",
        "right": sum(1 for r in rows if r["verdict"] == "верно"),
        "wrong": sum(1 for r in rows if r["verdict"] == "неверно"),
        "unmarked": sum(1 for r in rows if not r["verdict"]),
    }
    ctx = _base_context(request)
    ctx.update({"rows": rows, "totals": totals, "since": since, "until": until,
                "verdict": verdict, "min_sec": порог,
                "back": f"{ctx['base']}/ads?since={since}&until={until}"
                        + (f"&verdict={verdict}" if verdict else "")})
    return TEMPLATES.TemplateResponse("ads.html", ctx)


@app.post("/ads/mark")
async def ads_mark(request: Request, call_uid: str = Form(...), verdict: str = Form(""),
                   note: str = Form(""), back: str = Form("")) -> Any:
    """Отметка человека по одному разбору. Сам разбор не трогаем."""
    conn = request.app.state.db
    save_ad_verdict(conn, call_uid, verdict.strip(), note.strip(),
                    datetime.now(timezone.utc).isoformat())
    conn.commit()
    ctx = _base_context(request)
    адрес = back or f"{ctx['base']}/ads"
    return RedirectResponse(f"{адрес}#c{call_uid}", status_code=303)


@app.get("/digest", response_class=HTMLResponse)
async def digest_page(
    request: Request, since: str = "", until: str = "", manager: str = "",
) -> Any:
    """Выжимка для планёрки — решение владельца 05.10.2026.

    Не новый разбор, а подсветка того, что уже посчитал почасовой разбор
    звонков: где менеджер не дожал разговор, где заявка зависла без движения,
    где был разговор про технику, а заявки нет вовсе, и три-пять самых
    поучительных звонков на человека — обсуждать на планёрке.

    «Заявки без движения» — отдельное окно (45 дней), не зависящее от
    выбранного периода звонков: зависшая заявка интересна сама по себе, а не
    потому, что по ней вчера звонили.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    today = local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    until = as_date(until) or today
    since = as_date(since) or until
    if since > until:
        since, until = until, since

    who = next((m for m in managers(conn) if m["vats_login"] == manager), None)
    digest = digest_rows(conn, since, until, manager or None, settings.talk_threshold_sec)

    stale_since = (date.fromisoformat(today) - timedelta(days=45)).isoformat()
    stale_orders = orders_of_period(conn, stale_since, today, kind="rop")
    if who:
        stale_orders = [o for o in stale_orders if o.get("responsible") == who["display_name"]]

    ctx = _base_context(request)
    ctx.update({
        "since": since,
        "until": until,
        "quick": quick_periods(today),
        "manager_login": manager,
        "manager": who,
        "all_managers": managers(conn),
        "underdelivered": digest["underdelivered"],
        "talked_no_order": digest["talked_no_order"],
        "stale_orders": stale_orders,
        "stale_since": stale_since,
        "coaching": coaching_picks(digest["rows"]),
        "total_calls": len(digest["rows"]),
    })
    return TEMPLATES.TemplateResponse("digest.html", ctx)


@app.get("/report", response_class=HTMLResponse)
async def report(
    request: Request, since: str = "", until: str = "",
    day: str | None = None, days: str = "", manager: str | None = None,
    need: str = "", objects: str = "", inn: str = "", task: str = "",
    contact: str = "", company: str = "", orders: str = "",
    transcript: str = "", missed: str = "", q: str = "", min_sec: str = "",
) -> Any:
    """Развёрнутая таблица: что произошло по каждому разговору.

    Период задаётся датами «с» и «по». Старый вид ссылки (`day` + `days`)
    понимаем по-прежнему: на него ведут ссылки с других экранов.

    Каждая колонка фильтруется отдельно: «покажи разговоры без записанной
    потребности», «где поставлена задача», «где разбор нашёл упущенное».
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    today = local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    until = as_date(until) or as_date(day) or today
    since = as_date(since) or (
        date.fromisoformat(until) - timedelta(days=max(as_int(days), 1) - 1)
    ).isoformat()
    if since > until:
        since, until = until, since

    filters = {
        "need": need, "objects": objects, "inn": inn, "task": task,
        "contact": contact, "company": company, "orders": orders,
        "transcript": transcript, "missed": missed, "q": q,
        "min_sec": as_int(min_sec),
    }
    rows = report_rows(conn, since, until, manager, settings.talk_threshold_sec, filters)
    ctx = _base_context(request)
    ctx.update({
        "since": since,
        "until": until,
        "quick": quick_periods(today),
        "manager_login": manager,
        "manager": next((m for m in managers(conn) if m["vats_login"] == manager), None),
        "all_managers": managers(conn),
        "rows": rows,
        "totals": report_totals(rows),
        "filters": filters,
        "filters_on": any(value not in ("", 0, None) for value in filters.values()),
        "link": report_link(ctx["base"], since, until, manager, filters),
        "card_window_min": settings.card_window_min,
        "order_window_hours": settings.order_window_hours,
        "threshold": settings.talk_threshold_sec,
    })
    return TEMPLATES.TemplateResponse("report.html", ctx)


def quick_periods(today: str) -> list[dict[str, str]]:
    """Готовые периоды для шапки отчёта.

    Отчёт открывается на сегодняшний день, и в тихий день он пуст. Пустая
    страница без единой кнопки читается как «сервис сломался» — так и вышло
    28.09.2026. Поэтому рядом с полями дат всегда лежат три готовых периода:
    промахнуться некуда.
    """
    end = date.fromisoformat(today)
    return [
        {"name": "сегодня", "since": today, "until": today},
        {"name": "неделя", "since": (end - timedelta(days=6)).isoformat(), "until": today},
        {"name": "месяц", "since": (end - timedelta(days=29)).isoformat(), "until": today},
    ]


def report_link(
    base: str, since: str, until: str, manager: str | None, filters: dict[str, Any],
):
    """Ссылка на тот же отчёт с изменённым параметром — для переключателей в шапке.

    Фильтры в заголовке таблицы работают ссылками, а не формой с кнопкой:
    так отбор занимает одну строку и переживает перезагрузку страницы.
    """
    current = {"since": since, "until": until, "manager": manager or "", **filters}

    def build(**over: Any) -> str:
        params = {**current, **over}
        clean = {key: value for key, value in params.items() if value not in ("", 0, None)}
        return f"{base}/report?{urlencode(clean)}"

    return build


@app.get("/leads", response_class=HTMLResponse)
async def leads_page(
    request: Request, since: str = "", until: str = "", manager: str = "",
    mode: str = "open", min_sec: str = "",
) -> Any:
    """Входящие звонки менеджерам, по которым заявки нет.

    Клиент звонит менеджеру напрямую и просит технику. Если менеджер не завёл
    заявку — о просьбе не знает никто. Здесь дешёвая проверка по метаданным:
    разговор был, заявки за сутки после него не появилось.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    until = as_date(until) or local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    since = as_date(since) or (date.fromisoformat(until) - timedelta(days=6)).isoformat()
    if since > until:
        since, until = until, since
    threshold = as_int(min_sec) or settings.inbound_min_duration_sec

    rows = inbound_rows(conn, since, until, only_open=(mode != "all"),
                        manager=manager or None, min_sec=threshold)
    ctx = _base_context(request)
    ctx.update({
        "since": since, "until": until, "mode": mode, "manager_login": manager,
        "min_sec": threshold, "rows": rows,
        "all_managers": managers(conn),
        "totals": inbound_totals(conn, since, until, threshold),
        "wait_hours": settings.inbound_wait_hours,
        "window_hours": settings.inbound_order_window_hours,
        "records_dir": settings.records_dir,
    })
    return TEMPLATES.TemplateResponse("leads.html", ctx)


@app.get("/leads/dismiss/{uid}")
async def leads_dismiss(request: Request, uid: str, back: int = 0) -> Any:
    """Пометить звонок как «запроса не было» — или вернуть его в список.

    Строка не удаляется: по отметкам потом считается, насколько точно отбор
    находит настоящие запросы.
    """
    conn = request.app.state.db
    dismiss_inbound(conn, uid, datetime.now(timezone.utc).isoformat(), back=bool(back))
    conn.commit()
    base = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    return RedirectResponse(f"{base}/leads", status_code=303)


@app.get("/trial", response_class=HTMLResponse)
async def trial_page(
    request: Request, since: str = "", until: str = "", show: str = "",
) -> Any:
    """Пробный прогон просева рядом с тем, что система решила в тот день.

    Заявки по этому экрану не создаются и боевой просев не трогается: это
    примерка новой головы и новых правил на прошедшем дне.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    until = as_date(until) or (local_now(settings.timezone_offset_hours)
                               - timedelta(days=1)).strftime("%Y-%m-%d")
    since = as_date(since) or until
    if since > until:
        since, until = until, since

    # «Всё» для счётчиков считаем по полному прогону, а показываем по
    # умолчанию только те разговоры, где заявка есть хоть с одной стороны.
    всё = trial_rows(conn, since, until, "все")
    rows = trial_rows(conn, since, until, show or "есть")
    ctx = _base_context(request)
    ctx.update({
        "since": since, "until": until, "show": show, "calls": rows,
        "всего": len(всё),
        "было": sum(1 for r in всё if r["live_lead"]),
        "стало": sum(1 for r in всё if r["trial_lead"]),
        "новые": sum(1 for r in всё if r["trial_lead"] and not r["live_lead"]),
        "пропали": sum(1 for r in всё if r["live_lead"] and not r["trial_lead"]),
        "размечено": sum(1 for r in всё if r["verdict"]),
        "голова": всё[0]["engine"] if всё else "",
    })
    return TEMPLATES.TemplateResponse("trial.html", ctx)


@app.post("/trial/mark")
async def trial_mark(
    request: Request, call_uid: str = Form(...), verdict: str = Form(...),
    note: str = Form(""), back: str = Form(""),
) -> Any:
    """Отметка владельца по пробному прогону. В CRM ничего не трогает."""
    from app.db import save_trial_verdict

    conn = request.app.state.db
    save_trial_verdict(conn, call_uid, verdict.strip(), note.strip(),
                       datetime.now(timezone.utc).isoformat())
    conn.commit()
    ctx = _base_context(request)
    адрес = back or f"{ctx['base']}/trial"
    return RedirectResponse(f"{адрес}#c{call_uid}", status_code=303)


@app.get("/caught", response_class=HTMLResponse)
async def caught_page(
    request: Request, since: str = "", until: str = "", verdict: str = "",
) -> Any:
    """Пойманные запросы потока 2 — послушать и разметить.

    Экран учебный, а не рабочий: заявки отсюда не создаются. Владелец слушает
    разговор, смотрит, что из него понял просев, и пишет, в чём тот ошибся.
    Из пояснений растут правила, которым мы учим модель.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    until = as_date(until) or local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    since = as_date(since) or (date.fromisoformat(until) - timedelta(days=13)).isoformat()
    if since > until:
        since, until = until, since

    всё = caught_rows(conn, since, until, None)
    rows = [r for r in всё if (not r["verdict"] if verdict == "нет" else r["verdict"] == verdict)] \
        if verdict else всё
    ctx = _base_context(request)
    ctx.update({
        "since": since, "until": until, "verdict": verdict, "calls": rows,
        "всего": len(всё),
        "размечено": sum(1 for r in всё if r["verdict"]),
        "верно": sum(1 for r in всё if r["verdict"] == "верно"),
        "неверно": sum(1 for r in всё if r["verdict"] == "неверно"),
        "спорно": sum(1 for r in всё if r["verdict"] == "спорно"),
    })
    return TEMPLATES.TemplateResponse("caught.html", ctx)


@app.post("/caught/mark")
async def caught_mark_post(
    request: Request, call_uid: str = Form(...), verdict: str = Form(...),
    note: str = Form(""), back: str = Form(""),
) -> Any:
    """Отметка владельца по пойманному запросу. В CRM ничего не трогает."""
    from app.db import save_screen_verdict

    conn = request.app.state.db
    save_screen_verdict(conn, call_uid, verdict.strip(), note.strip(),
                        datetime.now(timezone.utc).isoformat())
    conn.commit()
    ctx = _base_context(request)
    адрес = back or f"{ctx['base']}/caught"
    return RedirectResponse(f"{адрес}#c{call_uid}", status_code=303)


@app.get("/leads/judge/{uid}")
async def leads_judge(request: Request, uid: str, verdict: str = "") -> Any:
    """Вердикт человека по уже заведённой заявке: «верная» или «лишняя».

    С 18.09.2026 заявка заводится сразу после разговора, а смотрят её потом:
    за три часа ожидания заказчик успевает найти технику в другом месте.
    Отметка нужна не для отчётности — по ней считается точность на живом
    потоке, и видно, когда запрос к модели пора править.

    Заявку в CRM отметка не трогает: лишнюю закрывает человек там, где виден
    весь контекст.
    """
    conn = request.app.state.db
    judge_screen(conn, uid, verdict, datetime.now(timezone.utc).isoformat())
    conn.commit()
    base = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    return RedirectResponse(f"{base}/leads", status_code=303)


@app.get("/leads/approve/{uid}")
async def leads_approve(request: Request, uid: str, back: int = 0) -> Any:
    """Подтвердить находку: по ней будет заведена заявка в CRM.

    Заявка создаётся не здесь, а ближайшим часовым запуском: распознавание
    записи целиком занимает минуты, столько держать страницу нельзя.
    """
    conn = request.app.state.db
    approve_screen(conn, uid, datetime.now(timezone.utc).isoformat(), back=bool(back))
    conn.commit()
    base = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    return RedirectResponse(f"{base}/leads", status_code=303)


@app.get("/quality", response_class=HTMLResponse)
async def quality_page(request: Request) -> Any:
    """Точность просева на проверочном наборе — цифрой, а не на ощупь.

    Отметки «заявка верная» и «запроса не было» на странице находок копятся,
    но по ним нельзя судить о правках запроса: набор звонков каждый день
    разный. Поэтому качество меряется на постоянном наборе с известными
    ответами, а сюда выводится последний замер и вся история — видно, какая
    правка что дала.
    """
    settings: Settings = request.app.state.settings
    data_dir = Path(settings.db_path).parent
    ctx = _base_context(request)
    ctx.update({"run": None, "errors": [], "kinds": [], "history": [],
                "checkset_n": 0, "checkset_date": ""})

    checkset_path = Path(__file__).resolve().parents[1] / "checkset" / "inbound-screening.json"
    kind_names: dict[str, str] = {}
    if checkset_path.exists():
        doc = json.loads(checkset_path.read_text(encoding="utf-8"))
        kind_names = doc.get("виды ошибок") or {}
        ctx["checkset_n"] = doc.get("разговоров") or len(doc.get("items") or [])
        ctx["checkset_date"] = doc.get("размечено") or ""

    last_path = data_dir / "screening_last.json"
    if last_path.exists():
        last = json.loads(last_path.read_text(encoding="utf-8"))
        ctx["run"] = last
        errors = [i for i in last.get("items", [])
                  if i.get("predicted") and i["predicted"] != i["label"]]
        ctx["errors"] = errors
        counts: dict[str, int] = {}
        for item in errors:
            if item["predicted"] == "request":
                counts[item.get("kind") or "прочее"] = counts.get(item.get("kind") or "прочее", 0) + 1
        ctx["kinds"] = [(k, n, kind_names.get(k, "разбирается"))
                        for k, n in sorted(counts.items(), key=lambda x: -x[1])]

    # Точность на живом потоке: отметки «верная»/«лишняя» по заведённым заявкам.
    # Проверочный набор показывает, что даёт правка запроса; эта цифра — что
    # получается на самом деле, на звонках, которых в наборе не было.
    conn = request.app.state.db
    live = dict(conn.execute(
        "SELECT verdict, COUNT(*) FROM screens "
        "WHERE created_order_id IS NOT NULL AND verdict <> '' GROUP BY verdict"
    ).fetchall())
    live_ok, live_wrong = live.get("ok", 0), live.get("wrong", 0)
    waiting = conn.execute(
        "SELECT COUNT(*) FROM screens WHERE created_order_id IS NOT NULL AND verdict = ''"
    ).fetchone()[0]
    ctx.update({
        "live_ok": live_ok, "live_wrong": live_wrong, "live_waiting": waiting,
        "live_precision": round(live_ok / (live_ok + live_wrong) * 100, 1)
        if (live_ok + live_wrong) else None,
    })

    # Остаток на счёте модели: конвейер уже вставал молча, когда деньги
    # кончились посреди дня.
    from app.stats import openai_balance, openai_spend

    today = local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    ctx["spend_today"] = openai_spend(str(data_dir), today)
    ctx["balance"] = openai_balance(str(data_dir), settings.openai_topup_usd,
                                    settings.openai_topup_at)

    runs_path = data_dir / "screening_runs.jsonl"
    if runs_path.exists():
        rows = []
        for line in runs_path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        ctx["history"] = list(reversed(rows))[:20]

    return TEMPLATES.TemplateResponse("quality.html", ctx)


@app.get("/record/{uid}.mp3")
async def record_file(request: Request, uid: str) -> Any:
    """Отдать скачанную запись разговора, если она уже лежит на сервере.

    Сама ВАТС записи наружу не отдаёт: они доступны только с российского
    адреса. Поэтому слушать можно то, что уже скачано `fetch_records.py`.
    """
    settings: Settings = request.app.state.settings
    path = Path(settings.records_dir) / f"{uid}.mp3"
    if not path.exists():
        return HTMLResponse("Запись ещё не скачана", status_code=404)
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(
    request: Request, since: str = "", until: str = "", kind: str = "",
) -> Any:
    """Заявки за период и чем они кончились."""
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    until = as_date(until) or local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    since = as_date(since) or (date.fromisoformat(until) - timedelta(days=13)).isoformat()
    if since > until:
        since, until = until, since

    # Считаем по всем заявкам периода, а не по отфильтрованным: иначе цифра
    # «нужна помощь» прыгает при переключении вкладки, и по ней нельзя перейти.
    everything = orders_of_period(conn, since, until, None)
    if kind == "rop":
        rows = [r for r in everything if r["needs_rop"]]
    elif kind == "lost":
        rows = [r for r in everything if r["stage_kind"] == "lost"]
    elif kind == "won":
        rows = [r for r in everything if r["stage_kind"] == "won"]
    elif kind == "open":
        rows = [r for r in everything if r["stage_kind"] not in ("won", "lost")]
    else:
        rows = everything
    ctx = _base_context(request)
    ctx.update({
        "since": since, "until": until, "kind": kind, "orders": rows,
        "total": len(everything),
        "won": sum(1 for r in everything if r["stage_kind"] == "won"),
        "lost": sum(1 for r in everything if r["stage_kind"] == "lost"),
        "analyzed": sum(1 for r in everything if r["verdict"]),
        "rop": sum(1 for r in everything if r["needs_rop"]),
    })
    return TEMPLATES.TemplateResponse("orders.html", ctx)


@app.get("/order/{order_id}", response_class=HTMLResponse)
async def order_page(request: Request, order_id: str) -> Any:
    """Одна заявка: что было в разговорах после неё и почему она не стала сделкой."""
    conn = request.app.state.db
    order = order_detail(conn, order_id)
    if order:
        # Расшифровка — сразу на странице, разворачиваемыми репликами, а не
        # отдельной ссылкой на /call/{uid}: РОП физически не успевает слушать
        # запись каждого звонка, нужно всё в одном месте (владелец, 15.11.2026).
        for c in order["calls"]:
            c["turns"] = split_turns(c.get("transcript_text"))
    ctx = _base_context(request)
    ctx.update({"order": order, "order_id": order_id})
    return TEMPLATES.TemplateResponse("order.html", ctx)


@app.post("/order/{order_id}/comment")
async def order_comment_post(request: Request, order_id: str, author: str = Form(""),
                             comment: str = Form(""), back: str = Form("")) -> Any:
    """Заметка РОПа по заявке — своя, не в Synergy (см. order.html)."""
    conn = request.app.state.db
    if comment.strip():
        save_order_comment(conn, order_id, author.strip(), comment.strip(),
                           datetime.now(timezone.utc).isoformat())
        conn.commit()
    ctx = _base_context(request)
    адрес = back or f"{ctx['base']}/order/{order_id}"
    return RedirectResponse(адрес, status_code=303)


def training_draft_text(draft_json: str) -> str:
    """Черновик звонка строками «[мм:сс] роль: текст» — метки времени нужны, чтобы
    исправленный текст потом можно было привязать к кускам звука."""
    lines = []
    try:
        segments = json.loads(draft_json or "[]")
    except ValueError:
        segments = []
    for seg in segments:
        moment = int(seg.get("start") or 0)
        lines.append(f"[{moment // 60:02d}:{moment % 60:02d}] {seg.get('role', '')}: {seg.get('text', '')}")
    return "\n".join(lines)


@app.get("/training", response_class=HTMLResponse)
async def training_page(request: Request, batch: int | None = None) -> Any:
    """Эталонный набор для дообучения распознавания: послушать звонок и поправить текст.

    Цель владельца (15.11.2026) — 50 часов исправленных звонков, начинать по 30
    минут в день. Черновик посчитан на офисной карте с метками времени; исправляют
    прямо в поле, формат строк «[мм:сс] роль: текст» не ломая.
    """
    conn = request.app.state.db
    all_rows = training_rows(conn)
    batches = sorted({r["batch"] for r in all_rows}, reverse=True)
    current = batch if batch in batches else (batches[0] if batches else None)
    items = []
    for row in all_rows:
        if row["batch"] != current:
            continue
        item = dict(row)
        item["text"] = row["corrected_text"] or training_draft_text(row["draft_json"])
        item["tag_list"] = [t for t in (row["tags"] or "").split(",") if t]
        item["rows"] = min(max(item["text"].count("\n") + 3, 6), 30)
        items.append(item)
    done_all = sum(r["duration_sec"] for r in all_rows if r["status"] == "done")
    ctx = _base_context(request)
    ctx.update({
        "items": items, "batch": current, "batches": batches,
        "batch_total_min": round(sum(i["duration_sec"] for i in items) / 60, 1),
        "batch_done_min": round(sum(i["duration_sec"] for i in items if i["status"] == "done") / 60, 1),
        "done_hours": round(done_all / 3600, 2), "goal_hours": 50,
    })
    return TEMPLATES.TemplateResponse("training.html", ctx)


@app.post("/training/save")
async def training_save(request: Request, call_uid: str = Form(...), corrected: str = Form(""),
                        status: str = Form("draft"), tags: list[str] = Form(default=[]),
                        batch: int = Form(0)) -> Any:
    """Сохранить исправление. done — готово, draft — не закончено, skip — не брать."""
    conn = request.app.state.db
    if status not in ("done", "draft", "skip"):
        status = "draft"
    allowed = {"акцент", "шум", "роли_перепутаны", "личный_разговор"}
    clean_tags = ",".join(t for t in tags if t in allowed)
    save_training_correction(conn, call_uid, corrected.strip(), clean_tags, status,
                             datetime.now(timezone.utc).isoformat())
    conn.commit()
    ctx = _base_context(request)
    return RedirectResponse(f"{ctx['base']}/training?batch={batch}#c{call_uid}", status_code=303)


@app.get("/call/{uid}", response_class=HTMLResponse)
async def call_page(request: Request, uid: str) -> Any:
    conn = request.app.state.db
    ctx = _base_context(request)
    ctx.update({"call": call_detail(conn, uid), "card_fields": CARD_FIELDS})
    return TEMPLATES.TemplateResponse("call.html", ctx)
