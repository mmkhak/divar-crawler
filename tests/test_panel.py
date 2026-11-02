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

    async def fetch_detail(token):
        parsed = parse_detail(token, detail)
        assert parsed["description"]
        return 200, detail, ""

    with TestClient(app) as client:
        app.state.db.update_settings({"anon_rps": "20", "default_daily_cap": "50", "delay_min": "20", "delay_max": "40"})
        app.state.manager.refresh_rate()
        app.state.manager.client.search = search
        app.state.manager.client.detail = fetch_detail
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

    async def fetch_detail(token):
        return 404, None, ""

    with TestClient(app) as client:
        app.state.db.update_settings({"anon_rps": "20", "default_daily_cap": "50", "delay_min": "5", "delay_max": "5"})
        app.state.manager.refresh_rate()
        app.state.manager.client.search = search
        app.state.manager.client.detail = fetch_detail
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


def test_fetch_phones_queues_even_without_details_flag(tmp_path):
    app = create_app(tmp_path)
    pages = [_page("phoneTok1", False, 1)]
    detail = json.loads((FIX / "detail_sample.json").read_text(encoding="utf-8"))
    contacts = []

    async def search(body):
        return 200, pages.pop(0), ""

    async def fetch_detail(token):
        return 200, detail, ""

    async def contact(token, auth_token, contact_uuid, proxy=None):
        contacts.append((token, auth_token, contact_uuid))
        return 200, '{"widget_list":[{"text":"09120000000"}]}'

    with TestClient(app) as client:
        app.state.db.update_settings(
            {"anon_rps": "20", "default_daily_cap": "50", "delay_min": "0", "delay_max": "0"}
        )
        app.state.manager.refresh_rate()
        app.state.manager.client.search = search
        app.state.manager.client.detail = fetch_detail
        app.state.manager.client.contact = contact
        app.state.db.add_account(
            {
                "phone": "09120000000",
                "token": "acct-token",
                "daily_cap": 10,
                "delay_min": 0.01,
                "delay_max": 0.01,
            }
        )
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
        assert created.json()["fetch_details"] is True
        job_id = created.json()["id"]
        client.post(f"/api/jobs/{job_id}/start")
        status = "running"
        for _ in range(80):
            status = client.get("/api/jobs").json()[0]["status"]
            if status in {"done", "error"}:
                break
            time.sleep(0.05)
        assert status == "done"
        rows = client.get("/api/listings").json()
        assert rows[0]["phone"] == "09120000000"
        assert rows[0]["phone_status"] == "done"
        assert contacts and contacts[0][0] == "phoneTok1"
