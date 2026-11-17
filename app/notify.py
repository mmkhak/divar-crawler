"""Optional Telegram alerts (captcha, etc.)."""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip().strip("'\"")
    return value


def telegram_configured() -> bool:
    return bool(_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"))


def _bot_token() -> str:
    token = _env("TELEGRAM_BOT_TOKEN")
    # people sometimes paste "bot123:ABC" from docs
    if token.lower().startswith("bot"):
        token = token[3:]
    return token


async def send_telegram(text: str) -> bool:
    token = _bot_token()
    chat_id = _env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    proxy = _env("TELEGRAM_PROXY") or None
    try:
        kwargs: dict = {"timeout": 15.0, "trust_env": False}
        if proxy:
            kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 400:
            hint = ""
            if response.status_code == 401:
                hint = f" (bad TELEGRAM_BOT_TOKEN; len={len(token)} prefix={token.split(':', 1)[0]!r})"
            log.warning(
                "telegram notify failed: %s %s%s",
                response.status_code,
                response.text[:200],
                hint,
            )
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
