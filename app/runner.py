"""Background crawl workers. Search stays on the anonymous API;
listing details and phones come from Playwright Chromium (no posts-v2 token API).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.accounts import AccountView, account_view_from_row, available
from app.catalog import load_cities
from app.db import Database, today_key
from app.divar.browser import BrowserPool
from app.divar.client import DivarClient, RateLimiter, build_search, jitter
from app.divar.parse import (
    canonical_phones,
    matches_keywords,
    pagination_state,
    parse_search_rows,
    snippet,
)
from app.divar.traffic import TrafficBuffer
from app.hub import EventHub


class JobManager:
    def __init__(
        self,
        db: Database,
        hub: EventHub,
        data_dir: Any,
        traffic: TrafficBuffer | None = None,
        browsers: BrowserPool | None = None,
    ) -> None:
        self.db = db
        self.hub = hub
        self.data_dir = Path(data_dir)
        self.traffic = traffic or TrafficBuffer()
        self.client = DivarClient(traffic=self.traffic)
        self.browsers = browsers or BrowserPool(self.data_dir / "chrome-profiles", self.traffic)
        self.browser = self.browsers  # captcha / login helpers on pool
        self.limiter = RateLimiter(float(db.setting("anon_rps") or 1))
        self._tasks: dict[int, asyncio.Task] = {}
        self._pause: dict[int, asyncio.Event] = {}
        self._stop: dict[int, asyncio.Event] = {}
        self._account_cursor = 0

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
                    "phone_status": "pending" if job["fetch_phones"] else "none",
                    "link": f"https://divar.ir/v/{row['token']}",
                    "red_text": row["red_text"],
                    "found_by": label,
                }
                saved, created = self.db.upsert_listing(job_id, record)
                if created:
                    added += 1
                    self.hub.publish("listing", _public_listing(saved, job["name"]))
                elif job["fetch_phones"] and saved.get("phone_status") == "none":
                    saved = self.db.update_listing(saved["id"], phone_status="pending") or saved
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
        """Scrape listing text from Chromium. Never calls posts-v2/{token}."""
        if not await self._wait_controls(job_id):
            return
        # Phones already open the same page; skip a second visit.
        if job["fetch_phones"] and listing.get("phone_status") == "pending":
            return
        account = self._pick_browser_account()
        if account is None:
            self._log(job_id, "برای جزئیات حداقل یک اکانت Chromium وصل‌شده لازم است", "error")
            return
        session = self.browsers.get(account.id)
        try:
            outcome = await session.fetch_listing_info(listing["token"])
        except Exception as exc:
            self._log(job_id, f"جزئیات مرورگر {listing['token']}: {exc}", "warn")
            return
        if outcome.get("needs_captcha"):
            self.hub.publish("captcha", self.browsers.note_captcha(account.id))
            self._log(
                job_id,
                f"کپچا روی اکانت {account.id} — VNC را باز کن و بعد «کپچا حل شد» را بزن",
                "error",
            )
            await self.browsers.wait_captcha_resolved()
            self.hub.publish("captcha", self.browsers.captcha_status())
            await self._fetch_detail(job_id, listing, job)
            return
        if outcome.get("needs_login"):
            self.db.update_browser_account(account.id, status="needs_login", last_error="login required")
            self.hub.publish("browser_account", self.db.get_browser_account(account.id))
            self._log(job_id, f"اکانت {account.id} لاگین نیست", "error")
            return
        description = outcome.get("description") or ""
        subtitle = outcome.get("subtitle") or ""
        specs = outcome.get("specs") or {}
        if job["desc_filter"] and not matches_keywords(description + " " + subtitle, job["desc_filter"]):
            updated = self.db.update_listing(
                listing["id"],
                description=description,
                snippet=snippet(description or listing.get("snippet") or ""),
                phone_status="skipped",
                specs_json=json.dumps(specs, ensure_ascii=False),
            )
            self.hub.publish("listing", _public_listing(updated, job["name"]))
            return
        found = canonical_phones(
            " ".join(
                part
                for part in (
                    description,
                    subtitle,
                    " ".join(str(v) for v in specs.values()),
                )
                if part
            )
        )
        phone_status = listing.get("phone_status") or "none"
        if job["fetch_phones"] and phone_status == "none":
            phone_status = "pending"
        updated = self.db.update_listing(
            listing["id"],
            description=description,
            snippet=snippet(description or subtitle or listing.get("snippet") or ""),
            phone="، ".join(found) if found else listing.get("phone"),
            phone_status=phone_status,
            specs_json=json.dumps(specs, ensure_ascii=False),
            price_text=listing.get("price_text") or "",
        )
        self.hub.publish("listing", _public_listing(updated, job["name"]))

    def _active_browser_accounts(self) -> list[AccountView]:
        today = today_key()
        return [
            view
            for view in (account_view_from_row(row) for row in self.db.list_browser_accounts())
            if available(view, today)
        ]

    def _pick_browser_account(self) -> AccountView | None:
        accounts = self._active_browser_accounts()
        if not accounts:
            return None
        index = self._account_cursor % len(accounts)
        self._account_cursor = (index + 1) % len(accounts)
        return accounts[index]

    async def _drain_phones(self, job_id: int, limit: int | None = None) -> None:
        """Reveal phones with all active Chromium accounts in parallel."""
        job = self.db.get_job(job_id)
        if not job or not job["fetch_phones"]:
            return
        handled = 0
        while limit is None or handled < limit:
            if not await self._wait_controls(job_id):
                return
            accounts = self._active_browser_accounts()
            if not accounts:
                self._log(
                    job_id,
                    "هیچ اکانت Chromium فعالی نیست؛ از برگه حساب‌ها چند اکانت وصل کنید",
                    "error",
                )
                return
            batch = len(accounts)
            if limit is not None:
                batch = min(batch, limit - handled)
            pending = self.db.pending_phones(job_id, limit=batch)
            if not pending:
                return
            pairs: list[tuple[dict[str, Any], AccountView]] = []
            for offset, listing in enumerate(pending):
                account = accounts[(self._account_cursor + offset) % len(accounts)]
                pairs.append((listing, account))
            self._account_cursor = (self._account_cursor + len(pairs)) % len(accounts)
            labels = [
                (self.db.get_browser_account(account.id) or {}).get("phone")
                or (self.db.get_browser_account(account.id) or {}).get("label")
                or f"#{account.id}"
                for _, account in pairs
            ]
            self._log(
                job_id,
                f"دریافت همزمان شماره با {len(pairs)} اکانت: {'، '.join(str(x) for x in labels)}",
            )
            results = await asyncio.gather(
                *[
                    self._fetch_phone_with_account(job_id, listing, account)
                    for listing, account in pairs
                ],
                return_exceptions=True,
            )
            progressed = 0
            for result in results:
                if isinstance(result, Exception):
                    self._log(job_id, f"خطای موازی شماره: {result}", "warn")
                    continue
                if result == "stop":
                    return
                if result in {"done", "empty"}:
                    progressed += 1
            if progressed == 0:
                # Avoid tight loop when every worker only hit login/captcha retry.
                await asyncio.sleep(1)
                if limit is not None and handled == 0:
                    # still allow captcha retries without burning the outer limit forever
                    continue
            handled += progressed

    async def _fetch_phone_with_account(
        self,
        job_id: int,
        listing: dict[str, Any],
        account: AccountView,
    ) -> str:
        row = self.db.get_browser_account(account.id) or {}
        delay_min = float(row.get("delay_min") or self.db.setting("delay_min") or 20)
        delay_max = float(row.get("delay_max") or self.db.setting("delay_max") or 40)
        wait = jitter(delay_min, delay_max)
        label = row.get("phone") or row.get("label") or f"#{account.id}"
        self._log(
            job_id,
            f"شماره با اکانت {label} پس از {wait:.0f} ثانیه · {listing['token']}",
        )
        await asyncio.sleep(wait)
        if not await self._wait_controls(job_id):
            return "stop"
        # Keep other accounts open — parallel use needs all profiles alive.
        session = self.browsers.get(account.id)
        try:
            outcome = await session.reveal_phone(listing["token"])
        except Exception as exc:
            self._log(job_id, f"خطای شماره مرورگر ({label}): {exc}", "warn")
            return "error"
        if outcome.get("needs_captcha"):
            self.hub.publish("captcha", self.browsers.note_captcha(account.id))
            self._log(
                job_id,
                f"کپچا روی اکانت {label} — VNC را باز کن، حل کن، بعد «کپچا حل شد» را بزن",
                "error",
            )
            await session.wait_captcha_resolved()
            self.hub.publish("captcha", self.browsers.captcha_status())
            self._log(job_id, f"کپچا اکانت {label} رفع شد")
            return "retry"
        if outcome.get("needs_login"):
            self.db.update_browser_account(
                account.id, status="needs_login", last_error="login required"
            )
            self.hub.publish("browser_account", self.db.get_browser_account(account.id))
            self._log(job_id, f"اکانت {label} لاگین نیست", "warn")
            return "retry"
        phones = list(outcome.get("phones") or [])
        description = outcome.get("description") or listing.get("description") or ""
        if not phones:
            phones = canonical_phones(
                " ".join(
                    part
                    for part in (
                        listing.get("phone") or "",
                        description,
                        listing.get("snippet") or "",
                        listing.get("specs_json") or "",
                    )
                    if part
                )
            )
        joined = "، ".join(phones)
        phone_status = "done" if phones else "empty"
        updates: dict[str, Any] = {"phone": joined, "phone_status": phone_status}
        if description and not listing.get("description"):
            updates["description"] = description
            updates["snippet"] = snippet(description or listing.get("snippet") or "")
        if outcome.get("specs"):
            updates["specs_json"] = json.dumps(outcome["specs"], ensure_ascii=False)
        updated = self.db.update_listing(listing["id"], **updates)
        self.db.bump_browser_account_view(account.id)
        self.hub.publish("browser_account", self.db.get_browser_account(account.id))
        if phones:
            self._log(job_id, f"شماره {listing['token']}: {joined} · اکانت {label}")
        else:
            detail = outcome.get("error") or "خالی"
            self._log(job_id, f"شماره‌ای برای {listing['token']} نبود ({detail})", "warn")
        job = self.db.get_job(job_id)
        self.hub.publish("listing", _public_listing(updated, (job or {}).get("name") or ""))
        return phone_status


def _needs_detail(listing: dict[str, Any], job: dict[str, Any]) -> bool:
    if not job["fetch_details"]:
        return False
    if listing.get("description") is not None:
        return False
    # Phone drain opens the same page and fills description there.
    if job["fetch_phones"] and listing.get("phone_status") == "pending":
        return False
    return True


def _public_listing(row: dict[str, Any], job_name: str) -> dict[str, Any]:
    data = dict(row)
    data["job_name"] = job_name
    return data


def city_name(data_dir: Any, city_id: str) -> str:
    for city in load_cities(data_dir):
        if str(city["id"]) == str(city_id):
            return city["name"]
    return city_id
