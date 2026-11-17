"""Anti-captcha rate limits for human Chromium phone reveals."""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.divar.client import jitter

TEHRAN = ZoneInfo("Asia/Tehran")

MIN_PHONE_DELAY_SEC = 20.0
DEFAULT_DELAY_MIN = 25.0
DEFAULT_DELAY_MAX = 45.0
DEFAULT_DAILY_CAP = 80
MAX_DAILY_CAP = 150

DEFAULT_REVEALS_PER_10_MIN = 5
DEFAULT_REVEALS_PER_HOUR = 20
MAX_REVEALS_PER_10_MIN = 12
MAX_REVEALS_PER_HOUR = 40

DEFAULT_BATCH_PAUSE_SEC = 600.0  # 10 minutes
BATCH_PAUSE_JITTER = 0.12

DEFAULT_CAPTCHA_COOLDOWN_SEC = 45 * 60
MIN_CAPTCHA_COOLDOWN_SEC = 30 * 60
MAX_CAPTCHA_COOLDOWN_SEC = 60 * 60

DEFAULT_EGRESS_DAILY_CAP = 50
MAX_EGRESS_DAILY_CAP = 150
DEFAULT_EGRESS_CAPTCHA_COOLDOWN_SEC = 90 * 60
MIN_EGRESS_CAPTCHA_COOLDOWN_SEC = 30 * 60
MAX_EGRESS_CAPTCHA_COOLDOWN_SEC = 6 * 60 * 60

REFRESH_BACKOFF_START_SEC = 5 * 60
REFRESH_BACKOFF_MAX_SEC = 30 * 60
REFRESH_BACKOFF_JITTER = 0.1


def clamp_phone_delays(
    delay_min: float | None,
    delay_max: float | None,
    *,
    fallback_min: float = DEFAULT_DELAY_MIN,
    fallback_max: float = DEFAULT_DELAY_MAX,
) -> tuple[float, float]:
    lo = float(delay_min if delay_min is not None else fallback_min)
    hi = float(delay_max if delay_max is not None else fallback_max)
    lo = max(MIN_PHONE_DELAY_SEC, lo)
    hi = max(lo, hi, MIN_PHONE_DELAY_SEC)
    if hi < lo:
        hi = lo
    return lo, hi


def clamp_daily_cap(value: int | float | None, *, default: int = DEFAULT_DAILY_CAP) -> int:
    cap = int(value if value is not None else default)
    return max(1, min(cap, MAX_DAILY_CAP))


def clamp_setting_int(
    value: float | int | None,
    *,
    default: int,
    upper: int,
    lower: int = 1,
) -> int:
    parsed = int(float(value if value is not None else default))
    return max(lower, min(parsed, upper))


def clamp_captcha_cooldown_sec(value: float | int | None) -> float:
    parsed = float(value if value is not None else DEFAULT_CAPTCHA_COOLDOWN_SEC)
    return max(MIN_CAPTCHA_COOLDOWN_SEC, min(parsed, MAX_CAPTCHA_COOLDOWN_SEC))


def clamp_egress_daily_cap(value: int | float | None, *, default: int = DEFAULT_EGRESS_DAILY_CAP) -> int:
    cap = int(value if value is not None else default)
    return max(1, min(cap, MAX_EGRESS_DAILY_CAP))


def clamp_egress_captcha_cooldown_sec(value: float | int | None) -> float:
    parsed = float(value if value is not None else DEFAULT_EGRESS_CAPTCHA_COOLDOWN_SEC)
    return max(
        MIN_EGRESS_CAPTCHA_COOLDOWN_SEC,
        min(parsed, MAX_EGRESS_CAPTCHA_COOLDOWN_SEC),
    )


def egress_rate_blocked(
    egress_row: dict[str, Any] | None,
    reveals_10m: int,
    reveals_1h: int,
    *,
    cap_10m: int,
    cap_1h: int,
    views_today: int,
    daily_cap: int,
    now: datetime | None = None,
) -> str | None:
    now = now or tehran_now()
    if views_today >= daily_cap:
        return "egress_daily"
    until = parse_iso((egress_row or {}).get("captcha_cooldown_until"))
    if until and now < until:
        return "egress_cooldown"
    if reveals_10m >= cap_10m:
        return "egress_rolling_10m"
    if reveals_1h >= cap_1h:
        return "egress_rolling_1h"
    return None


