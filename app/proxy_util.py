"""Proxy URL parsing, masking, and egress grouping (no secrets in logs)."""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

_DIRECT = "direct"


def egress_group(proxy_url: str | None) -> str:
    cleaned = (proxy_url or "").strip()
    if not cleaned:
        return _DIRECT
  # stable group per full proxy URL (including auth), without storing raw secret in events
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:24]
    host = _proxy_host_label(cleaned)
    return f"proxy:{host}:{digest}"


def egress_group_label(proxy_url: str | None) -> str:
    cleaned = (proxy_url or "").strip()
    if not cleaned:
        return "مستقیم (بدون پروکسی)"
    return _proxy_host_label(cleaned)


def _proxy_host_label(proxy_url: str) -> str:
    try:
        parsed = parse_proxy_url(proxy_url)
        return f"{parsed['server']}"
    except ValueError:
        return "پروکسی نامعتبر"


def mask_proxy_url(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    cleaned = proxy_url.strip()
    if not cleaned:
        return None
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return "***"
    if parsed.username:
        user = unquote(parsed.username)
        if parsed.password:
            return cleaned.replace(
                f"{parsed.username}:{parsed.password}",
                f"{user}:***",
                1,
            ).replace(f"{user}:***", f"{user}:***", 1)
        return cleaned.replace(parsed.username, user, 1)
    # socks5://user:pass@host — urlparse sometimes misses user in scheme-only cases
    m = re.match(r"^(?P<scheme>\w+)://(?P<auth>[^@]+)@(?P<rest>.+)$", cleaned)
    if m and ":" in m.group("auth"):
        user = m.group("auth").split(":", 1)[0]
        return f"{m.group('scheme')}://{user}:***@{m.group('rest')}"
    return cleaned


def scrub_secrets(text: str, proxy_url: str | None) -> str:
    """Remove proxy password fragments from free-form log lines."""
    if not text or not proxy_url:
        return text
    cleaned = proxy_url.strip()
    if not cleaned:
        return text
    out = text
    try:
        parsed = urlparse(cleaned)
        if parsed.password:
            out = out.replace(parsed.password, "***")
        if parsed.username and f"{parsed.username}:{parsed.password or ''}" in cleaned:
            raw_auth = f"{parsed.username}:{parsed.password or ''}"
            out = out.replace(raw_auth, f"{unquote(parsed.username)}:***")
    except Exception:
        pass
    return out


def parse_proxy_url(proxy_url: str) -> dict[str, str]:
    """Playwright-compatible proxy dict: server (+ username/password)."""
    cleaned = proxy_url.strip()
    if not cleaned:
        raise ValueError("empty proxy")
    if "://" not in cleaned:
        cleaned = f"http://{cleaned}"
    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https", "socks5"}:
        raise ValueError(f"unsupported proxy scheme: {scheme}")
    if not parsed.hostname:
        raise ValueError("proxy host missing")
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 1080 if scheme == "socks5" else 80
    server = f"{scheme}://{parsed.hostname}:{port}"
    out: dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


def playwright_proxy(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url or not str(proxy_url).strip():
        return None
    return parse_proxy_url(str(proxy_url).strip())


def public_proxy_fields(proxy_url: str | None) -> dict[str, Any]:
    masked = mask_proxy_url(proxy_url)
    return {
        "proxy_url": masked,
        "proxy_configured": bool((proxy_url or "").strip()),
        "egress_group": egress_group(proxy_url),
        "egress_label": egress_group_label(proxy_url),
    }


def normalize_proxy_input(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None