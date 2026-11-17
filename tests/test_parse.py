import json
from pathlib import Path

from app.accounts import AccountView, pick_account
from app.divar.client import auth_headers, browser_headers, session_cookies
from app.divar.parse import (
    canonical_phones,
    classify_contact,
    extract_token,
    matches_keywords,
    normalize_phone_input,
    pagination_state,
    parse_detail,
    phones_from_contact,
    parse_search_rows,
    search_body,
)

FIX = Path(__file__).parent / "fixtures"


def test_search_rows_and_pagination_from_fixture():
    payload = json.loads((FIX / "search_page1.json").read_text(encoding="utf-8"))
    rows = parse_search_rows(payload)
    assert len(rows) == 2
    assert rows[0]["token"] == "gam3eb18"
    assert rows[0]["district"] == "ونک"
    assert "کاشت" in rows[0]["title"]
    data, has_next = pagination_state(payload)
    assert has_next is True
    assert data["page"] == 1
    body = search_body("1", "beauty-and-haircare", None, data)
    assert body["pagination_data"]["page"] == 1
    assert body["city_ids"] == ["1"]
    assert "query" not in body["search_data"]
    queried = search_body("1", "beauty-and-haircare", "آرایشگاه", None)
    assert queried["search_data"]["query"] == "آرایشگاه"


def test_pagination_stops_when_has_next_is_false():
    payload = {"list_widgets": [], "pagination": {"has_next_page": False, "data": {"page": 3}}}
    data, has_next = pagination_state(payload)
    assert has_next is False
    assert data["page"] == 3
    assert parse_search_rows({"list_widgets": [{"widget_type": "TITLE", "data": {}}]}) == []


def test_detail_fixture():
    payload = json.loads((FIX / "detail_sample.json").read_text(encoding="utf-8"))
    parsed = parse_detail("gam3eb18", payload)
    assert parsed["contact_uuid"]
    assert parsed["description"]
    assert parsed["breadcrumb"]
    assert parsed["specs"]


def test_phone_parser_is_defensive():
    raw = "تماس: ۰۹۱۲۳۴۵۶۷۸۹ و +989121112233"
    assert canonical_phones(raw) == ["09123456789", "09121112233"]
    assert classify_contact(200, '{"widget_list":[{"text":"09120000000"}]}')["kind"] == "phones"
    assert classify_contact(200, "شماره‌ای ثبت نشده")["kind"] == "empty"
    assert classify_contact(200, "محدودیت روزانه")["kind"] == "limited"
    assert classify_contact(401, "LOGIN_REQUIRED")["kind"] == "expired"
    assert classify_contact(403, "blocked")["kind"] == "limited"
    assert classify_contact(429, "rate")["kind"] == "limited"
    assert classify_contact(404, "")["kind"] == "missing"
    assert classify_contact(500, "oops")["kind"] == "retry"
    assert canonical_phones("شماره۰۹۱۲۳۴۵۶۷۸۹") == ["09123456789"]
    assert canonical_phones("۰۹۱۲-۳۴۵-۶۷۸۹") == ["09123456789"]
    widget = json.dumps(
        {
            "widget_list": [
                {
                    "widget_type": "UNEXPANDABLE_ROW",
                    "data": {
                        "title": "شمارهٔ موبایل",
                        "value": "۰۹۳۰۵۳۵۲۳۲۰",
                        "action": {
                            "type": "CALL_PHONE",
                            "payload": {"phone_number": "09305352320"},
                        },
                    },
                }
            ]
        },
        ensure_ascii=False,
    )
    assert phones_from_contact(widget) == ["09305352320"]
    assert classify_contact(200, widget)["kind"] == "phones"


def test_auth_token_and_phone_normalize():
    assert extract_token({"token": "abc"}) == "abc"
    assert extract_token({"access_token": "xyz"}) == "xyz"
    assert extract_token({"sAccessToken": "web-jwt"}) == "web-jwt"
    assert extract_token({}, {"sAccessToken": "cookie-jwt"}) == "cookie-jwt"
    assert extract_token({}) is None
    assert normalize_phone_input("۰۹۱۲۱۲۳۴۵۶۷") == "09121234567"
    assert normalize_phone_input("+989121234567") == "09121234567"
    assert matches_keywords("سالن زیبایی ونک", ["سالن"])
    assert not matches_keywords("املاک", ["آرایشگاه"])


