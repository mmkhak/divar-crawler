"""Optional Telegram alerts (captcha, etc.)."""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def telegram_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


async def send_telegram(text: str) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 400:
            log.warning("telegram notify failed: %s %s", response.status_code, response.text[:200])
            return False
        return True
    except Exception as exc:
        log.warning("telegram notify error: %s", exc)
        return False


async def notify_captcha(
    *,
    account_label: str,
    account_phone: str | None = None,
    vnc_url: str | None = None,
    panel_url: str | None = None,
) -> bool:
    who = account_phone or account_label
    lines = [
        "⚠️ کپچا در خزش دیوار",
        f"اکانت: {who}",
        "برو کپچا را حل کن، بعد در پنل «کپچا حل شد» را بزن.",
    ]
    if vnc_url:
        lines.append(f"VNC: {vnc_url}")
    if panel_url:
        lines.append(f"پنل: {panel_url}")
    return await send_telegram("\n".join(lines))
