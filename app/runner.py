"""Background crawl workers. Anonymous search/detail share one rate limiter;
phone lookups are round-robin across accounts, each with its own delay.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.accounts import AccountView, pick_account
from app.catalog import load_cities
from app.db import Database, today_key
from app.divar.client import DivarClient, RateLimiter, build_search, jitter
from app.divar.parse import (
    canonical_phones,
    classify_contact,
    matches_keywords,
    pagination_state,
    parse_detail,
    parse_search_rows,
    snippet,
)
from app.hub import EventHub


class JobManager:
    def __init__(self, db: Database, hub: EventHub, data_dir: Any) -> None:
        self.db = db
        self.hub = hub
        self.data_dir = data_dir
        self.client = DivarClient()
        self.limiter = RateLimiter(float(db.setting("anon_rps") or 1))
        self._tasks: dict[int, asyncio.Task] = {}
        self._pause: dict[int, asyncio.Event] = {}
        self._stop: dict[int, asyncio.Event] = {}
        self._rr = 0
        self._account_lock = asyncio.Lock()

    def refresh_rate(self) -> None:
        self.limiter.set_rps(float(self.db.setting("anon_rps") or 1))

    def start(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] == "done":
            self.db.update_job(job_id, query_index=0, page=0, pagination=None, query_done=0, error=None)
        self._pause.setdefault(job_id, asyncio.Event()).set()
        self._stop[job_id] = asyncio.Event()
        existing = self._tasks.get(job_id)
        if existing and not existing.done():
            self.db.set_status(job_id, "running")
            self._emit_job(job_id)
            return
        self.db.set_status(job_id, "running", None)
        self._tasks[job_id] = asyncio.create_task(self._run(job_id))
        self._emit_job(job_id)

    def pause(self, job_id: int) -> None:
        self._pause.setdefault(job_id, asyncio.Event()).clear()
        if self.db.get_job(job_id):
            self.db.set_status(job_id, "paused")
            self._emit_job(job_id)

    def resume(self, job_id: int) -> None:
        self.start(job_id)

    async def stop(self, job_id: int) -> None:
        self._stop.setdefault(job_id, asyncio.Event()).set()
        self._pause.setdefault(job_id, asyncio.Event()).set()
        task = self._tasks.get(job_id)
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=8)
            except asyncio.TimeoutError:
                task.cancel()
        if self.db.get_job(job_id):
            self.db.set_status(job_id, "stopped")
            self._emit_job(job_id)

    def resume_interrupted(self) -> None:
        for job_id in self.db.jobs_to_resume():
            self.start(job_id)

    def _emit_job(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        job["counts"] = self.db.job_counts(job_id)
        self.hub.publish("job", job)

    def _log(self, job_id: int, message: str, level: str = "info") -> None:
        entry = self.db.add_log(job_id, level, message)
        self.hub.publish("log", entry)

    async def _wait_controls(self, job_id: int) -> bool:
        """Return False when the job should exit."""
        stop = self._stop.setdefault(job_id, asyncio.Event())
        pause = self._pause.setdefault(job_id, asyncio.Event())
        while not stop.is_set():
            if pause.is_set():
                return True
            self.db.set_status(job_id, "paused")
            await asyncio.sleep(0.3)
        return False

    async def _run(self, job_id: int) -> None:
        try:
            await self._crawl(job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.db.set_status(job_id, "error", str(exc)[:300])
            self._log(job_id, f"خطای کار: {exc}", "error")
            self._emit_job(job_id)

    async def _crawl(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        queries: list[str | None] = list(job["keywords"]) or [None]
        self._log(job_id, "شروع خزش")
        if job["fetch_phones"]:
            await self._drain_phones(job_id)
        while True:
            job = self.db.get_job(job_id)
            if not job or job["status"] in {"stopped"}:
                return
            if not await self._wait_controls(job_id):
                self.db.set_status(job_id, "stopped")
                self._emit_job(job_id)
                return
            index = job["query_index"]
            if index >= len(queries):
                await self._drain_phones(job_id)
                leftover = self.db.pending_phones(job_id, limit=1)
                if leftover:
                    self._log(job_id, "صف شماره خالی نشد؛ کار را دوباره شروع کنید", "warn")
                fresh = self.db.get_job(job_id)
                if fresh and fresh["status"] == "running":
                    self.db.set_status(job_id, "done")
                    self._log(job_id, "کار تمام شد")
                    self._emit_job(job_id)
                return
            query = queries[index]
            if job["query_done"] or job["page"] >= job["max_pages"]:
                self.db.update_job(job_id, query_index=index + 1, page=0, pagination=None, query_done=0)
                continue
            label = query or "کل دسته"
            body = build_search(job["city_id"], job["category"], query, job["pagination"])
            await self.limiter.wait()
            try:
                status, payload, text = await self.client.search(body)
            except Exception as exc:
                self.limiter.backoff(8)
                self._log(job_id, f"خطای شبکه در جستجو: {exc}", "warn")
                await asyncio.sleep(3)
                continue
            if status in {403, 429} or status >= 500:
                self.limiter.backoff(min(60, 5 * (2 if status == 429 else 1)))
                self._log(job_id, f"جستجو با وضعیت {status} کند شد؛ عقب‌نشینی", "warn")
                await asyncio.sleep(2)
                continue
            if status != 200 or payload is None:
                self._log(job_id, f"جستجو ناموفق ({status})", "error")
                self.db.set_status(job_id, "error", f"HTTP {status}")
                self._emit_job(job_id)
                return
            rows = parse_search_rows(payload)
            page_data, has_next = pagination_state(payload)
            added = 0
            for row in rows:
                title_blob = " ".join(
                    part for part in (row["title"], row["list_bottom"], row["price_text"]) if part
                )
                if not matches_keywords(title_blob, job["title_filter"]):
                    continue
                record = {
                    "token": row["token"],
                    "title": row["title"],
                    "district": row["district"],
                    "price_text": row["price_text"],
                    "snippet": snippet(row["list_bottom"] or row["title"]),
                    "description": None,
                    "phone": None,
                    "phone_status": "none",
                    "link": f"https://divar.ir/v/{row['token']}",
                    "red_text": row["red_text"],
                    "found_by": label,
                }
                saved, created = self.db.upsert_listing(job_id, record)
                if created:
                    added += 1
                    self.hub.publish("listing", _public_listing(saved, job["name"]))
                if _needs_detail(saved, job):
                    await self._fetch_detail(job_id, saved, job)
            new_page = job["page"] + 1
            finished_query = (not has_next) or new_page >= job["max_pages"]
            self.db.update_job(
                job_id,
                page=new_page,
                pagination=page_data,
                query_done=1 if finished_query else 0,
            )
            self._log(job_id, f"[{label}] صفحه {new_page}، تازه {added}، ردیف صفحه {len(rows)}")
            self._emit_job(job_id)
            if finished_query:
                self.db.update_job(job_id, query_index=index + 1, page=0, pagination=None, query_done=0)
            await self._drain_phones(job_id, limit=5)

    async def _fetch_detail(self, job_id: int, listing: dict[str, Any], job: dict[str, Any]) -> None:
        if not await self._wait_controls(job_id):
            return
        await self.limiter.wait()
        try:
            status, payload, _text = await self.client.detail(listing["token"])
        except Exception as exc:
            self.limiter.backoff(8)
            self._log(job_id, f"جزئیات {listing['token']}: {exc}", "warn")
            return
        if status in {403, 429, 500, 502, 503}:
            self.limiter.backoff(12 if status in {403, 429} else 6)
            self._log(job_id, f"جزئیات کند شد ({status})؛ فاصله بیشتر", "warn")
            return
        if status == 404 or payload is None:
            self.db.update_listing(listing["id"], description="", phone_status="skipped")
            return
        parsed = parse_detail(listing["token"], payload)
        description = parsed["description"] or ""
        if job["desc_filter"] and not matches_keywords(
            description + " " + (parsed["subtitle"] or ""), job["desc_filter"]
        ):
            updated = self.db.update_listing(
                listing["id"],
                description=description,
                snippet=snippet(description or listing.get("snippet") or ""),
                phone_status="skipped",
                contact_uuid=parsed["contact_uuid"],
                specs_json=json.dumps(parsed["specs"], ensure_ascii=False),
                breadcrumb=" > ".join(part for part in parsed["breadcrumb"] if part),
            )
            self.hub.publish("listing", _public_listing(updated, job["name"]))
            return
        found = canonical_phones(
            " ".join(
                part
                for part in (
                    description,
                    parsed.get("subtitle") or "",
                    " ".join(str(v) for v in (parsed.get("specs") or {}).values()),
                )
                if part
            )
        )
        phone_status = "none"
        if job["fetch_phones"]:
            phone_status = "pending"
        updated = self.db.update_listing(
            listing["id"],
            description=description,
            snippet=snippet(description or parsed["subtitle"] or listing.get("snippet") or ""),
            contact_uuid=parsed["contact_uuid"],
            phone="، ".join(found) if found else listing.get("phone"),
            phone_status=phone_status,
            specs_json=json.dumps(parsed["specs"], ensure_ascii=False),
            breadcrumb=" > ".join(part for part in parsed["breadcrumb"] if part),
            price_text=listing.get("price_text") or "",
        )
        self.hub.publish("listing", _public_listing(updated, job["name"]))

    async def _drain_phones(self, job_id: int, limit: int | None = None) -> None:
        job = self.db.get_job(job_id)
        if not job or not job["fetch_phones"]:
            return
        handled = 0
        while limit is None or handled < limit:
            if not await self._wait_controls(job_id):
                return
            pending = self.db.pending_phones(job_id, limit=1)
            if not pending:
                return
            listing = pending[0]
            account = await self._next_account()
            if account is None:
                self._log(job_id, "هیچ حساب آماده‌ای برای شماره نیست", "warn")
                return
            wait = jitter(float(account["delay_min"]), float(account["delay_max"]))
            self._log(job_id, f"شماره با حساب {account['phone']} پس از {wait:.0f} ثانیه")
            await asyncio.sleep(wait)
            if not await self._wait_controls(job_id):
                return
            try:
                status, text = await self.client.contact(
                    listing["token"],
                    account["token"],
                    listing.get("contact_uuid"),
                    account.get("proxy"),
                )
            except Exception as exc:
                self._log(job_id, f"خطای شماره: {exc}", "warn")
                return
            outcome = classify_contact(status, text)
            kind = outcome["kind"]
            if kind == "expired":
                self.db.update_account(account["id"], status="expired", last_error="401")
                self._log(job_id, f"حساب {account['phone']} منقضی شد؛ دوباره وارد شوید", "error")
                self.hub.publish("account", {"id": account["id"], "status": "expired"})
                continue
            if kind == "limited":
                self.db.update_account(
                    account["id"],
                    status="limited",
                    views_day=today_key(),
                    last_error=(outcome.get("detail") or "")[:180],
                )
                self._log(job_id, f"حساب {account['phone']} برای امروز محدود شد", "warn")
                self.hub.publish("account", {"id": account["id"], "status": "limited"})
                continue
            if kind == "retry":
                self._log(job_id, f"پاسخ شماره {status}؛ بعداً دوباره", "warn")
                return
            phones = list(outcome["phones"])
            if not phones:
                phones = canonical_phones(
                    " ".join(
                        part
                        for part in (
                            listing.get("phone") or "",
                            listing.get("description") or "",
                            listing.get("snippet") or "",
                            listing.get("specs_json") or "",
                        )
                        if part
                    )
                )
            joined = "، ".join(phones)
            phone_status = "done" if phones else "empty"
            updated = self.db.update_listing(listing["id"], phone=joined, phone_status=phone_status)
            if phones:
                self._log(job_id, f"شماره {listing['token']}: {joined}")
            else:
                self._log(job_id, f"شماره‌ای برای {listing['token']} نبود")
            self.db.update_account(
                account["id"],
                views_today=int(account["views_today"]) + 1,
                views_day=today_key(),
                last_used_at=updated["created_at"],
                status="active",
            )
            self.hub.publish("listing", _public_listing(updated, job["name"]))
            handled += 1

    async def _next_account(self) -> dict[str, Any] | None:
        async with self._account_lock:
            secrets = self.db.account_secrets()
            views = [
                AccountView(
                    id=row["id"],
                    status=row["status"],
                    views_today=row["views_today"],
                    daily_cap=row["daily_cap"],
                    views_day=row["views_day"],
                    delay_min=row["delay_min"],
                    delay_max=row["delay_max"],
                )
                for row in secrets
            ]
            chosen, self._rr = pick_account(views, self._rr, today_key())
            if not chosen:
                return None
            return next(row for row in secrets if row["id"] == chosen.id)


def _needs_detail(listing: dict[str, Any], job: dict[str, Any]) -> bool:
    if listing.get("phone_status") in {"done", "empty", "skipped"}:
        return job["fetch_details"] and listing.get("description") is None
    if job["fetch_phones"] and listing.get("phone_status") != "pending":
        return True
    if listing.get("phone_status") == "pending":
        return False
    return bool(job["fetch_details"]) and listing.get("description") is None


def _public_listing(row: dict[str, Any], job_name: str) -> dict[str, Any]:
    data = dict(row)
    data["job_name"] = job_name
    return data


def city_name(data_dir: Any, city_id: str) -> str:
    for city in load_cities(data_dir):
        if str(city["id"]) == str(city_id):
            return city["name"]
    return city_id
