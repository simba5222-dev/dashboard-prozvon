"""Дашборд прозвона: экраны и служебные ручки.

Доступ закрыт basic-авторизацией на уровне nginx — дашборд показывает работу
конкретных людей. Отдельных учёток внутри приложения пока нет: когда менеджерам
понадобится видеть свою статистику, это надо будет делать по-настоящему, а не
раздачей общего пароля.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import Settings, get_settings
from app.db import (
    CARD_FIELDS, approve_screen, connect, dismiss_inbound, has_any_data, init_schema,
)
from fastapi.responses import FileResponse, RedirectResponse

from app.stats import (
    call_detail,
    calls_of_day,
    day_summary,
    inbound_rows,
    inbound_totals,
    local_now,
    managers,
    order_detail,
    orders_of_period,
    period_summary,
    report_rows,
    report_totals,
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
    }


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

    rows = orders_of_period(conn, since, until, kind or None)
    ctx = _base_context(request)
    ctx.update({
        "since": since, "until": until, "kind": kind, "orders": rows,
        "won": sum(1 for r in rows if r["stage_kind"] == "won"),
        "lost": sum(1 for r in rows if r["stage_kind"] == "lost"),
        "analyzed": sum(1 for r in rows if r["verdict"]),
    })
    return TEMPLATES.TemplateResponse("orders.html", ctx)


@app.get("/order/{order_id}", response_class=HTMLResponse)
async def order_page(request: Request, order_id: str) -> Any:
    """Одна заявка: что было в разговорах после неё и почему она не стала сделкой."""
    conn = request.app.state.db
    ctx = _base_context(request)
    ctx.update({"order": order_detail(conn, order_id), "order_id": order_id})
    return TEMPLATES.TemplateResponse("order.html", ctx)


@app.get("/call/{uid}", response_class=HTMLResponse)
async def call_page(request: Request, uid: str) -> Any:
    conn = request.app.state.db
    ctx = _base_context(request)
    ctx.update({"call": call_detail(conn, uid), "card_fields": CARD_FIELDS})
    return TEMPLATES.TemplateResponse("call.html", ctx)
