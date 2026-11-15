"""HTTP client for the unofficial Divar web API."""
from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from typing import Any

import httpx

from app.divar.parse import search_body
from app.divar.reports import contact_api_report, post_detail_boot_reports

SEARCH_URL = "https://api.divar.ir/v8/postlist/w/search"
POST_URL = "https://api.divar.ir/v8/posts-v2/web/{token}"
AUTH_URL = "https://api.divar.ir/v5/auth/authenticate"
CONFIRM_URL = "https://api.divar.ir/v5/auth/confirm"
CONTACT_URL = "https://api.divar.ir/v8/postcontact/web/contact_info_v2/{token}"
REPORT_URL = "https://api.divar.ir/v1/client-exporter/send-report"
CITIES_URL = "https://api.divar.ir/v8/places/cities"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)


class RateLimiter:
    def __init__(self, rps: float = 1.0) -> None:
        self.rps = max(0.05, rps)
        self._next = 0.0
        self._lock = asyncio.Lock()

    def set_rps(self, rps: float) -> None:
        self.rps = max(0.05, rps)

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = time.monotonic() + (1.0 / self.rps)

    def backoff(self, seconds: float) -> None:
        self._next = max(self._next, time.monotonic() + seconds)


class DivarClient:
    def __init__(self, timeout: float = 25.0) -> None:
        self.timeout = timeout
        self._did = str(uuid.uuid4())

    def _client(
        self,
        proxy: str | None = None,
        auth_token: str | None = None,
        listing_token: str | None = None,
    ) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "headers": browser_headers(listing_token=listing_token),
            "cookies": session_cookies(self._did, auth_token),
        }
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    async def search(self, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str]:
        async with self._client() as client:
            response = await client.post(SEARCH_URL, json=body)
            return _json_response(response)

    async def detail(self, token: str) -> tuple[int, dict[str, Any] | None, str]:
        await self.send_post_detail_reports(token)
        async with self._client(listing_token=token) as client:
            response = await client.get(POST_URL.format(token=token))
            return _json_response(response)

    async def send_report(
        self,
        payload: dict[str, Any],
        listing_token: str | None = None,
        auth_token: str | None = None,
        proxy: str | None = None,
    ) -> int:
        headers = {
            **browser_headers(listing_token=listing_token),
            "Accept": "*/*",
            "Content-Type": "text/plain;charset=UTF-8",
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            async with self._client(
                proxy=proxy, auth_token=auth_token, listing_token=listing_token
            ) as client:
                response = await client.post(REPORT_URL, content=body, headers=headers)
                return response.status_code
        except Exception:
            return 0

    async def send_post_detail_reports(
        self,
        listing_token: str,
        auth_token: str | None = None,
        proxy: str | None = None,
    ) -> None:
        for payload in post_detail_boot_reports():
            await self.send_report(payload, listing_token, auth_token, proxy)

    async def request_code(self, phone: str, proxy: str | None = None) -> tuple[int, str]:
        async with self._client(proxy) as client:
            response = await client.post(AUTH_URL, json={"phone": phone})
            return response.status_code, response.text

    async def confirm_code(
        self, phone: str, code: str, proxy: str | None = None
    ) -> tuple[int, dict[str, Any] | None, str]:
        async with self._client(proxy) as client:
            response = await client.post(CONFIRM_URL, json={"phone": phone, "code": code})
            status, payload, text = _json_response(response)
            cookies = {key: value for key, value in response.cookies.items()}
            if cookies:
                data = dict(payload or {})
                data["_cookies"] = cookies
                if cookies.get("sAccessToken") and "sAccessToken" not in data:
                    data["sAccessToken"] = cookies["sAccessToken"]
                if cookies.get("token") and "token" not in data:
                    data["token"] = cookies["token"]
                payload = data
            return status, payload, text

    async def contact(
        self,
        token: str,
        auth_token: str,
        contact_uuid: str | None,
        proxy: str | None = None,
    ) -> tuple[int, str]:
        headers = auth_headers(auth_token)
        body = {"contact_uuid": contact_uuid} if contact_uuid else {}
        started = time.monotonic()
        async with self._client(proxy, auth_token=auth_token, listing_token=token) as client:
            response = await client.post(
                CONTACT_URL.format(token=token), json=body, headers=headers
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            await self.send_report(
                contact_api_report(token, response.status_code, max(elapsed_ms, 1)),
                listing_token=token,
                auth_token=auth_token,
                proxy=proxy,
            )
            return response.status_code, response.text

    async def fetch_cities(self) -> tuple[int, Any]:
        async with self._client() as client:
            response = await client.get(CITIES_URL)
            if response.status_code != 200:
                return response.status_code, None
            return 200, response.json()


def browser_headers(listing_token: str | None = None) -> dict[str, str]:
    referer = f"https://divar.ir/v/{listing_token}" if listing_token else "https://divar.ir/"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://divar.ir",
        "Referer": referer,
        "Content-Type": "application/json",
        "sec-ch-ua": '"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "x-web-serving-mode": "desktop",
        "x-screen-size": "1920x1080",
    }
    if listing_token:
        headers["x-render-type"] = "CSR"
    return headers


def session_cookies(device_id: str, auth_token: str | None = None) -> dict[str, str]:
    cookies = {"did": device_id, "theme": "light"}
    if auth_token:
        cookies["sAccessToken"] = auth_token
        cookies["token"] = auth_token
    return cookies


def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": "Basic " + auth_token}


def build_search(
    city_id: str, category: str, query: str | None, pagination_data: Any
) -> dict[str, Any]:
    return search_body(city_id, category, query, pagination_data)


def jitter(low: float, high: float) -> float:
    low, high = min(low, high), max(low, high)
    return random.uniform(low, high)


def _json_response(response: httpx.Response) -> tuple[int, dict[str, Any] | None, str]:
    payload = None
    if response.status_code == 200:
        try:
            data = response.json()
            payload = data if isinstance(data, dict) else None
        except Exception:
            payload = None
    return response.status_code, payload, response.text
