"""Stable per-account Chromium fingerprints (UA, viewport, locale, timezone)."""
from __future__ import annotations

import hashlib
import json
import random
from typing import Any

LOCALE = "fa-IR"
TIMEZONE = "Asia/Tehran"
ACCEPT_LANGUAGE = "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7"

# Recent Chrome user agents (desktop + mobile), curated for realism.
_PROFILES: list[dict[str, Any]] = [
    {
        "name": "chrome_win",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "platform": "Win32",
        "mobile": False,
        "viewport": {"width": 1366, "height": 768},
        "device_scale_factor": 1.0,
    },
    {
        "name": "chrome_mac",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "platform": "MacIntel",
        "mobile": False,
        "viewport": {"width": 1440, "height": 900},
        "device_scale_factor": 2.0,
    },
    {
        "name": "chrome_linux",
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "platform": "Linux x86_64",
        "mobile": False,
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 1.0,
    },
    {
        "name": "chrome_android",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
        ),
        "platform": "Linux armv81",
        "mobile": True,
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
    },
    {
        "name": "chrome_iphone",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) CriOS/131.0.0.0 Mobile/15E148 Safari/604.1"
        ),
        "platform": "iPhone",
        "mobile": True,
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
    },
]


def _seeded_rng(seed: str) -> random.Random:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def generate_fingerprint(*, account_id: int, regenerate: bool = False, salt: str = "stable") -> dict[str, Any]:
    seed = f"acct:{account_id}:{salt}"
    if regenerate:
        seed = f"{seed}:new:{random.randint(0, 10**9)}"
    rng = _seeded_rng(seed)
    base = _PROFILES[rng.randrange(len(_PROFILES))]
    jitter_w = rng.randint(-24, 24)
    jitter_h = rng.randint(-18, 18)
    viewport = {
        "width": max(320, int(base["viewport"]["width"]) + jitter_w),
        "height": max(480, int(base["viewport"]["height"]) + jitter_h),
    }
    return {
        "profile_name": base["name"],
        "user_agent": base["user_agent"],
        "platform": base["platform"],
        "mobile": bool(base["mobile"]),
        "viewport": viewport,
        "device_scale_factor": float(base["device_scale_factor"]),
        "locale": LOCALE,
        "timezone_id": TIMEZONE,
        "accept_language": ACCEPT_LANGUAGE,
    }


def parse_fingerprint(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("user_agent"):
        return None
    return data


def ensure_fingerprint_json(account_id: int, raw: str | None, *, regenerate: bool = False) -> str:
    if not regenerate:
        existing = parse_fingerprint(raw)
        if existing:
            return json.dumps(existing, ensure_ascii=False)
    fp = generate_fingerprint(account_id=account_id, regenerate=regenerate, salt="stable")
    return json.dumps(fp, ensure_ascii=False)


def fingerprint_from_row(row: dict[str, Any], *, regenerate: bool = False) -> dict[str, Any]:
    account_id = int(row["id"])
    raw = row.get("fingerprint_json")
    if not regenerate:
        parsed = parse_fingerprint(raw if isinstance(raw, str) else None)
        if parsed:
            return parsed
    fp = generate_fingerprint(account_id=account_id, regenerate=regenerate)
    return fp


def playwright_context_options(fp: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_agent": fp["user_agent"],
        "viewport": dict(fp["viewport"]),
        "device_scale_factor": float(fp.get("device_scale_factor") or 1.0),
        "locale": fp.get("locale") or LOCALE,
        "timezone_id": fp.get("timezone_id") or TIMEZONE,
        "is_mobile": bool(fp.get("mobile")),
        "extra_http_headers": {"Accept-Language": fp.get("accept_language") or ACCEPT_LANGUAGE},
    }


def platform_init_script(platform: str) -> str:
    safe = (platform or "Win32").replace("\\", "\\\\").replace("'", "\\'")
    return f"""() => {{
  try {{
    Object.defineProperty(navigator, 'platform', {{ get: () => '{safe}' }});
  }} catch (e) {{}}
}}"""