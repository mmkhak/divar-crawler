"""Unit tests for anti-captcha rate policy helpers."""
from __future__ import annotations

import pytest

from app.rate_policy import (
    DEFAULT_REVEALS_PER_10_MIN,
    apply_captcha_penalty_fields,
    account_rate_blocked,
    clamp_daily_cap,
    clamp_phone_delays,
    refresh_backoff_seconds,
    tehran_now,
    tehran_midnight_next,
)


@pytest.mark.rate_policy_prod
def test_clamp_phone_delays_enforces_minimum():
    lo, hi = clamp_phone_delays(1, 5)
    assert lo >= 20
    assert hi >= lo


def test_clamp_daily_cap_bounds():
    assert clamp_daily_cap(500) == 150
    assert clamp_daily_cap(10) == 10
    assert clamp_daily_cap(None, default=80) == 80


def test_rolling_caps_block_reason():
    row = {"status": "active"}
    assert account_rate_blocked(row, 5, 0, cap_10m=5, cap_1h=20) == "rolling_10m"
    assert account_rate_blocked(row, 0, 20, cap_10m=5, cap_1h=20) == "rolling_1h"
    assert account_rate_blocked(row, 0, 0, cap_10m=5, cap_1h=20) is None


def test_captcha_penalty_second_hit_benches_until_midnight():
    row = {"captcha_hits_today": 1, "captcha_hits_day": "2026-01-01"}
    fields = apply_captcha_penalty_fields(row, 2700, today_key="2026-01-01")
    assert fields["status"] == "benched"
    assert fields["captcha_hits_today"] == 2


def test_captcha_penalty_first_hit_sets_cooldown():
    now = tehran_now()
    row = {"captcha_hits_today": 0, "captcha_hits_day": None}
    fields = apply_captcha_penalty_fields(row, 1800, today_key=now.date().isoformat(), now=now)
    assert fields["status"] == "captcha"
    until = fields["captcha_cooldown_until"]
    assert until
    parsed = tehran_midnight_next(now)
    assert parsed > now


def test_refresh_backoff_grows_and_caps():
    first = refresh_backoff_seconds(0)
    second = refresh_backoff_seconds(1)
    late = refresh_backoff_seconds(10)
    assert 4 * 60 <= first <= 6 * 60
    assert second > first
    assert late <= 31 * 60


def test_human_browse_lock_conflict(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(tmp_path)
    with TestClient(app) as client:
        j1 = client.post(
            "/api/jobs",
            json={"category": "beauty", "fetch_phones": True},
        ).json()["id"]
        j2 = client.post(
            "/api/jobs",
            json={"category": "beauty", "fetch_phones": True},
        ).json()["id"]
        app.state.manager._human_browse_holder = j1
        conflict = client.post(f"/api/jobs/{j2}/start")
        assert conflict.status_code == 409
        assert "خزش انسانی" in conflict.json()["detail"]