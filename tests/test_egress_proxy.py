"""Proxy masking, egress grouping, fingerprint persistence, captcha group pause."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.fingerprint import ensure_fingerprint_json, parse_fingerprint
from app.main import create_app
from app.proxy_util import egress_group, mask_proxy_url
from app.runner import JobManager


def test_mask_proxy_hides_password():
    raw = "http://user:sekret@proxy.example.com:8080"
    masked = mask_proxy_url(raw)
    assert masked is not None
    assert "sekret" not in masked
    assert "user:***" in masked


def test_egress_group_direct_and_proxy():
    assert egress_group(None) == "direct"
    assert egress_group("") == "direct"
    a = egress_group("socks5://u:p@1.2.3.4:1080")
    b = egress_group("socks5://u:p@1.2.3.4:1080")
    c = egress_group("socks5://u2:p@5.6.7.8:1080")
    assert a == b
    assert a != c


def test_fingerprint_stable_until_regenerate(tmp_path):
    app = create_app(tmp_path)
    db = app.state.db
    first = ensure_fingerprint_json(7, None)
    second = ensure_fingerprint_json(7, first)
    assert first == second
    regen = ensure_fingerprint_json(7, first, regenerate=True)
    assert regen != first
    assert parse_fingerprint(regen)


def test_reveal_events_include_egress_group(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        account = client.post("/api/browser-accounts", json={"label": "A"}).json()
        db = app.state.db
        db.update_browser_account(account["id"], status="active", proxy_url="http://u:pw@g.example.com:8080")
        row = db.record_reveal_event(
            account["id"],
            kind="reveal",
            token="abc",
            egress_group_key=egress_group("http://u:pw@g.example.com:8080"),
        )
        assert row["egress_group"].startswith("proxy:")
        events = client.get("/api/reveal-events").json()
        assert events[0]["egress_group"] == row["egress_group"]
        assert "pw" not in json.dumps(events)


def test_api_masks_proxy_on_browser_account(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/browser-accounts",
            json={"label": "P", "proxy_url": "http://ali:secret@10.0.0.1:3128"},
        ).json()
        assert "secret" not in json.dumps(created)
        assert created.get("proxy_url_masked") or "ali:***" in json.dumps(created)
        listed = client.get("/api/browser-accounts").json()[0]
        assert "secret" not in json.dumps(listed)


def test_captcha_does_not_switch_same_egress_group(tmp_path):
    app = create_app(tmp_path)
    manager: JobManager = app.state.manager
    db = app.state.db
    a1 = db.add_browser_account({"label": "one"})
    a2 = db.add_browser_account({"label": "two"})
    db.update_browser_account(a1["id"], status="active", phone="09121111111")
    db.update_browser_account(a2["id"], status="active", phone="09122222222")
    picked = manager._pick_browser_account_excluding(
        int(a1["id"]),
        exclude_egress=egress_group(None),
    )
    assert picked is None


def test_mark_account_captcha_pauses_egress_group(tmp_path):
    app = create_app(tmp_path)
    db = app.state.db
    account = db.add_browser_account({"label": "captcha"})
    db.update_browser_account(account["id"], status="active")
    db.update_settings({"egress_captcha_cooldown_sec": "1800"})

    import asyncio

    asyncio.run(app.state.manager._mark_account_captcha(1, int(account["id"])))
    eg = egress_group(None)
    row = db.get_egress_group(eg)
    assert row and row.get("captcha_cooldown_until")