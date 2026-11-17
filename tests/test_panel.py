import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.divar.parse import parse_detail
from app.exporting import to_csv, to_xlsx
from app.main import create_app

FIX = Path(__file__).parent / "fixtures"


def _page(token: str, has_next: bool, page: int) -> dict:
    payload = json.loads((FIX / "search_page1.json").read_text(encoding="utf-8"))
    payload["list_widgets"] = payload["list_widgets"][:1]
    payload["list_widgets"][0]["data"]["token"] = token
    payload["list_widgets"][0]["data"]["action"]["payload"]["token"] = token
    payload["pagination"] = {"has_next_page": has_next, "data": {"page": page, "last": token}}
    return payload


def test_export_headers_and_rtl():
    csv_bytes = to_csv([["سالن", "https://divar.ir/v/x", "ونک", "", "خلاصه", "", "", "کار", "", "x"]])
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    assert "عنوان".encode() in csv_bytes
    book = to_xlsx([["سالن", "https://divar.ir/v/x", "ونک", "توافقی", "", "", "09120000000", "کار", "", "x"]])
    from openpyxl import load_workbook
    import io
    sheet = load_workbook(io.BytesIO(book)).active
    assert sheet.sheet_view.rightToLeft is True
    assert sheet["A1"].value == "عنوان"
    assert sheet["G2"].value == "09120000000"