def test_parse_divar_search_url_and_aliases():
    from app.divar.urls import (
        build_search_list_url,
        extract_search_from_preloaded,
        parse_divar_search_url,
        resolve_category_slug,
        resolve_city_slug,
    )

    parsed = parse_divar_search_url("https://divar.ir/s/iran/beauty-services")
    assert parsed["city_slug"] == "iran"
    assert parsed["category_slug"] == "beauty-services"
    assert resolve_category_slug([], "beauty-services") == "beauty-and-haircare"
    assert resolve_city_slug([], "iran")["id"] == "715"
    assert (
        build_search_list_url(city_slug="tehran", category="beauty-and-haircare", keywords=["کاشت"])
        == "https://divar.ir/s/tehran/beauty-and-haircare?q=%DA%A9%D8%A7%D8%B4%D8%AA"
    )
    assert build_search_list_url(
        city_slug="x",
        source_url="https://divar.ir/s/iran/beauty-services",
    ) == "https://divar.ir/s/iran/beauty-services"

    html = """
    <script>window.__PRELOADED_STATE__ = {
      "search": {"searchData": {"formData": {"category": {"str": {"value": "beauty-and-haircare"}}}}},
      "multiCity": {"selectedCities": [{"id": 715, "name": "کل ایران", "slug": "iran"}]}
    };</script>
    """
    live = extract_search_from_preloaded(html)
    assert live["city_id"] == "715"
    assert live["category"] == "beauty-and-haircare"


def test_round_robin_skips_blocked_accounts():
    today = "2026-11-01"
    accounts = [
        AccountView(1, "expired", 0, 50, today, 20, 40),
        AccountView(2, "limited", 10, 50, today, 20, 40),
        AccountView(3, "active", 50, 50, today, 20, 40),
        AccountView(4, "active", 1, 50, today, 20, 40),
        AccountView(5, "active", 0, 50, today, 20, 40),
    ]
    first, cursor = pick_account(accounts, 0, today)
    second, cursor = pick_account(accounts, cursor, today)
    third, _ = pick_account(accounts, cursor, today)
    assert [first.id, second.id, third.id] == [4, 5, 4]
    none, _ = pick_account(accounts[:3], 0, today)
    assert none is None
    refreshed = AccountView(2, "active", 10, 50, "2026-10-31", 20, 40)
    picked, _ = pick_account([refreshed], 0, today)
    assert picked.id == 2


def test_browser_session_looks_like_divar_web():
    headers = browser_headers(listing_token="ga5zZ8ke")
    assert headers["Origin"] == "https://divar.ir"
    assert headers["Referer"] == "https://divar.ir/v/ga5zZ8ke"
    assert headers["x-render-type"] == "CSR"
    assert headers["x-web-serving-mode"] == "desktop"
    cookies = session_cookies("device-1", "jwt-token")
    assert cookies["did"] == "device-1"
    assert cookies["sAccessToken"] == "jwt-token"
    assert auth_headers("jwt-token")["Authorization"] == "Basic jwt-token"


def test_send_report_payloads():
    from app.divar.reports import contact_api_report, post_detail_boot_reports

    boots = post_detail_boot_reports("view-1")
    assert len(boots) == 3
    assert boots[0]["reports"][0]["user_event"]["web_page_load_event"]["context"]["page"] == "POST_DETAIL"
    assert boots[1]["reports"][0]["user_event"]["web_page_load_event"]["load_status"]["status"] == "BOOT"
    assert boots[2]["reports"][0]["user_event"]["web_page_load_event"]["load_status"]["status"] == "READY"
    report = contact_api_report("ga5zZ8ke", 200, 200)
    assert "contact_info_v2/ga5zZ8ke" in report["reports"][0]["api_call"]["path"]
    assert report["reports"][0]["api_call"]["status_code"] == 200


def test_traffic_buffer_redacts_tokens():
    from app.divar.traffic import TrafficBuffer, redact

    assert "***" in redact("Authorization: Basic eyJhbGciOi.secret")
    buffer = TrafficBuffer(limit=2)
    buffer.record("POST", "https://api.divar.ir/v8/x", 200, 12, "Basic abc.def")
    assert "***" in buffer.recent()[0]["note"]
    buffer.record("GET", "https://api.divar.ir/v8/y", 404, 5, "ok")
    buffer.record("GET", "https://api.divar.ir/v8/z", 200, 3, "ok")
    rows = buffer.recent()
    assert len(rows) == 2
    assert rows[0]["url"].endswith("/y")
    assert rows[1]["url"].endswith("/z")