def phone_reveal_wait(
    row: dict[str, Any] | None,
    settings: dict[str, str],
) -> float:
    global_min = float(settings.get("delay_min") or DEFAULT_DELAY_MIN)
    global_max = float(settings.get("delay_max") or DEFAULT_DELAY_MAX)
    acc_min = row.get("delay_min") if row else None
    acc_max = row.get("delay_max") if row else None
    lo, hi = clamp_phone_delays(acc_min, acc_max, fallback_min=global_min, fallback_max=global_max)
    lo, hi = clamp_phone_delays(lo, hi)
    return jitter(lo, hi)


def batch_pause_seconds(base_sec: float, *, jitter_ratio: float = BATCH_PAUSE_JITTER) -> float:
    base = max(0.0, float(base_sec))
    if base <= 0:
        return 0.0
    spread = base * jitter_ratio
    return max(0.0, random.uniform(base - spread, base + spread))


def refresh_backoff_seconds(streak: int) -> float:
    """streak=0 → first wait after empty refresh (~5 min)."""
    streak = max(0, int(streak))
    base = REFRESH_BACKOFF_START_SEC * (2**streak)
    base = min(base, REFRESH_BACKOFF_MAX_SEC)
    spread = base * REFRESH_BACKOFF_JITTER
    return random.uniform(base - spread, base + spread)


def tehran_now() -> datetime:
    return datetime.now(TEHRAN)


def tehran_midnight_next(from_dt: datetime | None = None) -> datetime:
    now = from_dt or tehran_now()
    local = now.astimezone(TEHRAN)
    next_day = (local.date() + timedelta(days=1))
    return datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=TEHRAN)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(TEHRAN)
    except ValueError:
        return None


def account_rate_blocked(
    row: dict[str, Any],
    reveals_10m: int,
    reveals_1h: int,
    *,
    cap_10m: int,
    cap_1h: int,
    now: datetime | None = None,
) -> str | None:
    """Return a short reason code when this account must not reveal yet."""
    now = now or tehran_now()
    status = str(row.get("status") or "")
    if status in {"needs_login", "benched"}:
        return status
    until = parse_iso(row.get("captcha_cooldown_until"))
    if until and now < until:
        return "cooldown"
    if status == "captcha":
        return "captcha"
    if reveals_10m >= cap_10m:
        return "rolling_10m"
    if reveals_1h >= cap_1h:
        return "rolling_1h"
    return None


def seconds_until_rolling_slot(
    last_reveal_at: str | None,
    window_sec: int,
    now: datetime | None = None,
) -> float:
    """How long to wait until the oldest reveal in the window can expire (approx)."""
    last = parse_iso(last_reveal_at)
    if not last:
        return 0.0
    now = now or tehran_now()
    elapsed = (now - last).total_seconds()
    return max(0.0, float(window_sec) - elapsed)


def apply_captcha_penalty_fields(
    row: dict[str, Any],
    cooldown_sec: float,
    *,
    today_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute DB fields after a captcha on contact reveal."""
    now = now or tehran_now()
    hits_day = str(row.get("captcha_hits_day") or "")
    hits = int(row.get("captcha_hits_today") or 0)
    if hits_day != today_key:
        hits = 0
    hits += 1
    fields: dict[str, Any] = {
        "captcha_hits_day": today_key,
        "captcha_hits_today": hits,
        "status": "captcha",
        "last_error": "needs captcha on contact",
    }
    if hits >= 2:
        bench_until = tehran_midnight_next(now).isoformat(timespec="seconds")
        fields["captcha_cooldown_until"] = bench_until
        fields["status"] = "benched"
        fields["last_error"] = "second captcha today; benched until Tehran midnight"
    else:
        until = now + timedelta(seconds=cooldown_sec)
        fields["captcha_cooldown_until"] = until.isoformat(timespec="seconds")
    return fields


def account_eligible_for_reveal(
    row: dict[str, Any],
    *,
    today: str,
    reveals_10m: int,
    reveals_1h: int,
    cap_10m: int,
    cap_1h: int,
    views_today: int | None = None,
    daily_cap: int | None = None,
) -> bool:
    from app.accounts import available, account_view_from_row

    view = account_view_from_row(row)
    if views_today is not None:
        view.views_today = views_today
    if daily_cap is not None:
        view.daily_cap = daily_cap
    if not available(view, today):
        return False
    return account_rate_blocked(row, reveals_10m, reveals_1h, cap_10m=cap_10m, cap_1h=cap_1h) is None