"""Unit tests for human browse loop helpers (no live Divar)."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.runner import (
    HUMAN_EMPTY_SCROLL_LIMIT,
    fresh_list_ads,
    human_list_page_cap,
    list_exhausted_after_refresh,
)


def test_human_list_page_cap():
    assert human_list_page_cap(0) is None
    assert human_list_page_cap(None) is None
    assert human_list_page_cap(5) == 5


def test_fresh_list_ads_filters_seen():
    ads = [{"token": "a", "title": "1"}, {"token": "b", "title": "2"}]
    assert fresh_list_ads(ads, set()) == ads
    assert fresh_list_ads(ads, {"a"}) == [{"token": "b", "title": "2"}]


def test_list_exhausted_after_refresh():
    assert list_exhausted_after_refresh(HUMAN_EMPTY_SCROLL_LIMIT, False) is False
    assert list_exhausted_after_refresh(HUMAN_EMPTY_SCROLL_LIMIT, True) is True


def test_processed_tokens_skipped_on_restart(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        account = client.post(
            "/api/browser-accounts",
            json={"label": "A1", "delay_min": 0.01, "delay_max": 0.01},
        ).json()
        app.state.db.update_browser_account(account["id"], status="active", phone="09121111111")
        job_id = client.post(
            "/api/jobs",
            json={"category": "beauty-and-haircare", "fetch_phones": True, "max_pages": 1},
        ).json()["id"]
        app.state.db.upsert_listing(
            job_id,
            {
                "token": "doneTok",
                "title": "قبلی",
                "phone_status": "done",
                "link": "https://divar.ir/v/doneTok",
            },
        )
        opened_ads = []

        async def open_search_list(list_url):
            return {}

        async def collect_list_ads():
            return [
                {"token": "doneTok", "title": "قبلی", "href": "https://divar.ir/v/doneTok"},
                {"token": "newTok", "title": "جدید", "href": "https://divar.ir/v/newTok"},
            ]

        async def scroll_search_list():
            return {}

        async def open_ad_from_list(token, list_url):
            opened_ads.append(token)
            return {
                "phones": ["09120000000"],
                "needs_login": False,
                "needs_captcha": False,
                "description": "",
            }

        session = app.state.browsers.get(account["id"])
        session.open_search_list = open_search_list
        session.collect_list_ads = collect_list_ads
        session.scroll_search_list = scroll_search_list
        session.open_ad_from_list = open_ad_from_list
        app.state.db.update_settings(
            {"delay_min": "0.01", "delay_max": "0.01", "phone_batch_pause_sec": "0"}
        )

        client.post(f"/api/jobs/{job_id}/start")
        for _ in range(80):
            if client.get("/api/jobs").json()[0]["status"] == "done":
                break
            time.sleep(0.05)
        assert opened_ads == ["newTok"]


def test_captcha_switches_account_when_available(tmp_path):
    app = create_app(tmp_path)
    resolve_calls = []

    with TestClient(app) as client:
        a1 = client.post(
            "/api/browser-accounts",
            json={"label": "captcha", "delay_min": 0.01, "delay_max": 0.01},
        ).json()
        a2 = client.post(
            "/api/browser-accounts",
            json={"label": "healthy", "delay_min": 0.01, "delay_max": 0.01},
        ).json()

        async def fake_resolve(job_id, account_id, session=None):
            resolve_calls.append(account_id)
            if account_id == a1["id"]:
                row = app.state.db.get_browser_account(a2["id"])
                from app.accounts import account_view_from_row

                return account_view_from_row(row)
            return None

        app.state.manager._resolve_captcha_or_switch = fake_resolve  # type: ignore[method-assign]
        app.state.db.update_browser_account(a1["id"], status="active", phone="09121111111")
        app.state.db.update_browser_account(
            a2["id"],
            status="active",
            phone="09122222222",
            proxy_url="http://10.0.0.2:3128",
        )
        app.state.manager._account_cursor = 0
        job_id = client.post(
            "/api/jobs",
            json={"category": "beauty-and-haircare", "fetch_phones": True, "max_pages": 1},
        ).json()["id"]

        captcha_once = {"n": 0}

        async def open_ad_from_list(token, list_url):
            if captcha_once["n"] == 0:
                captcha_once["n"] += 1
                return {"needs_captcha": True, "phones": [], "description": ""}
            return {
                "phones": ["09120000000"],
                "needs_login": False,
                "needs_captcha": False,
                "description": "",
            }

        async def open_search_list(list_url):
            return {}

        async def collect_list_ads():
            return [{"token": "t1", "title": "x", "href": "https://divar.ir/v/t1"}]

        async def scroll_search_list():
            return {}

        for acc_id in (a1["id"], a2["id"]):
            session = app.state.browsers.get(acc_id)
            session.open_search_list = open_search_list
            session.collect_list_ads = collect_list_ads
            session.scroll_search_list = scroll_search_list
            session.open_ad_from_list = open_ad_from_list

        app.state.db.update_settings(
            {"delay_min": "0.01", "delay_max": "0.01", "phone_batch_pause_sec": "0"}
        )
        client.post(f"/api/jobs/{job_id}/start")
        for _ in range(120):
            if client.get("/api/jobs").json()[0]["status"] == "done":
                break
            time.sleep(0.05)
        assert a1["id"] in resolve_calls
        rows = client.get("/api/listings").json()
        assert rows and rows[0]["phone_status"] == "done"