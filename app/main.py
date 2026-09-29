"""Дашборд прозвона: экраны и служебные ручки.

Доступ закрыт basic-авторизацией на уровне nginx — дашборд показывает работу
конкретных людей. Отдельных учёток внутри приложения пока нет: когда менеджерам
понадобится видеть свою статистику, это надо будет делать по-настоящему, а не
раздачей общего пароля.
"""

from __future__ import annotations

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
    judge_screen, save_ad_verdict,
)
from fastapi.responses import FileResponse, RedirectResponse

from app.stats import _parsed_analysis as parsed_analysis  # noqa: E402
from app.stats import (  # noqa: F401
    search_day, search_feed, search_tasks, heard_checks, heard_summary,
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


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, day: str | None = None,
                      who: str | None = None) -> Any:
    """День отдела поиска техники: звонки поставщикам и правки в карточках.

    Отдельный экран, а не строка в прозвоне: работа другая и меряется другим.
    Прозвон меряется планом звонков и дисциплиной карточки клиента, поиск —
    обзвоном поставщиков и тем, что после него поменялось в транспорте.
    """
    settings: Settings = request.app.state.settings
    conn = request.app.state.db
    current = day or local_now(settings.timezone_offset_hours).strftime("%Y-%m-%d")
    ctx = _base_context(request)
    heard = heard_checks(conn, current)
    ctx.update({
        "day": current,
        "people": search_day(conn, current, threshold_sec=settings.talk_threshold_sec),
        "feed": search_feed(conn, current, who),
        "tasks": search_tasks(conn),
        "heard": heard,
        "heard_total": heard_summary(heard),
        "who": who,
    })
    return TEMPLATES.TemplateResponse("search.html", ctx)


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