def test_job_streams_two_pages_and_exports(tmp_path):
    app = create_app(tmp_path)
    pages = [_page("aaaa1111", True, 1), _page("bbbb2222", False, 2)]
    seen_bodies = []
    detail = json.loads((FIX / "detail_sample.json").read_text(encoding="utf-8"))

    async def search(body):
        seen_bodies.append(body)
        return 200, pages.pop(0), ""

    async def fetch_listing_info(token):
        parsed = parse_detail(token, detail)
        assert parsed["description"]
        return {
            "description": parsed["description"],
            "subtitle": parsed.get("subtitle") or "",
            "specs": parsed.get("specs") or {},
            "needs_login": False,
            "needs_captcha": False,
            "error": None,
        }

    with TestClient(app) as client:
        account = client.post("/api/browser-accounts", json={"label": "detail"}).json()
        app.state.db.update_browser_account(account["id"], status="active", phone="09120000001")
        app.state.browsers.get(account["id"]).fetch_listing_info = fetch_listing_info
        app.state.db.update_settings({"anon_rps": "20", "default_daily_cap": "50", "delay_min": "20", "delay_max": "40"})
        app.state.manager.refresh_rate()
        app.state.manager.client.search = search
        created = client.post(
            "/api/jobs",
            json={
                "name": "سالن",
                "category": "beauty-and-haircare",
                "city_id": "1",
                "keywords": ["آرایشگاه"],
                "max_pages": 5,
                "fetch_details": True,
                "fetch_phones": False,
            },
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        assert created.json()["city_name"] == "تهران"
        assert client.post(f"/api/jobs/{job_id}/start").status_code == 200
        status = "running"
        for _ in range(80):
            status = client.get("/api/jobs").json()[0]["status"]
            if status in {"done", "error"}:
                break
            time.sleep(0.05)
        assert status == "done"
        assert seen_bodies[0]["search_data"]["query"] == "آرایشگاه"
        assert "pagination_data" not in seen_bodies[0]
        assert seen_bodies[1]["pagination_data"]["page"] == 1
        rows = client.get("/api/listings").json()
        assert {row["token"] for row in rows} == {"aaaa1111", "bbbb2222"}
        assert rows[0]["district"] == "ونک"
        assert rows[0]["description"]
        exported = client.get(f"/api/export.csv?job_id={job_id}")
        assert exported.content.startswith(b"\xef\xbb\xbf")
        assert "سالن".encode() in exported.content or rows[0]["title"].encode() in exported.content
        sheet = client.get(f"/api/export.xlsx?job_id={job_id}")
        assert sheet.status_code == 200
        assert sheet.content[:2] == b"PK"


def test_pause_resume_persists(tmp_path):
    app = create_app(tmp_path)
    gate = {"go": False}

    async def search(body):
        while not gate["go"]:
            import asyncio
            await asyncio.sleep(0.02)
        return 200, _page("cccc3333", False, 1), ""

    async def fetch_listing_info(token):
        return {
            "description": "",
            "subtitle": "",
            "specs": {},
            "needs_login": False,
            "needs_captcha": False,
            "error": "empty",
        }

    with TestClient(app) as client:
        app.state.db.update_settings({"anon_rps": "20", "default_daily_cap": "50", "delay_min": "5", "delay_max": "5"})
        app.state.manager.refresh_rate()
        app.state.manager.client.search = search
        app.state.manager.browser.fetch_listing_info = fetch_listing_info
        job_id = client.post("/api/jobs", json={"category": "beauty-and-haircare", "max_pages": 1, "fetch_details": False}).json()["id"]
        client.post(f"/api/jobs/{job_id}/start")
        time.sleep(0.1)
        assert client.post(f"/api/jobs/{job_id}/pause").json()["status"] == "paused"
        reloaded = create_app(tmp_path)
        assert reloaded.state.db.get_job(job_id)["status"] == "paused"
        gate["go"] = True
        client.post(f"/api/jobs/{job_id}/stop")


def test_confirm_saves_token_without_leaking_it(tmp_path):
    app = create_app(tmp_path)

    async def request_code(phone, proxy=None):
        assert phone == "09120000000"
        return 200, "{}"

    async def confirm_code(phone, code, proxy=None):
        assert code == "1234"
        return 200, {"access_token": "secret-token-value"}, ""

    app.state.auth.request_code = request_code
    app.state.auth.confirm_code = confirm_code
    with TestClient(app) as client:
        sent = client.post("/api/accounts/request-code", json={"phone": "09120000000"})
        assert sent.status_code == 200
        saved = client.post(
            "/api/accounts/confirm",
            json={
                "phone": "09120000000",
                "code": "۱۲۳۴",
                "daily_cap": 10,
                "proxy": "http://127.0.0.1:8888",
            },
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["status"] == "active"
        assert "secret-token-value" not in json.dumps(body)
        assert body["daily_cap"] == 10
        secret = app.state.db.account_secrets()[0]
        assert secret["token"] == "secret-token-value"
        assert secret["proxy"] == "http://127.0.0.1:8888"


def test_fetch_phones_human_browse_opens_ads_one_by_one(tmp_path):
    app = create_app(tmp_path)
    opened = []
    scrolls = {"n": 0}

    async def open_search_list(list_url):
        opened.append(("list", list_url))
        return {}

    async def scroll_search_list():
        scrolls["n"] += 1
        return {}

    async def collect_list_ads():
        if scrolls["n"] <= 1:
            return [{"token": "phoneTok1", "title": "آگهی تست", "href": "https://divar.ir/v/phoneTok1"}]
        return []

    async def open_ad_from_list(token, list_url):
        opened.append(("ad", token))
        return {
            "phones": ["09120000000"],
            "needs_login": False,
            "needs_captcha": False,
            "error": None,
            "description": "توضیح از صفحه",
        }

    with TestClient(app) as client:
        account = client.post(
            "/api/browser-accounts",
            json={"label": "A1", "daily_cap": 50, "delay_min": 0.01, "delay_max": 0.01},
        ).json()
        app.state.db.update_browser_account(account["id"], status="active", phone="09121111111")
        session = app.state.browsers.get(account["id"])
        session.open_search_list = open_search_list
        session.scroll_search_list = scroll_search_list
        session.collect_list_ads = collect_list_ads
        session.open_ad_from_list = open_ad_from_list
        app.state.db.update_settings(
            {"anon_rps": "20", "default_daily_cap": "50", "delay_min": "0.01", "delay_max": "0.01"}
        )
        app.state.manager.refresh_rate()
        created = client.post(
            "/api/jobs",
            json={
                "category": "beauty-and-haircare",
                "max_pages": 1,
                "fetch_details": False,
                "fetch_phones": True,
            },
        )
        assert created.status_code == 200
        assert created.json()["fetch_details"] is False
        job_id = created.json()["id"]
        client.post(f"/api/jobs/{job_id}/start")
        status = "running"
        for _ in range(120):
            status = client.get("/api/jobs").json()[0]["status"]
            if status in {"done", "error"}:
                break
            time.sleep(0.05)
        assert status == "done"
        rows = client.get("/api/listings").json()
        assert rows[0]["phone"] == "09120000000"
        assert rows[0]["phone_status"] == "done"
        assert rows[0]["description"] == "توضیح از صفحه"
        assert ("ad", "phoneTok1") in opened
        assert any(item[0] == "list" for item in opened)


def test_telegram_notify_captcha_message(monkeypatch):
    import asyncio

    from app import notify

    sent = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            sent["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json):
            sent["url"] = url
            sent["json"] = json
            return FakeResponse()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setattr(notify.httpx, "AsyncClient", FakeClient)
    ok = asyncio.get_event_loop().run_until_complete(
        notify.notify_captcha(
            account_label="A1",
            account_phone="09120000000",
            vnc_url="https://divar2.pixlink.ir/vnc.html",
            panel_url="https://divar.pixlink.ir/",
        )
    )
    assert ok is True
    assert "tok" in sent["url"]
    assert sent["json"]["chat_id"] == "42"
    assert "کپچا" in sent["json"]["text"]
    assert "09120000000" in sent["json"]["text"]
    assert sent["client_kwargs"].get("proxy") == "socks5://127.0.0.1:1080"


def test_job_loop_toggle(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/jobs",
            json={"category": "beauty-and-haircare", "max_pages": 1, "fetch_details": False},
        ).json()
        job_id = created["id"]
        assert created.get("loop") is False
        enabled = client.post(f"/api/jobs/{job_id}/loop")
        assert enabled.status_code == 200
        assert enabled.json()["loop"] is True
        disabled = client.post(f"/api/jobs/{job_id}/loop")
        assert disabled.json()["loop"] is False


def test_create_job_from_divar_url(tmp_path):
    app = create_app(tmp_path)

    async def fetch_html(url):
        assert "beauty-services" in url
        html = """
        <script>window.__PRELOADED_STATE__ = {
          "search": {"searchData": {"formData": {"category": {"str": {"value": "beauty-and-haircare"}}}}},
          "multiCity": {"selectedCities": [{"id": 715, "name": "کل ایران", "slug": "iran"}]}
        };</script>
        """
        return 200, html

    app.state.auth.fetch_html = fetch_html
    with TestClient(app) as client:
        created = client.post(
            "/api/jobs/from-url",
            json={
                "url": "https://divar.ir/s/iran/beauty-services",
                "fetch_phones": True,
                "fetch_details": False,
                "max_pages": 2,
                "start": False,
            },
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["category"] == "beauty-and-haircare"
        assert body["city_id"] == "715"
        assert body["city_name"] == "کل ایران"
        assert body["fetch_phones"] is True
        assert body["status"] == "paused"


def test_browser_accounts_crud_and_captcha(tmp_path):
    app = create_app(tmp_path)
    opened = []

    async def open_login(label=""):
        opened.append(label or True)

    async def verify_login():
        return {"logged_in": True, "phone": "09123334444", "needs_captcha": False}

    with TestClient(app) as client:
        created = client.post("/api/browser-accounts", json={"label": "اصلی", "daily_cap": 50})
        assert created.status_code == 200
        account_id = created.json()["id"]
        assert created.json()["status"] == "needs_login"
        patched = client.patch(
            f"/api/browser-accounts/{account_id}",
            json={"daily_cap": 120, "delay_min": 10, "delay_max": 15},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["daily_cap"] == 120
        assert patched.json()["delay_min"] == 10
        assert patched.json()["delay_max"] == 15

        session = app.state.browsers.get(account_id)
        session.open_login = open_login
        session.verify_login = verify_login
        login = client.post(f"/api/browser-accounts/{account_id}/open-login")
        assert login.status_code == 200
        assert opened

        verified = client.post(f"/api/browser-accounts/{account_id}/verify")
        assert verified.status_code == 200
        assert verified.json()["ok"] is True
        assert verified.json()["account"]["status"] == "active"
        assert verified.json()["account"]["phone"] == "09123334444"

        listed = client.get("/api/browser-accounts").json()
        assert len(listed) == 1
        assert listed[0]["status"] == "active"

        idle = client.get("/api/browser/captcha")
        assert idle.json()["active"] is False
        session.begin_captcha()
        app.state.browsers.note_captcha(account_id)
        assert client.get("/api/browser/captcha").json()["active"] is True
        assert client.post("/api/browser/captcha/resolved").json()["active"] is False

        assert client.delete(f"/api/browser-accounts/{account_id}").status_code == 200
        assert client.get("/api/browser-accounts").json() == []
