"""FastAPI panel. Binds to localhost unless --host says otherwise."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.catalog import load_categories, load_cities, refresh_cities, search_cities
from app.db import Database
from app.divar.client import DivarClient
from app.divar.parse import FA_DIGITS, extract_token, normalize_phone_input
from app.exporting import listing_row, to_csv, to_xlsx
from app.hub import EventHub
from app.runner import JobManager, city_name

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
DATA = Path(ROOT / "data")


class JobIn(BaseModel):
    name: str = ""
    category: str
    keywords: list[str] = Field(default_factory=list)
    city_id: str = "1"
    title_filter: list[str] = Field(default_factory=list)
    desc_filter: list[str] = Field(default_factory=list)
    max_pages: int = 3
    fetch_details: bool = True
    fetch_phones: bool = False


class SettingsIn(BaseModel):
    anon_rps: float = 1
    default_daily_cap: int = 50
    delay_min: float = 20
    delay_max: float = 40


class CodeRequest(BaseModel):
    phone: str
    proxy: str | None = None


class CodeConfirm(BaseModel):
    phone: str
    code: str
    proxy: str | None = None
    daily_cap: int = 50
    delay_min: float = 20
    delay_max: float = 40


def _split_words(values: list[str]) -> list[str]:
    words: list[str] = []
    for value in values:
        for part in value.replace("،", ",").split(","):
            part = part.strip()
            if part:
                words.append(part)
    return words


def create_app(data_dir: Path | None = None) -> FastAPI:
    data = data_dir or DATA
    data.mkdir(parents=True, exist_ok=True)
    db = Database(data / "panel.db")
    hub = EventHub()
    manager = JobManager(db, hub, data)
    auth_client = DivarClient()
    app = FastAPI(title="پنل دیوار")
    app.state.db = db
    app.state.manager = manager
    app.state.data = data
    app.state.auth = auth_client

    @app.on_event("startup")
    async def _startup() -> None:
        manager.resume_interrupted()

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/categories")
    async def categories() -> list[dict[str, Any]]:
        return load_categories(data)

    @app.get("/api/cities")
    async def cities(q: str = "") -> list[dict[str, Any]]:
        return search_cities(load_cities(data), q)

    @app.post("/api/cities/refresh")
    async def cities_refresh() -> dict[str, Any]:
        return await refresh_cities(data, app.state.auth)

    @app.get("/api/settings")
    async def get_settings() -> dict[str, str]:
        return db.settings()

    @app.put("/api/settings")
    async def put_settings(body: SettingsIn) -> dict[str, str]:
        if body.anon_rps <= 0 or body.anon_rps > 5:
            raise HTTPException(400, "نرخ درخواست باید بین ۰ و ۵ باشد")
        if body.delay_min < 5:
            raise HTTPException(400, "حداقل فاصله شماره ۵ ثانیه است")
        saved = db.update_settings(
            {
                "anon_rps": str(body.anon_rps),
                "default_daily_cap": str(body.default_daily_cap),
                "delay_min": str(body.delay_min),
                "delay_max": str(body.delay_max),
            }
        )
        manager.refresh_rate()
        return saved

    @app.get("/api/jobs")
    async def jobs() -> list[dict[str, Any]]:
        result = []
        for job in db.list_jobs():
            job["counts"] = db.job_counts(job["id"])
            result.append(job)
        return result

    @app.post("/api/jobs")
    async def create_job(body: JobIn) -> dict[str, Any]:
        category = body.category.strip()
        if not category:
            raise HTTPException(400, "دسته را وارد کنید")
        name = body.name.strip() or category
        job = db.create_job(
            {
                "name": name,
                "category": category,
                "keywords": _split_words(body.keywords),
                "city_id": str(body.city_id),
                "city_name": city_name(data, body.city_id),
                "title_filter": _split_words(body.title_filter),
                "desc_filter": _split_words(body.desc_filter),
                "max_pages": max(1, min(body.max_pages, 200)),
                "fetch_details": body.fetch_details or body.fetch_phones,
                "fetch_phones": body.fetch_phones,
            }
        )
        job["counts"] = db.job_counts(job["id"])
        return job

    @app.post("/api/jobs/{job_id}/start")
    async def start_job(job_id: int) -> dict[str, Any]:
        _require_job(db, job_id)
        manager.start(job_id)
        job = db.get_job(job_id)
        job["counts"] = db.job_counts(job_id)
        return job

    @app.post("/api/jobs/{job_id}/pause")
    async def pause_job(job_id: int) -> dict[str, Any]:
        _require_job(db, job_id)
        manager.pause(job_id)
        job = db.get_job(job_id)
        job["counts"] = db.job_counts(job_id)
        return job

    @app.post("/api/jobs/{job_id}/stop")
    async def stop_job(job_id: int) -> dict[str, Any]:
        _require_job(db, job_id)
        await manager.stop(job_id)
        job = db.get_job(job_id)
        job["counts"] = db.job_counts(job_id)
        return job

    @app.get("/api/listings")
    async def listings(
        job_id: int | None = None,
        q: str = "",
        sort: str = "created_at",
        direction: str = "desc",
        limit: int = Query(200, le=2000),
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = db.query_listings(job_id, q, sort, direction, limit, offset)
        names = {job["id"]: job["name"] for job in db.list_jobs()}
        for row in rows:
            row["job_name"] = names.get(row["job_id"], "")
        return rows

    @app.get("/api/logs")
    async def logs(job_id: int | None = None) -> list[dict[str, Any]]:
        return db.recent_logs(300, job_id)

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        queue = await hub.subscribe()

        async def stream():
            try:
                yield "event: hello\ndata: {}\n\n"
                while True:
                    event, payload = await queue.get()
                    yield f"event: {event}\ndata: {payload}\n\n"
            finally:
                await hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/accounts")
    async def accounts() -> list[dict[str, Any]]:
        return db.list_accounts()

    @app.post("/api/accounts/request-code")
    async def request_code(body: CodeRequest) -> dict[str, Any]:
        try:
            phone = normalize_phone_input(body.phone)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        status, text = await app.state.auth.request_code(phone, _proxy(body.proxy))
        if status != 200:
            raise HTTPException(status, _safe_error(text))
        return {"ok": True, "phone": phone}

    @app.post("/api/accounts/confirm")
    async def confirm_code(body: CodeConfirm) -> dict[str, Any]:
        try:
            phone = normalize_phone_input(body.phone)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        code = "".join(ch for ch in body.code.translate(FA_DIGITS) if ch.isdigit())
        if not code:
            raise HTTPException(400, "کد را وارد کنید")
        status, payload, text = await app.state.auth.confirm_code(phone, code, _proxy(body.proxy))
        if status != 200:
            raise HTTPException(status, _safe_error(text))
        token = extract_token(payload)
        if not token:
            raise HTTPException(502, "توکن در پاسخ دیوار نبود")
        settings = db.settings()
        account = db.add_account(
            {
                "phone": phone,
                "token": token,
                "proxy": _proxy(body.proxy),
                "daily_cap": body.daily_cap or int(settings["default_daily_cap"]),
                "delay_min": body.delay_min,
                "delay_max": body.delay_max,
            }
        )
        return account

    @app.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: int) -> dict[str, bool]:
        db.delete_account(account_id)
        return {"ok": True}

    @app.get("/api/export.csv")
    async def export_csv(job_id: int | None = None, q: str = "") -> Response:
        rows, name = _export_rows(db, job_id, q)
        return Response(
            to_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={name}.csv"},
        )

    @app.get("/api/export.xlsx")
    async def export_xlsx(job_id: int | None = None, q: str = "") -> Response:
        rows, name = _export_rows(db, job_id, q)
        return Response(
            to_xlsx(rows),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={name}.xlsx"},
        )

    return app


def _require_job(db: Database, job_id: int) -> dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "کار پیدا نشد")
    return job


def _export_rows(db: Database, job_id: int | None, q: str) -> tuple[list[list[Any]], str]:
    names = {job["id"]: job["name"] for job in db.list_jobs()}
    items = db.query_listings(job_id, q, "created_at", "desc", 5000, 0)
    rows = [listing_row(item, names.get(item["job_id"], "")) for item in items]
    if job_id and job_id in names:
        return rows, f"job-{job_id}"
    return rows, "listings"


def _proxy(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_error(text: str) -> str:
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or "درخواست ناموفق بود")[:180]
    except Exception:
        pass
    return "درخواست دیوار ناموفق بود"


app = create_app()
