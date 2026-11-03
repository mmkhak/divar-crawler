"""HTTP client for the unofficial Divar web API."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from app.divar.parse import search_body

SEARCH_URL = "https://api.divar.ir/v8/postlist/w/search"
POST_URL = "https://api.divar.ir/v8/posts-v2/web/{token}"
AUTH_URL = "https://api.divar.ir/v5/auth/authenticate"
CONFIRM_URL = "https://api.divar.ir/v5/auth/confirm"
CONTACT_URL = "https://api.divar.ir/v8/postcontact/web/contact_info_v2/{token}"
CITIES_URL = "https://api.divar.ir/v8/places/cities"
UA = "Mozilla/5.0 (compatible; DivarPanel/1.0)"


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

    def _client(self, proxy: str | None = None) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "headers": {"User-Agent": UA, "Content-Type": "application/json"},
        }
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    async def search(self, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str]:
        async with self._client() as client:
            response = await client.post(SEARCH_URL, json=body)
            return _json_response(response)

    async def detail(self, token: str) -> tuple[int, dict[str, Any] | None, str]:
        async with self._client() as client:
            response = await client.get(POST_URL.format(token=token))
            return _json_response(response)

    async def request_code(self, phone: str, proxy: str | None = None) -> tuple[int, str]:
        async with self._client(proxy) as client:
            response = await client.post(AUTH_URL, json={"phone": phone})
            return response.status_code, response.text

    async def confirm_code(
        self, phone: str, code: str, proxy: str | None = None
    ) -> tuple[int, dict[str, Any] | None, str]:
        async with self._client(proxy) as client:
            response = await client.post(CONFIRM_URL, json={"phone": phone, "code": code})
            return _json_response(response)

    async def contact(
        self,
        token: str,
        auth_token: str,
        contact_uuid: str | None,
        proxy: str | None = None,
    ) -> tuple[int, str]:
        headers = {
            "Authorization": "Basic " + auth_token,
            "Origin": "https://divar.ir",
            "Referer": f"https://divar.ir/v/{token}",
        }
        body = {"contact_uuid": contact_uuid} if contact_uuid else {}
        async with self._client(proxy) as client:
            response = await client.post(
                CONTACT_URL.format(token=token), json=body, headers=headers
            )
            return response.status_code, response.text

    async def fetch_cities(self) -> tuple[int, Any]:
        async with self._client() as client:
            response = await client.get(CITIES_URL)
            if response.status_code != 200:
                return response.status_code, None
            return 200, response.json()


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
