"""Дашборд прозвона: экраны и служебные ручки.

Доступ закрыт basic-авторизацией на уровне nginx — дашборд показывает работу
конкретных людей. Отдельных учёток внутри приложения пока нет: когда менеджерам
понадобится видеть свою статистику, это надо будет делать по-настоящему, а не
раздачей общего пароля.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import Settings, get_settings
from app.db import CARD_FIELDS, connect, has_any_data, init_schema
from app.stats import (
    call_detail,
    calls_of_day,
    day_summary,
    local_now,
    managers,
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


def _base_context(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return {
        "request": request,
        "version": __version__,
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
    request: Request, day: str | None = None, days: int = 1, manager: str | None = None,
) -> Any:
    """Развёрнутая таблица: что произошло по каждому разговору."""
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    until = day or local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    days = max(1, min(days, 60))
    since = (date.fromisoformat(until) - timedelta(days=days - 1)).isoformat()

    rows = report_rows(conn, since, until, manager, settings.talk_threshold_sec)
    ctx = _base_context(request)
    ctx.update({
        "day": until,
        "since": since,
        "days": days,
        "manager_login": manager,
        "manager": next((m for m in managers(conn) if m["vats_login"] == manager), None),
        "all_managers": managers(conn),
        "rows": rows,
        "totals": report_totals(rows),
        "order_window_hours": settings.order_window_hours,
        "threshold": settings.talk_threshold_sec,
    })
    return TEMPLATES.TemplateResponse("report.html", ctx)


@app.get("/call/{uid}", response_class=HTMLResponse)
async def call_page(request: Request, uid: str) -> Any:
    conn = request.app.state.db
    ctx = _base_context(request)
    ctx.update({"call": call_detail(conn, uid), "card_fields": CARD_FIELDS})
    return TEMPLATES.TemplateResponse("call.html", ctx)
