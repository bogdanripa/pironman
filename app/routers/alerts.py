"""Test/inspect the Telegram alert wiring. The alerts themselves fire from a
background loop (app/alerts.py); this just lets you confirm delivery."""
from fastapi import APIRouter, Depends

from ..auth import require_key
from .. import notify

router = APIRouter(prefix="/alerts", tags=["alerts"],
                   dependencies=[Depends(require_key)])


@router.post("/test", operation_id="alerts_test",
             summary="Send a test Telegram alert to confirm the wiring")
async def alerts_test():
    """Send a test message to the configured Telegram chat. Returns whether
    Telegram is configured and whether the send succeeded — use it after setting
    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to verify alerts will reach you."""
    ok = await notify.send("✅ Pironman alerts are wired up.")
    return {"configured": notify.configured(), "sent": ok}
