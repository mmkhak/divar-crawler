"""Playwright Chromium session used only to reveal listing phone numbers."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.divar.parse import canonical_phones, phones_from_contact
from app.divar.traffic import TrafficBuffer


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

LOGIN_URL = "https://divar.ir/my-divar"
POST_URL = "https://divar.ir/v/{token}"
CONTACT_HINT = ("contact_info", "postcontact")
LOGIN_MARKERS = (
    "ورود به دیوار",
    "کد تأیید",
    "شماره موبایل خود را وارد کنید",
    "وارد حساب کاربری خود شوید",
    "برای دریافت اطلاعات تماس",
    "LOGIN_REQUIRED",
)


class BrowserSession:
    def __init__(self, profile_dir: Path, traffic: TrafficBuffer | None = None) -> None:
        self.profile_dir = Path(profile_dir)
        self.traffic = traffic or TrafficBuffer()
        self._playwright = None
        self._context = None
        self._page = None
        self._lock = None

    def _get_lock(self):
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def ensure(self) -> None:
        if self._page is not None:
            return
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        headless = _env_bool("BROWSER_HEADLESS", False)
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=headless,
            viewport={"width": 1365, "height": 900},
            locale="fa-IR",
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    async def close(self) -> None:
        async with self._get_lock():
            if self._context is not None:
                await self._context.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._context = None
            self._playwright = None
            self._page = None

    async def open_login(self) -> None:
        async with self._get_lock():
            await self.ensure()
            assert self._page is not None
            started = time.monotonic()
            await self._page.goto(LOGIN_URL, wait_until="domcontentloaded")
            self.traffic.record(
                "BROWSER",
                LOGIN_URL,
                200,
                int((time.monotonic() - started) * 1000),
                "open login",
            )

    async def reveal_phone(self, token: str) -> dict[str, Any]:
        """Open listing page, click contact, return phones.

        Result keys: phones, needs_login, error
        """
        async with self._get_lock():
            await self.ensure()
            assert self._page is not None
            page = self._page
            url = POST_URL.format(token=token)
            started = time.monotonic()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                self.traffic.record(
                    "BROWSER",
                    url,
                    0,
                    int((time.monotonic() - started) * 1000),
                    f"open fail {exc}",
                )
                return {"phones": [], "needs_login": False, "error": str(exc)[:200]}

            self.traffic.record(
                "BROWSER",
                url,
                200,
                int((time.monotonic() - started) * 1000),
                f"open post {token}",
            )
            await self._dismiss_overlays(page)

            if await self._looks_like_login(page):
                return {"phones": [], "needs_login": True, "error": "login required"}

            try:
                await page.wait_for_selector("button.post-actions__get-contact", timeout=12000)
            except Exception:
                pass

            click_started = time.monotonic()
            response_body = ""
            response_status = 0
            clicked = False
            try:
                async with page.expect_response(
                    lambda r: any(h in r.url for h in CONTACT_HINT),
                    timeout=15000,
                ) as pending:
                    clicked = await self._click_contact(page)
                    if not clicked:
                        raise RuntimeError("contact button not found")
                    response = await pending.value
                    response_status = response.status
                    response_body = await response.text()
            except Exception as exc:
                if not clicked:
                    clicked = await self._click_contact(page)
                if not clicked:
                    self.traffic.record(
                        "BROWSER",
                        url,
                        404,
                        int((time.monotonic() - click_started) * 1000),
                        "contact button missing",
                    )
                    if await self._looks_like_login(page):
                        return {"phones": [], "needs_login": True, "error": "login required"}
                    return {
                        "phones": [],
                        "needs_login": False,
                        "error": f"contact button not found ({exc})",
                    }
                await page.wait_for_timeout(2000)
                response_body = await self._safe_text(page)

            if response_status == 401 or "LOGIN_REQUIRED" in (response_body or ""):
                self.traffic.record(
                    "BROWSER",
                    url,
                    401,
                    int((time.monotonic() - click_started) * 1000),
                    "reveal needs login",
                )
                return {"phones": [], "needs_login": True, "error": "login required"}

            phones = phones_from_contact(response_body) if response_body else []
            if not phones:
                await page.wait_for_timeout(1500)
                tels = await page.eval_on_selector_all(
                    'a[href^="tel:"]',
                    "els => els.map(e => e.getAttribute('href') || '')",
                )
                blob = " ".join(tels) + " " + await self._safe_text(page)
                phones = canonical_phones(blob)

            if await self._looks_like_login(page) and not phones:
                self.traffic.record(
                    "BROWSER",
                    url,
                    401,
                    int((time.monotonic() - click_started) * 1000),
                    "reveal needs login",
                )
                return {"phones": [], "needs_login": True, "error": "login required"}

            self.traffic.record(
                "BROWSER",
                url,
                response_status or (200 if phones else 204),
                int((time.monotonic() - click_started) * 1000),
                f"reveal phone {token}: {len(phones)}",
            )
            return {
                "phones": phones,
                "needs_login": False,
                "error": None if phones else "empty",
            }

    async def _click_contact(self, page: Any) -> bool:
        await self._dismiss_overlays(page)
        selectors = [
            "button.post-actions__get-contact",
            'button.kt-button--primary:has-text("اطلاعات تماس")',
            'button:has-text("اطلاعات تماس")',
            'button:has-text("برو به اطلاعات تماس")',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=4000)
                await locator.scroll_into_view_if_needed(timeout=2000)
                try:
                    await locator.click(timeout=4000)
                except Exception:
                    await locator.click(timeout=4000, force=True)
                return True
            except Exception:
                continue
        return False

    async def _dismiss_overlays(self, page: Any) -> None:
        selectors = [
            'button:has-text("متوجه شدم")',
            'button:has-text("باشه")',
            'button:has-text("بستن")',
            'button:has-text("انصراف")',
            '[aria-label="بستن"]',
            ".kt-new-modal__close",
            ".kt-new-modal button.kt-button--primary",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                if await locator.is_visible():
                    await locator.click(timeout=800)
            except Exception:
                continue

    async def _looks_like_login(self, page: Any) -> bool:
        url = (page.url or "").lower()
        if "auth" in url or "login" in url:
            return True
        text = await self._safe_text(page)
        return any(marker in text for marker in LOGIN_MARKERS)

    async def _safe_text(self, page: Any) -> str:
        try:
            return await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""
