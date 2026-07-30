"""Telegram notifications for platform alerts.

One outbound call to the Telegram Bot API. No-op (returns False) unless both
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are configured, so the rest of the code
can call send() unconditionally.
"""
import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


async def send(text: str) -> bool:
    """Send a Telegram message. Returns True on success, False if unconfigured or
    the API call failed (callers never depend on delivery)."""
    if not configured():
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        return r.status_code == 200
    except httpx.HTTPError:
        return False
