import json
from pathlib import Path

from app.accounts import AccountView, pick_account
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
    assert extract_token({}) is None
    assert normalize_phone_input("۰۹۱۲۱۲۳۴۵۶۷") == "09121234567"
    assert normalize_phone_input("+989121234567") == "09121234567"
    assert matches_keywords("سالن زیبایی ونک", ["سالن"])
    assert not matches_keywords("املاک", ["آرایشگاه"])


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
    refreshed = AccountView(2, "limited", 10, 50, "2026-10-31", 20, 40)
    picked, _ = pick_account([refreshed], 0, today)
    assert picked.id == 2
