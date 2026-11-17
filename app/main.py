"""FastAPI panel. Binds to localhost unless --host says otherwise."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.catalog import load_categories, load_cities, refresh_cities, search_cities
from app.db import Database
from app.divar.browser import BrowserPool
from app.divar.client import DivarClient
from app.divar.parse import FA_DIGITS, extract_token, normalize_phone_input
from app.divar.traffic import TrafficBuffer
from app.divar.urls import resolve_divar_search_url
from app.exporting import listing_row, to_csv, to_xlsx
from app.hub import EventHub
from app.rate_policy import (
    MAX_DAILY_CAP,
    MAX_REVEALS_PER_10_MIN,
    MAX_REVEALS_PER_HOUR,
    MIN_PHONE_DELAY_SEC,
    clamp_captcha_cooldown_sec,
    clamp_daily_cap,
    clamp_phone_delays,
    clamp_setting_int,
    parse_iso,
    tehran_now,
)
from app.runner import HumanBrowseConflictError, JobManager, city_name

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))


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
    loop: bool = False


class JobFromUrlIn(BaseModel):
    url: str
    name: str = ""
    title_filter: list[str] = Field(default_factory=list)
    desc_filter: list[str] = Field(default_factory=list)
    max_pages: int = 3
    fetch_details: bool = True
    fetch_phones: bool = False
    loop: bool = False
    start: bool = True


class SettingsIn(BaseModel):
    anon_rps: float = 1
    default_daily_cap: int = 80
    delay_min: float = 25
    delay_max: float = 45
    phone_batch_size: int = 4
    phone_batch_pause_sec: float = 600
    reveals_per_10_min: int = 5
    reveals_per_hour: int = 20
    captcha_cooldown_sec: float = 2700


class CodeRequest(BaseModel):
    phone: str
    proxy: str | None = None


class CodeConfirm(BaseModel):
    phone: str
    code: str
    proxy: str | None = None
    daily_cap: int = 80
    delay_min: float = 25
    delay_max: float = 45


class BrowserAccountIn(BaseModel):
    label: str = ""
    daily_cap: int = 80
    delay_min: float = 25
    delay_max: float = 45


class BrowserAccountUpdate(BaseModel):
    label: str | None = None
    daily_cap: int | None = None
    delay_min: float | None = None
    delay_max: float | None = None


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
    traffic = TrafficBuffer()
    traffic.on_entry(lambda entry: hub.publish("traffic", entry))
    browsers = BrowserPool(data / "chrome-profiles", traffic)
    manager = JobManager(db, hub, data, traffic=traffic, browsers=browsers)
    auth_client = DivarClient(traffic=traffic)
    app = FastAPI(title="پنل دیوار")
    app.state.db = db
    app.state.manager = manager
    app.state.data = data
    app.state.auth = auth_client
    app.state.traffic = traffic
    app.state.browsers = browsers
    app.state.browser = browsers  # captcha endpoints

    @app.on_event("startup")
    async def _startup() -> None:
        db.release_phone_claims()
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
        delay_min, delay_max = clamp_phone_delays(body.delay_min, body.delay_max)
        if body.delay_min < MIN_PHONE_DELAY_SEC or body.delay_max < MIN_PHONE_DELAY_SEC:
            raise HTTPException(
                400,
                f"حداقل فاصله بین هر شماره حداقل {int(MIN_PHONE_DELAY_SEC)} ثانیه است (در کد هم اجبار می‌شود)",
            )
        if delay_max < delay_min:
            raise HTTPException(400, "بیشترین تأخیر نباید از کمترین کمتر باشد")
        cap_default = clamp_daily_cap(body.default_daily_cap)
        if body.default_daily_cap > MAX_DAILY_CAP:
            raise HTTPException(400, f"سقف روزانه پیش‌فرض حداکثر {MAX_DAILY_CAP} است")
        if body.phone_batch_size < 1 or body.phone_batch_size > 50:
            raise HTTPException(400, "اندازه بسته باید بین ۱ و ۵۰ باشد")
        if body.phone_batch_pause_sec < 0:
            raise HTTPException(400, "مکث بسته نمی‌تواند منفی باشد")
        reveals_10m = clamp_setting_int(
            body.reveals_per_10_min, default=5, upper=MAX_REVEALS_PER_10_MIN
        )
        reveals_1h = clamp_setting_int(body.reveals_per_hour, default=20, upper=MAX_REVEALS_PER_HOUR)
        captcha_cd = clamp_captcha_cooldown_sec(body.captcha_cooldown_sec)
        saved = db.update_settings(
            {
                "anon_rps": str(body.anon_rps),
                "default_daily_cap": str(cap_default),
                "delay_min": str(delay_min),
                "delay_max": str(delay_max),
                "phone_batch_size": str(int(body.phone_batch_size)),
                "phone_batch_pause_sec": str(body.phone_batch_pause_sec),
                "reveals_per_10_min": str(reveals_10m),
                "reveals_per_hour": str(reveals_1h),
                "captcha_cooldown_sec": str(int(captcha_cd)),
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
                "max_pages": max(0, min(body.max_pages, 200)),
                "fetch_details": body.fetch_details,
                "fetch_phones": body.fetch_phones,
                "loop": body.loop,
            }
        )
        job["counts"] = db.job_counts(job["id"])
        return job

    @app.post("/api/jobs/from-url")
    async def create_job_from_url(body: JobFromUrlIn) -> dict[str, Any]:
        try:
            resolved = await resolve_divar_search_url(
                body.url,
                cities=load_cities(data),
                categories=load_categories(data),
                fetch_html=auth_client.fetch_html,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        category = resolved["category"]
        city_id = str(resolved["city_id"])
        city_label = resolved["city_name"]
        name = body.name.strip() or f"{category} · {city_label}"
        job = db.create_job(
            {
                "name": name,
                "category": category,
                "keywords": resolved.get("keywords") or [],
                "city_id": city_id,
                "city_name": city_label,
                "title_filter": _split_words(body.title_filter),
                "desc_filter": _split_words(body.desc_filter),
                "max_pages": max(0, min(body.max_pages, 200)),
                "fetch_details": body.fetch_details,
                "fetch_phones": body.fetch_phones,
                "loop": body.loop,
                "source_url": resolved.get("source_url"),
            }
        )
        job["counts"] = db.job_counts(job["id"])
        job["resolved_from"] = resolved.get("resolved_from")
        if body.start:
            try:
                manager.start(job["id"])
            except HumanBrowseConflictError as exc:
                other = int(exc.args[0]) if exc.args else 0
                raise HTTPException(
                    409,
                    f"یک کار خزش انسانی دیگر (شناسه {other}) همین‌الان روی این سرور/IP فعال است.",
                ) from exc
            job = db.get_job(job["id"]) or job
            job["counts"] = db.job_counts(job["id"])
            job["resolved_from"] = resolved.get("resolved_from")
        return job

    @app.post("/api/jobs/{job_id}/start")
    async def start_job(job_id: int) -> dict[str, Any]:
        job = _require_job(db, job_id)
        try:
            manager.start(job_id)
        except HumanBrowseConflictError as exc:
            other = int(exc.args[0]) if exc.args else 0
            raise HTTPException(
                409,
                f"یک کار خزش انسانی دیگر (شناسه {other}) همین‌الان روی این سرور/IP فعال است. "
                "اول آن را متوقف کنید یا صبر کنید تمام شود.",
            ) from exc
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

    @app.post("/api/jobs/{job_id}/loop")
    async def toggle_job_loop(job_id: int) -> dict[str, Any]:
        job = _require_job(db, job_id)
        enabled = not bool(job.get("loop"))
        db.update_job(job_id, loop=enabled)
        if enabled and job["status"] in {"done", "stopped", "paused", "error"}:
            try:
                manager.start(job_id)
            except HumanBrowseConflictError as exc:
                other = int(exc.args[0]) if exc.args else 0
                raise HTTPException(
                    409,
                    f"یک کار خزش انسانی دیگر (شناسه {other}) همین‌الان روی این سرور/IP فعال است.",
                ) from exc
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

    @app.get("/api/traffic")
    async def traffic_logs(limit: int = Query(200, le=400)) -> list[dict[str, Any]]:
        return app.state.traffic.recent(limit)

    @app.delete("/api/traffic")
    async def clear_traffic() -> dict[str, bool]:
        app.state.traffic.clear()
        return {"ok": True}

    @app.get("/api/browser/captcha")
    async def browser_captcha_status() -> dict[str, Any]:
        return browsers.captcha_status()

    @app.post("/api/browser/captcha/resolved")
    async def browser_captcha_resolved() -> dict[str, Any]:
        blocked_id = browsers.captcha_status().get("account_id")
        status = browsers.resolve_captcha()

        def _post_captcha_status(account_id: int) -> None:
            row = db.get_browser_account(account_id) or {}
            until = parse_iso(row.get("captcha_cooldown_until"))
            if until and tehran_now() < until:
                next_status = str(row.get("status") or "cooldown")
                if next_status == "captcha":
                    next_status = "cooldown"
                restored = db.update_browser_account(
                    account_id,
                    status=next_status,
                    last_error=row.get("last_error") or "post-captcha cooldown",
                )
            else:
                restored = db.update_browser_account(account_id, status="active", last_error=None)
            hub.publish("browser_account", restored)

        if blocked_id:
            _post_captcha_status(int(blocked_id))
        for account in db.list_browser_accounts():
            if account.get("status") == "captcha":
                _post_captcha_status(int(account["id"]))
        hub.publish("captcha", status)
        return {"ok": True, **status}

    @app.get("/api/reveal-events")
    async def reveal_events(
        account_id: int | None = None,
        kind: str | None = None,
        limit: int = Query(200, le=2000),
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return db.list_reveal_events(account_id=account_id, kind=kind, limit=limit, offset=offset)

    @app.post("/api/browser-accounts/{account_id}/focus")
    async def focus_browser_account(account_id: int) -> dict[str, Any]:
        account = db.get_browser_account(account_id)
        if not account:
            raise HTTPException(404, "اکانت پیدا نشد")
        try:
            await browsers.focus(account_id)
        except Exception as exc:
            raise HTTPException(500, f"نمایش اکانت ناموفق بود: {exc}") from exc
        return {
            "ok": True,
            "vnc_url": browsers.vnc_url,
            "message": "فقط پنجره این اکانت روی VNC مانده است.",
            "account": account,
        }

    @app.get("/api/browser-accounts")
    async def browser_accounts() -> list[dict[str, Any]]:
        return db.list_browser_accounts()

    @app.post("/api/browser-accounts")
    async def create_browser_account(body: BrowserAccountIn) -> dict[str, Any]:
        if body.delay_min < MIN_PHONE_DELAY_SEC or body.delay_max < MIN_PHONE_DELAY_SEC:
            raise HTTPException(
                400,
                f"حداقل فاصله بین هر شماره حداقل {int(MIN_PHONE_DELAY_SEC)} ثانیه است",
            )
        cap = clamp_daily_cap(body.daily_cap)
        lo, hi = clamp_phone_delays(body.delay_min, body.delay_max)
        account = db.add_browser_account(
            {
                "label": body.label,
                "daily_cap": cap,
                "delay_min": lo,
                "delay_max": hi,
            }
        )
        hub.publish("browser_account", account)
        return account

    @app.patch("/api/browser-accounts/{account_id}")
    async def patch_browser_account(account_id: int, body: BrowserAccountUpdate) -> dict[str, Any]:
        account = db.get_browser_account(account_id)
        if not account:
            raise HTTPException(404, "اکانت پیدا نشد")
        fields: dict[str, Any] = {}
        if body.label is not None:
            label = body.label.strip()
            if label:
                fields["label"] = label
        if body.daily_cap is not None:
            if body.daily_cap < 1:
                raise HTTPException(400, "سقف روزانه باید حداقل ۱ باشد")
            if body.daily_cap > MAX_DAILY_CAP:
                raise HTTPException(400, f"سقف روزانه هر اکانت حداکثر {MAX_DAILY_CAP} است")
            fields["daily_cap"] = clamp_daily_cap(body.daily_cap)
            # If they raise the cap after hitting limit, allow the account again today.
            if account.get("status") == "limited" and int(body.daily_cap) > int(account.get("views_today") or 0):
                fields["status"] = "active"
                fields["last_error"] = None
        if body.delay_min is not None:
            if body.delay_min < MIN_PHONE_DELAY_SEC:
                raise HTTPException(
                    400,
                    f"حداقل فاصله بین هر شماره حداقل {int(MIN_PHONE_DELAY_SEC)} ثانیه است",
                )
            fields["delay_min"] = float(body.delay_min)
        if body.delay_max is not None:
            if body.delay_max < MIN_PHONE_DELAY_SEC:
                raise HTTPException(
                    400,
                    f"حداقل فاصله بین هر شماره حداقل {int(MIN_PHONE_DELAY_SEC)} ثانیه است",
                )
            fields["delay_max"] = float(body.delay_max)
        if not fields:
            return account
        if "delay_min" in fields or "delay_max" in fields:
            delay_min = float(fields.get("delay_min", account.get("delay_min") or 25))
            delay_max = float(fields.get("delay_max", account.get("delay_max") or 45))
            lo, hi = clamp_phone_delays(delay_min, delay_max)
            fields["delay_min"] = lo
            fields["delay_max"] = hi
            if delay_max < delay_min:
                raise HTTPException(400, "بیشترین تأخیر نباید از کمترین کمتر باشد")
        updated = db.update_browser_account(account_id, **fields)
        hub.publish("browser_account", updated)
        return updated

    @app.post("/api/browser-accounts/{account_id}/open-login")
    async def open_browser_account_login(account_id: int) -> dict[str, Any]:
        account = db.get_browser_account(account_id)
        if not account:
            raise HTTPException(404, "اکانت پیدا نشد")
        label = account.get("label") or f"اکانت {account_id}"
        try:
            # Close other profiles first — otherwise VNC still shows the previous Chromium.
            session = await browsers.focus(account_id)
            await session.open_login(label=str(label))
        except Exception as exc:
            raise HTTPException(500, f"باز کردن Chromium ناموفق بود: {exc}") from exc
        db.update_browser_account(account_id, status="needs_login", last_error=None)
        return {
            "ok": True,
            "message": f"Chromium اکانت «{label}» باز شد (پنجره‌های دیگر بسته شد). در VNC وارد شو، بعد تأیید اتصال.",
            "vnc_url": browsers.vnc_url,
            "account": db.get_browser_account(account_id),
            "profile_dir": str(browsers.profile_dir(account_id)),
        }

    @app.post("/api/browser-accounts/{account_id}/verify")
    async def verify_browser_account(account_id: int) -> dict[str, Any]:
        account = db.get_browser_account(account_id)
        if not account:
            raise HTTPException(404, "اکانت پیدا نشد")
        try:
            session = await browsers.focus(account_id)
            result = await session.verify_login()
        except Exception as exc:
            raise HTTPException(500, f"بررسی لاگین ناموفق بود: {exc}") from exc
        if result.get("needs_captcha"):
            hub.publish("captcha", browsers.note_captcha(account_id))
            updated = db.update_browser_account(
                account_id, status="needs_login", last_error="captcha"
            )
            return {"ok": False, "needs_captcha": True, "account": updated, **browsers.captcha_status()}
        if not result.get("logged_in"):
            updated = db.update_browser_account(
                account_id, status="needs_login", phone=None, last_error="not logged in"
            )
            hub.publish("browser_account", updated)
            return {"ok": False, "logged_in": False, "account": updated}
        phone = result.get("phone")
        label = account["label"]
        if phone and (not label or label.startswith("اکانت")):
            label = phone
        updated = db.update_browser_account(
            account_id,
            status="active",
            phone=phone,
            label=label,
            last_error=None,
        )
        hub.publish("browser_account", updated)
        return {"ok": True, "logged_in": True, "account": updated}

    @app.delete("/api/browser-accounts/{account_id}")
    async def delete_browser_account(account_id: int) -> dict[str, bool]:
        if not db.get_browser_account(account_id):
            raise HTTPException(404, "اکانت پیدا نشد")
        await browsers.close(account_id)
        db.delete_browser_account(account_id)
        hub.publish("browser_account", {"id": account_id, "deleted": True})
        return {"ok": True}

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
        cookies = (payload or {}).get("_cookies") if isinstance(payload, dict) else None
        token = extract_token(payload, cookies if isinstance(cookies, dict) else None)
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
