"""Build Divar client-exporter send-report payloads like the web app."""
from __future__ import annotations

import time
import uuid
from typing import Any

APP_VERSION = "the-wall-v15-5-0"
BROWSER_VERSION = "157.0.0.0"
DEVICE = {"os": {"name": "web", "version": BROWSER_VERSION}}


def now_ms() -> int:
    return int(time.time() * 1000)


def page_context(view_id: str, page: str = "POST_DETAIL") -> dict[str, Any]:
    return {
        "view_id": view_id,
        "page": page,
        "app_name": "APP_THE_WALL",
        "is_pwa": False,
        "serving_mode": "desktop",
        "render_mode": "ssr",
        "browser_tier": "MODERN",
        "app_version": APP_VERSION,
    }


def post_detail_boot_reports(view_id: str | None = None) -> list[dict[str, Any]]:
    """Page-load sequence sent when opening a listing."""
    view = view_id or str(uuid.uuid4())
    started = now_ms()
    context = page_context(view)
    return [
        {
            "reports": [
                {
                    "user_event": {
                        "timestamp": str(started),
                        "web_page_load_event": {"context": context, "liveness": {}},
                    }
                }
            ],
            "device": DEVICE,
        },
        {
            "reports": [
                {
                    "user_event": {
                        "timestamp": str(started + 170),
                        "web_page_load_event": {
                            "context": context,
                            "load_status": {
                                "status": "BOOT",
                                "cause": "CAUSE_UNSPECIFIED",
                                "elapsed": 170,
                            },
                        },
                    }
                }
            ],
            "device": DEVICE,
        },
        {
            "reports": [
                {
                    "user_event": {
                        "timestamp": str(started + 360),
                        "web_page_load_event": {
                            "context": context,
                            "load_status": {"status": "READY", "elapsed": 360},
                        },
                    }
                }
            ],
            "device": DEVICE,
        },
    ]


def api_call_report(
    path: str,
    method: str,
    status_code: int,
    response_time_ms: int,
) -> dict[str, Any]:
    return {
        "reports": [
            {
                "api_call": {
                    "timestamp": str(now_ms()),
                    "path": path,
                    "method": method,
                    "status_code": status_code,
                    "response_time": response_time_ms,
                    "cdn_provider": "sotoon cdn",
                    "cdn_cache_status": "BYPASS",
                }
            }
        ],
        "device": DEVICE,
    }


def contact_api_report(token: str, status_code: int, response_time_ms: int) -> dict[str, Any]:
    path = f"https://api.divar.ir/v8/postcontact/web/contact_info_v2/{token}"
    return api_call_report(path, "POST", status_code, response_time_ms)
