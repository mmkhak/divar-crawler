"""Check proxy egress IP without exposing credentials."""
from __future__ import annotations

from typing import Any

import httpx

from app.proxy_util import mask_proxy_url, parse_proxy_url, scrub_secrets


async def fetch_egress_ip(proxy_url: str | None) -> dict[str, Any]:
    masked = mask_proxy_url(proxy_url)
    if not proxy_url or not str(proxy_url).strip():
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get("https://api.ipify.org?format=json")
            response.raise_for_status()
            payload = response.json()
        ip = str(payload.get("ip") or "")
        return {"ok": True, "ip": ip, "proxy_url_masked": None, "egress_group": "direct"}
    parsed = parse_proxy_url(str(proxy_url).strip())
    server = parsed["server"]
    auth = None
    if parsed.get("username"):
        auth = httpx.BasicAuth(parsed["username"], parsed.get("password") or "")
    proxy_mount = server
    try:
        async with httpx.AsyncClient(
            timeout=25.0,
            proxy=proxy_mount,
            auth=auth,
        ) as client:
            response = await client.get("https://api.ipify.org?format=json")
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        safe_msg = scrub_secrets(str(exc)[:200], proxy_url)
        return {
            "ok": False,
            "error": safe_msg,
            "proxy_url_masked": masked,
        }
    return {
        "ok": True,
        "ip": str(data.get("ip") or ""),
        "proxy_url_masked": masked,
    }