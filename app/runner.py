"""Background crawl workers.

With fetch_phones: Chromium opens the Divar list URL, scrolls, opens ads one by one,
and reveals contact — like a person. Without phones: anonymous search API only.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import os

from app.accounts import AccountView, account_view_from_row, available
from app.catalog import load_cities
from app.db import Database, today_key
from app.divar.browser import BrowserPool
from app.divar.client import DivarClient, RateLimiter, build_search
from app.divar.parse import (
    canonical_phones,
    matches_keywords,
    pagination_state,
    parse_search_rows,
    snippet,
)
from app.divar.traffic import TrafficBuffer
from app.divar.urls import build_search_list_url
from app.hub import EventHub
from app.notify import notify_captcha
from app.proxy_util import egress_group as egress_group_key
from app.proxy_util import scrub_secrets
from app.rate_policy import (
    DEFAULT_REVEALS_PER_10_MIN,
    DEFAULT_REVEALS_PER_HOUR,
    account_rate_blocked,
    apply_captcha_penalty_fields,
    batch_pause_seconds,
    clamp_captcha_cooldown_sec,
    clamp_egress_captcha_cooldown_sec,
    clamp_egress_daily_cap,
    clamp_setting_int,
    egress_rate_blocked,
    parse_iso,
    phone_reveal_wait,
    refresh_backoff_seconds,
    tehran_now,
)

HUMAN_EMPTY_SCROLL_LIMIT = 4
REFRESH_BACKOFF_MAX_STREAK = 4


class HumanBrowseConflictError(RuntimeError):
    """Another human-browse job is already running on this host/IP."""


def human_list_page_cap(max_pages: int | None) -> int | None:
    """Optional cap on list scroll rounds; 0 or missing means unlimited."""
    value = int(max_pages if max_pages is not None else 0)
    if value <= 0:
        return None
    return value


def fresh_list_ads(ads: list[dict[str, str]], seen: set[str]) -> list[dict[str, str]]:
    return [ad for ad in ads if ad.get("token") and ad["token"] not in seen]


def list_exhausted_after_refresh(empty_scrolls: int, list_refreshed: bool) -> bool:
    return empty_scrolls >= HUMAN_EMPTY_SCROLL_LIMIT and list_refreshed


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
        self._human_browse_by_egress: dict[str, int] = {}
        self._job_egress: dict[int, str] = {}
        self._batch_count_by_egress: dict[str, int] = {}

    def human_browse_active_job(self) -> int | None:
        if not self._human_browse_by_egress:
            return None
        return next(iter(self._human_browse_by_egress.values()))

    def human_browse_by_egress(self) -> dict[str, int]:
        return dict(self._human_browse_by_egress)

    def _release_human_browse(self, job_id: int) -> None:
        egress = self._job_egress.pop(job_id, None)
        if egress and self._human_browse_by_egress.get(egress) == job_id:
            self._human_browse_by_egress.pop(egress, None)

    def _claim_human_browse_egress(self, job_id: int, egress: str) -> None:
        holder = self._human_browse_by_egress.get(egress)
        if holder is not None and holder != job_id:
            raise HumanBrowseConflictError(holder)
        prev = self._job_egress.get(job_id)
        if prev and prev != egress and self._human_browse_by_egress.get(prev) == job_id:
            self._human_browse_by_egress.pop(prev, None)
        self._human_browse_by_egress[egress] = job_id
        self._job_egress[job_id] = egress

    def _egress_for_account_id(self, account_id: int) -> str:
        row = self.db.get_browser_account(int(account_id), internal=True) or {}
        return egress_group_key(row.get("proxy_url"))

    def _egress_daily_cap(self) -> int:
        return clamp_egress_daily_cap(int(float(self.db.setting("egress_daily_cap") or 50)))

    def _egress_reveal_counts(self, egress: str) -> tuple[int, int]:
        return (
            self.db.count_reveal_events_for_egress(egress, 600),
            self.db.count_reveal_events_for_egress(egress, 3600),
        )

    def _egress_views_today(self, egress: str) -> int:
        row = self.db.get_egress_group(egress) or {}
        return int(row.get("views_today") or 0)

    def _egress_blocked_reason(self, egress: str) -> str | None:
        cap_10m, cap_1h = self._rate_caps()
        row = self.db.get_egress_group(egress)
        r10, r1h = self._egress_reveal_counts(egress)
        return egress_rate_blocked(
            row,
            r10,
            r1h,
            cap_10m=cap_10m,
            cap_1h=cap_1h,
            views_today=self._egress_views_today(egress),
            daily_cap=self._egress_daily_cap(),
        )

    def _egress_wait_seconds(self, egress: str, reason: str | None = None) -> float:
        if reason in {"egress_rolling_10m", "rolling_10m"}:
            window = 600
            oldest = self.db.oldest_reveal_in_window_egress(egress, window)
        elif reason in {"egress_rolling_1h", "rolling_1h"}:
            window = 3600
            oldest = self.db.oldest_reveal_in_window_egress(egress, window)
        else:
            row = self.db.get_egress_group(egress) or {}
            until = parse_iso(row.get("captcha_cooldown_until"))
            if until:
                return max(0.0, (until - tehran_now()).total_seconds())
            return 0.0
        if not oldest:
            return 5.0
        start = parse_iso(oldest)
        if not start:
            return 5.0
        elapsed = (tehran_now() - start).total_seconds()
        return max(1.0, float(window) - elapsed)

    def _rate_caps(self) -> tuple[int, int]:
        settings = self.db.settings()
        cap_10m = clamp_setting_int(
            settings.get("reveals_per_10_min"),
            default=DEFAULT_REVEALS_PER_10_MIN,
            upper=12,
        )
        cap_1h = clamp_setting_int(
            settings.get("reveals_per_hour"),
            default=DEFAULT_REVEALS_PER_HOUR,
            upper=40,
        )
        return cap_10m, cap_1h

    def refresh_rate(self) -> None:
        self.limiter.set_rps(float(self.db.setting("anon_rps") or 1))

    def start(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job.get("fetch_phones"):
            starter = self._peek_browser_account()
            if starter is not None:
                self._claim_human_browse_egress(job_id, self._egress_for_account_id(starter.id))
        if job["status"] == "done":
            self.db.update_job(job_id, query_index=0, page=0, pagination=None, query_done=0, error=None)
        self.db.release_phone_claims(job_id)
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
        self._release_human_browse(job_id)

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
        finally:
            self._release_human_browse(job_id)

    async def _crawl(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        if job.get("fetch_phones"):
            await self._crawl_human_browse(job_id)
            return
        await self._crawl_api_search(job_id)

    async def _crawl_human_browse(self, job_id: int) -> None:
        """Open list URL in Chromium; collect visible cards, open each ad, reveal contact."""
        self._log(job_id, "شروع خزش انسانی (لیست → جمع‌آوری → آگهی → تماس)")
        while True:
            job = self.db.get_job(job_id)
            if not job or job["status"] in {"stopped"}:
                return
            if not await self._wait_controls(job_id):
                self.db.set_status(job_id, "stopped")
                self._emit_job(job_id)
                return
            account = self._pick_browser_account()
            if account is None:
                self.db.set_status(job_id, "error", "no browser account")
                self._log(job_id, "هیچ اکانت Chromium فعالی نیست", "error")
                self._emit_job(job_id)
                return
            list_url = self._list_url_for_job(job)
            self._log(job_id, f"باز کردن لیست: {list_url}")
            session = self.browsers.get(account.id)
            opened = await session.open_search_list(list_url)
            if opened.get("needs_captcha"):
                account = await self._resolve_captcha_or_switch(job_id, account.id, session=session)
                if account is None:
                    return
                session = self.browsers.get(account.id)
                opened = await session.open_search_list(list_url)
            if opened.get("needs_captcha"):
                await asyncio.sleep(2)
                continue
            if opened.get("needs_login"):
                self.db.update_browser_account(account.id, status="needs_login", last_error="login required")
                self.hub.publish("browser_account", self.db.get_browser_account(account.id))
                self._log(job_id, "اکانت لاگین نیست", "error")
                return
            if opened.get("error"):
                self._log(job_id, f"باز کردن لیست ناموفق: {opened['error']}", "warn")
                await asyncio.sleep(3)
                continue

            seen: set[str] = set(self.db.processed_listing_tokens(job_id))
            empty_scrolls = 0
            scroll_rounds = 0
            page_cap = human_list_page_cap(job.get("max_pages"))
            switch_account = False
            list_finished = False
            list_refreshed = False
            batch_size = max(1, int(float(self.db.setting("phone_batch_size") or 4)))
            batch_pause_base = max(0.0, float(self.db.setting("phone_batch_pause_sec") or 600))
            egress = self._egress_for_account_id(account.id)
            self._claim_human_browse_egress(job_id, egress)
            batch_count = self._batch_count_by_egress.get(egress, 0)
            empty_refresh_streak = 0
            fresh_queue = fresh_list_ads(await session.collect_list_ads(), seen)

            while not list_finished:
                if not await self._wait_controls(job_id):
                    self.db.set_status(job_id, "stopped")
                    self._emit_job(job_id)
                    return
                job = self.db.get_job(job_id) or job
                account_row = self.db.get_browser_account(account.id)
                if not account_row or not available(account_view_from_row(account_row), today_key()):
                    self._log(job_id, "اکانت از دسترس خارج شد؛ تعویض", "warn")
                    switch_account = True
                    break

                batch_added = 0
                while fresh_queue:
                    ad = fresh_queue.pop(0)
                    token = ad["token"]
                    title = ad.get("title") or ""
                    if not matches_keywords(title, job.get("title_filter") or []):
                        seen.add(token)
                        continue
                    record = {
                        "token": token,
                        "title": title,
                        "district": "",
                        "price_text": "",
                        "snippet": snippet(title),
                        "description": None,
                        "phone": None,
                        "phone_status": "pending",
                        "link": ad.get("href") or f"https://divar.ir/v/{token}",
                        "red_text": "",
                        "found_by": "browser-list",
                    }
                    saved, created = self.db.upsert_listing(job_id, record)
                    if created:
                        batch_added += 1
                        self.hub.publish("listing", _public_listing(saved, job["name"]))
                    elif saved.get("phone_status") in {"done", "empty", "skipped"}:
                        seen.add(token)
                        continue
                    saved = self.db.update_listing(saved["id"], phone_status="pending") or saved

                    ready = await self._prepare_account_for_reveal(job_id, account)
                    if ready is None:
                        self.db.set_status(job_id, "error", "no rate-safe browser account")
                        self._log(job_id, "همه اکانت‌ها در کول‌داون یا سقف نرخ‌اند", "error")
                        self._emit_job(job_id)
                        return
                    if ready.id != account.id:
                        account = ready
                        egress = self._egress_for_account_id(account.id)
                        self._claim_human_browse_egress(job_id, egress)
                        batch_count = self._batch_count_by_egress.get(egress, 0)
                        session = self.browsers.get(account.id)
                        await session.open_search_list(list_url)
                        fresh_queue = fresh_list_ads(await session.collect_list_ads(), seen)
                    result = await self._human_open_ad(
                        job_id,
                        saved,
                        account,
                        session,
                        list_url,
                    )
                    if result == "stop":
                        self.db.set_status(job_id, "stopped")
                        self._emit_job(job_id)
                        return
                    if result == "captcha":
                        account = await self._resolve_captcha_or_switch(job_id, account.id, session=session)
                        if account is None:
                            if not await self._wait_for_any_egress_account(job_id):
                                return
                            account = self._pick_browser_account()
                            if account is None:
                                return
                        egress = self._egress_for_account_id(account.id)
                        self._claim_human_browse_egress(job_id, egress)
                        batch_count = self._batch_count_by_egress.get(egress, 0)
                        session = self.browsers.get(account.id)
                        await session.open_search_list(list_url)
                        fresh_queue = fresh_list_ads(await session.collect_list_ads(), seen)
                        break
                    if result == "switch_account":
                        switch_account = True
                        break
                    if result == "ok":
                        seen.add(token)
                        batch_count += 1
                        self._batch_count_by_egress[egress] = batch_count
                    elif result == "error":
                        batch_count += 1
                        self._batch_count_by_egress[egress] = batch_count
                    if result in {"ok", "error"}:
                        if batch_count >= batch_size and batch_pause_base > 0:
                            batch_pause = batch_pause_seconds(batch_pause_base)
                            mins = batch_pause / 60
                            self._log(
                                job_id,
                                f"{batch_size} شماره پشت‌سرهم (گروه {egress})؛ {mins:.0f} دقیقه صبر",
                            )
                            if not await self._sleep_interruptible(job_id, batch_pause):
                                self.db.set_status(job_id, "stopped")
                                self._emit_job(job_id)
                                return
                            batch_count = 0
                            self._batch_count_by_egress[egress] = 0
                            await session.open_search_list(list_url)
                            fresh_queue = fresh_list_ads(await session.collect_list_ads(), seen)
                            break
                if switch_account:
                    break

                cap_label = str(page_cap) if page_cap is not None else "∞"
                self._log(
                    job_id,
                    f"اسکرول {scroll_rounds}/{cap_label} · تازه در دسته {batch_added} · کل دیده {len(seen)}",
                )
                self._emit_job(job_id)

                if page_cap is not None and scroll_rounds >= page_cap:
                    list_finished = True
                    break

                scrolled = await session.scroll_search_list()
                if scrolled.get("needs_captcha"):
                    account = await self._resolve_captcha_or_switch(job_id, account.id, session=session)
                    if account is None:
                        return
                    session = self.browsers.get(account.id)
                    await session.open_search_list(list_url)
                    empty_scrolls = 0
                    list_refreshed = False
                    fresh_queue = fresh_list_ads(await session.collect_list_ads(), seen)
                    continue

                scroll_rounds += 1
                self.db.update_job(job_id, page=scroll_rounds)

                ads = await session.collect_list_ads()
                fresh_queue = fresh_list_ads(ads, seen)
                if fresh_queue:
                    empty_scrolls = 0
                    empty_refresh_streak = 0
                    continue

                empty_scrolls += 1
                if empty_scrolls >= HUMAN_EMPTY_SCROLL_LIMIT:
                    if not list_refreshed:
                        self._log(job_id, "اسکرول خالی؛ رفرش لیست")
                        opened = await session.open_search_list(list_url)
                        if opened.get("needs_captcha"):
                            account = await self._resolve_captcha_or_switch(
                                job_id, account.id, session=session
                            )
                            if account is None:
                                return
                            session = self.browsers.get(account.id)
                            opened = await session.open_search_list(list_url)
                        list_refreshed = True
                        empty_scrolls = 0
                        fresh_queue = fresh_list_ads(await session.collect_list_ads(), seen)
                        if fresh_queue:
                            empty_refresh_streak = 0
                            continue
                    wait_sec = refresh_backoff_seconds(empty_refresh_streak)
                    empty_refresh_streak += 1
                    self._log(
                        job_id,
                        f"آگهی تازه نیست؛ {wait_sec / 60:.0f} دقیقه تا رفرش بعدی (پشت‌سرهم {empty_refresh_streak})",
                    )
                    if not await self._sleep_interruptible(job_id, wait_sec):
                        self.db.set_status(job_id, "stopped")
                        self._emit_job(job_id)
                        return
                    opened = await session.open_search_list(list_url)
                    if opened.get("needs_captcha"):
                        account = await self._resolve_captcha_or_switch(job_id, account.id, session=session)
                        if account is None:
                            return
                        session = self.browsers.get(account.id)
                        await session.open_search_list(list_url)
                    empty_scrolls = 0
                    fresh_queue = fresh_list_ads(await session.collect_list_ads(), seen)
                    if fresh_queue:
                        empty_refresh_streak = 0
                        continue
                    job_loop = bool((self.db.get_job(job_id) or {}).get("loop"))
                    if not job_loop and empty_refresh_streak >= REFRESH_BACKOFF_MAX_STREAK:
                        list_finished = True
                        break
                    continue

            if switch_account:
                await asyncio.sleep(1)
                continue

            fresh = self.db.get_job(job_id)
            if (
                fresh
                and fresh.get("loop")
                and fresh["status"] == "running"
                and await self._wait_controls(job_id)
            ):
                self.db.update_job(
                    job_id,
                    query_index=0,
                    page=0,
                    pagination=None,
                    query_done=0,
                    error=None,
                )
                self._log(job_id, "حلقه دائم: لیست از اول دوباره")
                self._emit_job(job_id)
                await asyncio.sleep(3)
                continue
            if fresh and fresh["status"] == "running":
                self.db.set_status(job_id, "done")
                self._log(job_id, "کار تمام شد" + (" · لیست تمام" if list_finished else ""))
                self._emit_job(job_id)
            return

    def _list_url_for_job(self, job: dict[str, Any]) -> str:
        if job.get("source_url"):
            return str(job["source_url"])
        city_slug = "iran"
        for city in load_cities(self.data_dir):
            if str(city.get("id")) == str(job.get("city_id")):
                city_slug = str(city.get("slug") or city_slug)
                break
            if str(city.get("name")) == str(job.get("city_name") or ""):
                city_slug = str(city.get("slug") or city_slug)
                break
        if str(job.get("city_id")) == "715" or str(job.get("city_name") or "").find("ایران") >= 0:
            city_slug = "iran"
        return build_search_list_url(
            city_slug=city_slug,
            category=str(job.get("category") or "root"),
            keywords=list(job.get("keywords") or []),
        )

    async def _sleep_interruptible(self, job_id: int, seconds: float) -> bool:
        """Sleep in chunks so pause/stop still work. False = stop."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, seconds)
        while True:
            if not await self._wait_controls(job_id):
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(1.0, remaining))

    async def _human_open_ad(
        self,
        job_id: int,
        listing: dict[str, Any],
        account: AccountView,
        session: Any,
        list_url: str,
        pre_wait: float | None = None,
    ) -> str:
        row = self.db.get_browser_account(account.id, internal=True) or {}
        if pre_wait is None:
            wait = phone_reveal_wait(row, self.db.settings())
        else:
            wait = max(0.0, float(pre_wait))
        label = row.get("phone") or row.get("label") or f"#{account.id}"
        self._log(job_id, f"باز کردن آگهی {listing['token']} با {label} بعد از {wait:.0f}ث")
        if wait > 0 and not await self._sleep_interruptible(job_id, wait):
            return "stop"
        if not await self._wait_controls(job_id):
            return "stop"
        try:
            outcome = await session.open_ad_from_list(listing["token"], list_url)
        except Exception as exc:
            self._log(job_id, f"خطای باز کردن آگهی: {exc}", "warn")
            self.db.update_listing(listing["id"], phone_status="pending")
            return "error"
        if outcome.get("needs_captcha"):
            self.db.update_listing(listing["id"], phone_status="pending")
            return "captcha"
        if outcome.get("needs_login"):
            self.db.update_browser_account(account.id, status="needs_login", last_error="login required")
            self.hub.publish("browser_account", self.db.get_browser_account(account.id))
            self._log(job_id, f"اکانت {label} لاگین نیست", "warn")
            self.db.update_listing(listing["id"], phone_status="pending")
            return "switch_account"

        phones = list(outcome.get("phones") or [])
        description = outcome.get("description") or listing.get("description") or ""
        job = self.db.get_job(job_id) or {}
        if job.get("desc_filter") and description:
            if not matches_keywords(description, job["desc_filter"]):
                updated = self.db.update_listing(
                    listing["id"],
                    description=description,
                    snippet=snippet(description or listing.get("snippet") or ""),
                    phone_status="skipped",
                )
                self.hub.publish("listing", _public_listing(updated, job.get("name") or ""))
                return "ok"
        if not phones:
            phones = canonical_phones(description)
        joined = "، ".join(phones)
        phone_status = "done" if phones else "empty"
        updates: dict[str, Any] = {"phone": joined, "phone_status": phone_status}
        if description:
            updates["description"] = description
            updates["snippet"] = snippet(description or listing.get("snippet") or "")
        if outcome.get("specs"):
            updates["specs_json"] = json.dumps(outcome["specs"], ensure_ascii=False)
        updated = self.db.update_listing(listing["id"], **updates)
        self.db.bump_browser_account_view(account.id)
        if phone_status in {"done", "empty"}:
            eg = egress_group_key(row.get("proxy_url"))
            self.db.record_reveal_event(
                account.id,
                kind="reveal",
                job_id=job_id,
                token=str(listing.get("token") or ""),
                egress_group_key=eg,
            )
        self.hub.publish("browser_account", self.db.get_browser_account(account.id))
        if phones:
            self._log(job_id, f"شماره {listing['token']}: {joined} · {label}")
        else:
            self._log(job_id, f"شماره‌ای برای {listing['token']} نبود ({outcome.get('error') or 'خالی'})", "warn")
        self.hub.publish("listing", _public_listing(updated, job.get("name") or ""))
        if not available(account_view_from_row(self.db.get_browser_account(account.id) or {}), today_key()):
            return "switch_account"
        return "ok"

    async def _crawl_api_search(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        queries: list[str | None] = list(job["keywords"]) or [None]
        self._log(job_id, "شروع خزش (API جستجو)")
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
                fresh = self.db.get_job(job_id)
                if (
                    fresh
                    and fresh.get("loop")
                    and fresh["status"] == "running"
                    and await self._wait_controls(job_id)
                ):
                    self.db.update_job(
                        job_id,
                        query_index=0,
                        page=0,
                        pagination=None,
                        query_done=0,
                        error=None,
                    )
                    self._log(job_id, "حلقه دائم: تمام شد؛ از اول دوباره شروع می‌شود")
                    self._emit_job(job_id)
                    await asyncio.sleep(3)
                    continue
                if fresh and fresh["status"] == "running":
                    self.db.set_status(job_id, "done")
                    self._log(job_id, "کار تمام شد")
                    self._emit_job(job_id)
                return
            query = queries[index]
            max_pages = int(job.get("max_pages") or 0)
            if job["query_done"] or (max_pages > 0 and job["page"] >= max_pages):
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
            finished_query = (not has_next) or (max_pages > 0 and new_page >= max_pages)
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
            await self._handle_account_captcha(job_id, account.id)
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

    def _reveal_counts(self, account_id: int) -> tuple[int, int]:
        return (
            self.db.count_reveal_events(account_id, 600),
            self.db.count_reveal_events(account_id, 3600),
        )

    def _account_blocked(
        self,
        row: dict[str, Any],
        account_id: int,
        *,
        r10: int | None = None,
        r1h: int | None = None,
    ) -> str | None:
        cap_10m, cap_1h = self._rate_caps()
        if r10 is None or r1h is None:
            r10, r1h = self._reveal_counts(account_id)
        blocked = account_rate_blocked(row, r10, r1h, cap_10m=cap_10m, cap_1h=cap_1h)
        if blocked:
            return blocked
        egress = egress_group_key(row.get("proxy_url"))
        er10, er1h = self._egress_reveal_counts(egress)
        return self._egress_blocked_reason_with_counts(egress, er10, er1h)

    def _egress_blocked_reason_with_counts(self, egress: str, r10: int, r1h: int) -> str | None:
        cap_10m, cap_1h = self._rate_caps()
        row = self.db.get_egress_group(egress)
        return egress_rate_blocked(
            row,
            r10,
            r1h,
            cap_10m=cap_10m,
            cap_1h=cap_1h,
            views_today=self._egress_views_today(egress),
            daily_cap=self._egress_daily_cap(),
        )

    async def _wait_for_any_egress_account(self, job_id: int, *, max_rounds: int = 500) -> bool:
        """Wait while every egress group is cooling down (panel keeps running)."""
        for _ in range(max_rounds):
            if self._pick_browser_account_for_rate() is not None:
                return True
            waits: list[float] = []
            for group in self.db.list_egress_groups():
                key = str(group.get("egress_group") or "")
                reason = self._egress_blocked_reason(key)
                if reason in {"egress_cooldown", "egress_rolling_10m", "egress_rolling_1h"}:
                    waits.append(self._egress_wait_seconds(key, reason))
            if not waits:
                return self._pick_browser_account() is not None
            wait = min(max(waits), 120.0)
            mins = wait / 60
            self._log(
                job_id,
                f"همه گروه‌های خروجی مکث دارند؛ ~{mins:.0f} دقیقه صبر",
                "warn",
            )
            if not await self._sleep_interruptible(job_id, wait):
                return False
        return False

    def _rolling_wait_seconds(self, account_id: int, reason: str) -> float:
        window = 600 if reason == "rolling_10m" else 3600
        oldest = self.db.oldest_reveal_in_window(account_id, window)
        if not oldest:
            return 5.0
        start = parse_iso(oldest)
        if not start:
            return 5.0
        elapsed = (tehran_now() - start).total_seconds()
        return max(1.0, float(window) - elapsed)

    def _cooldown_wait_seconds(self, row: dict[str, Any]) -> float:
        until = parse_iso(row.get("captcha_cooldown_until"))
        if not until:
            return 0.0
        return max(0.0, (until - tehran_now()).total_seconds())

    async def _prepare_account_for_reveal(
        self,
        job_id: int,
        account: AccountView,
    ) -> AccountView | None:
        cap_10m, cap_1h = self._rate_caps()
        attempts = 0
        while attempts < 24:
            attempts += 1
            row = self.db.get_browser_account(account.id) or {}
            view = account_view_from_row(row)
            if not available(view, today_key()):
                alt = self._pick_browser_account_excluding(account.id)
                if alt is None:
                    return None
                account = alt
                continue
            r10, r1h = self._reveal_counts(account.id)
            blocked = self._account_blocked(row, account.id, r10=r10, r1h=r1h)
            if not blocked:
                return account
            alt = self._pick_browser_account_for_rate(exclude_id=account.id)
            if alt is not None:
                account = alt
                continue
            if blocked in {"rolling_10m", "rolling_1h"}:
                wait = self._rolling_wait_seconds(account.id, blocked)
            elif blocked in {"egress_rolling_10m", "egress_rolling_1h"}:
                egress = egress_group_key(row.get("proxy_url"))
                wait = self._egress_wait_seconds(egress, blocked)
            elif blocked == "egress_cooldown":
                egress = egress_group_key(row.get("proxy_url"))
                wait = self._egress_wait_seconds(egress, blocked)
            elif blocked == "egress_daily":
                wait = 3600.0
            elif blocked == "cooldown":
                wait = self._cooldown_wait_seconds(row)
            elif blocked == "benched":
                wait = self._cooldown_wait_seconds(row)
            else:
                wait = 30.0
            if wait <= 0:
                continue
            self._log(
                job_id,
                f"سقف نرخ اکانت {row.get('label') or account.id}; {wait:.0f}ث صبر",
                "warn",
            )
            if not await self._sleep_interruptible(job_id, min(wait, 120.0)):
                return None
        return None

    def _pick_browser_account_for_rate(
        self,
        exclude_id: int | None = None,
        *,
        exclude_egress: str | None = None,
    ) -> AccountView | None:
        today = today_key()
        candidates: list[AccountView] = []
        for row in self.db.list_browser_accounts():
            internal = self.db.get_browser_account(int(row["id"]), internal=True) or row
            if exclude_id is not None and int(internal["id"]) == int(exclude_id):
                continue
            eg = egress_group_key(internal.get("proxy_url"))
            if exclude_egress and eg == exclude_egress:
                continue
            view = account_view_from_row(internal)
            if not available(view, today):
                continue
            if self._account_blocked(internal, int(internal["id"])):
                continue
            candidates.append(view)
        if not candidates:
            return None
        index = self._account_cursor % len(candidates)
        self._account_cursor = (index + 1) % len(candidates)
        return candidates[index]

    def _active_browser_accounts(self) -> list[AccountView]:
        today = today_key()
        views: list[AccountView] = []
        for row in self.db.list_browser_accounts():
            internal = self.db.get_browser_account(int(row["id"]), internal=True) or row
            view = account_view_from_row(internal)
            if not available(view, today):
                continue
            if self._account_blocked(internal, int(internal["id"])):
                continue
            views.append(view)
        return views

    async def _mark_account_captcha(self, job_id: int, account_id: int) -> str:
        row = self.db.get_browser_account(account_id, internal=True) or {}
        label = str(row.get("phone") or row.get("label") or f"#{account_id}")
        cooldown = clamp_captcha_cooldown_sec(float(self.db.setting("captcha_cooldown_sec") or 2700))
        penalty = apply_captcha_penalty_fields(row, cooldown, today_key=today_key())
        self.db.update_browser_account(account_id, **penalty)
        egress = egress_group_key(row.get("proxy_url"))
        egress_cd = clamp_egress_captcha_cooldown_sec(
            float(self.db.setting("egress_captcha_cooldown_sec") or 5400)
        )
        until = tehran_now() + timedelta(seconds=egress_cd)
        self.db.pause_egress_group(
            egress,
            until_iso=until.isoformat(timespec="seconds"),
            last_error=scrub_secrets("captcha on egress group", row.get("proxy_url")),
        )
        self.db.record_reveal_event(
            account_id,
            kind="captcha",
            job_id=job_id,
            egress_group_key=egress,
        )
        updated = self.db.get_browser_account(account_id)
        self.hub.publish("browser_account", updated)
        status = self.browsers.note_captcha(
            account_id,
            label=str(row.get("label") or label),
            phone=row.get("phone"),
        )
        self.hub.publish("captcha", status)
        try:
            await notify_captcha(
                account_label=str(row.get("label") or label),
                account_phone=row.get("phone"),
                vnc_url=status.get("vnc_url") or self.browsers.vnc_url,
                panel_url=os.environ.get("PANEL_URL") or None,
            )
        except Exception as exc:
            self._log(job_id, f"ارسال تلگرام برای کپچا ناموفق: {exc}", "warn")
        return label

    async def _resolve_captcha_or_switch(
        self,
        job_id: int,
        account_id: int,
        session: Any = None,
    ) -> AccountView | None:
        label = await self._mark_account_captcha(job_id, account_id)
        blocked_egress = egress_group_key(row.get("proxy_url"))
        other = self._pick_browser_account_excluding(account_id, exclude_egress=blocked_egress)
        if other is not None:
            self._log(
                job_id,
                f"کپچا روی اکانت {label}؛ ادامه با اکانت روی خروجی دیگر",
                "warn",
            )
            return other
        wait_left = self._egress_wait_seconds(blocked_egress, "egress_cooldown")
        if wait_left > 0:
            mins = wait_left / 60
            self._log(
                job_id,
                f"گروه خروجی {blocked_egress} تا ~{mins:.0f} دقیقه مکث (کپچا)",
                "warn",
            )
            if not await self._sleep_interruptible(job_id, min(wait_left, 120.0)):
                return None
            alt = self._pick_browser_account_for_rate(exclude_egress=blocked_egress)
            if alt is not None:
                return alt
        self._log(
            job_id,
            (
                f"کپچا روی اکانت {label} — VNC را باز کن، کپچا را حل کن، بعد در پنل «کپچا حل شد» را بزن"
                if self.browsers.vnc_url
                else f"کپچا روی اکانت {label} — در پنجره Chromium حل کن، بعد در پنل «کپچا حل شد» را بزن"
            ),
            "error",
        )
        try:
            await self.browsers.focus(account_id)
        except Exception as exc:
            self._log(job_id, f"نمایش اکانت کپچا در VNC ناموفق: {exc}", "warn")
        waiter = session.wait_captcha_resolved if session is not None else self.browsers.wait_captcha_resolved
        await waiter()
        row = self.db.get_browser_account(account_id) or {}
        wait_left = self._cooldown_wait_seconds(row)
        if wait_left > 0 or str(row.get("status")) == "benched":
            new_status = str(row.get("status") or "cooldown")
            if new_status == "captcha":
                new_status = "cooldown"
            self.db.update_browser_account(
                account_id,
                status=new_status,
                last_error=row.get("last_error") or "post-captcha cooldown",
            )
            self.hub.publish("browser_account", self.db.get_browser_account(account_id))
            self.hub.publish("captcha", self.browsers.captcha_status())
            mins = max(1.0, wait_left) / 60
            self._log(
                job_id,
                f"کپچا حل شد ولی اکانت {label} تا {mins:.0f} دقیقه دیگر کنار گذاشته می‌شود",
                "warn",
            )
            alt = self._pick_browser_account_for_rate(
                exclude_id=account_id,
                exclude_egress=egress_group_key(row.get("proxy_url")),
            )
            return alt
        self.db.update_browser_account(account_id, status="active", last_error=None)
        self.hub.publish("browser_account", self.db.get_browser_account(account_id))
        self.hub.publish("captcha", self.browsers.captcha_status())
        self._log(job_id, f"کپچا اکانت {label} رفع شد؛ ادامه")
        row = self.db.get_browser_account(account_id)
        return account_view_from_row(row) if row else None

    async def _handle_account_captcha(
        self,
        job_id: int,
        account_id: int,
        session: Any = None,
    ) -> None:
        await self._resolve_captcha_or_switch(job_id, account_id, session=session)

    def _pick_browser_account_excluding(
        self,
        exclude_id: int,
        *,
        exclude_egress: str | None = None,
    ) -> AccountView | None:
        accounts = []
        for account in self._active_browser_accounts():
            if account.id == int(exclude_id):
                continue
            if exclude_egress:
                row = self.db.get_browser_account(account.id, internal=True) or {}
                if egress_group_key(row.get("proxy_url")) == exclude_egress:
                    continue
            accounts.append(account)
        if not accounts:
            return None
        index = self._account_cursor % len(accounts)
        self._account_cursor = (index + 1) % len(accounts)
        return accounts[index]

    def _peek_browser_account(self) -> AccountView | None:
        accounts = self._active_browser_accounts()
        if not accounts:
            return None
        index = self._account_cursor % len(accounts)
        return accounts[index]

    def _pick_browser_account(self) -> AccountView | None:
        accounts = self._active_browser_accounts()
        if not accounts:
            return None
        index = self._account_cursor % len(accounts)
        self._account_cursor = (index + 1) % len(accounts)
        return accounts[index]

    async def _drain_phones(
        self,
        job_id: int,
        search_done: asyncio.Event | None = None,
    ) -> None:
        """One continuous worker per active account; they pull from a shared pending queue."""
        job = self.db.get_job(job_id)
        if not job or not job["fetch_phones"]:
            return
        accounts = self._active_browser_accounts()
        if not accounts:
            self._log(
                job_id,
                "هیچ اکانت Chromium فعالی نیست؛ از برگه حساب‌ها چند اکانت وصل کنید",
                "error",
            )
            return
        labels = []
        for account in accounts:
            row = self.db.get_browser_account(account.id) or {}
            labels.append(row.get("phone") or row.get("label") or f"#{account.id}")
        self._log(job_id, f"ورکرهای شماره فعال: {'، '.join(str(x) for x in labels)}")

        async def worker(account: AccountView) -> None:
            while True:
                if not await self._wait_controls(job_id):
                    return
                row = self.db.get_browser_account(account.id)
                if not row or not available(account_view_from_row(row), today_key()):
                    return
                listing = self.db.claim_pending_phone(job_id)
                if listing is None:
                    if search_done is None or search_done.is_set():
                        return
                    await asyncio.sleep(0.4)
                    continue
                result = await self._fetch_phone_with_account(job_id, listing, account)
                if result == "stop":
                    self.db.update_listing(listing["id"], phone_status="pending")
                    return
                if result in {"retry", "error"}:
                    self.db.update_listing(listing["id"], phone_status="pending")
                    latest = self.db.get_browser_account(account.id) or {}
                    if latest.get("status") != "active":
                        return
                    await asyncio.sleep(0.5)

        await asyncio.gather(*(worker(account) for account in accounts))

    async def _fetch_phone_with_account(
        self,
        job_id: int,
        listing: dict[str, Any],
        account: AccountView,
    ) -> str:
        row = self.db.get_browser_account(account.id) or {}
        wait = phone_reveal_wait(row, self.db.settings())
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
            await self._handle_account_captcha(job_id, account.id, session=session)
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
