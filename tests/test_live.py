"""One live anonymous call. No login and no SMS."""
from app.divar.client import DivarClient, build_search
from app.divar.parse import pagination_state, parse_detail, parse_search_rows


def test_live_search_and_detail_sync():
    import asyncio

    async def run():
        client = DivarClient()
        body = build_search("1", "beauty-and-haircare", None, None)
        status, payload, _text = await client.search(body)
        assert status == 200, status
        rows = parse_search_rows(payload)
        assert len(rows) >= 10
        _data, has_next = pagination_state(payload)
        assert has_next is True
        detail_status, detail, _ = await client.detail(rows[0]["token"])
        assert detail_status == 200
        parsed = parse_detail(rows[0]["token"], detail)
        assert parsed["contact_uuid"]

    asyncio.run(run())
