"""
Agora RTM (Signaling) server-side message publisher.

Uses the Agora Signaling REST API v2 to publish messages to a channel
without needing a persistent SDK connection.

Endpoint (Signaling REST API):
  POST https://api.agora.io/dev/v2/project/{appid}/rtm/users/{uid}/channel_messages

Auth: HTTP Basic Auth — base64(AGORA_CUSTOMER_ID:AGORA_CUSTOMER_SECRET)
  Customer credentials: Agora Console → Developer Toolkit → RESTful API

Request body:
  {
    "channel_name": "<channel>",
    "payload": "<message text>",
    "enable_historical_messaging": false
  }

Response 200: { "result": "success", "request_id": "...", "code": "message_sent" }

References:
  https://docs.agora.io/en/signaling/rest-api/channel-message
"""

import base64
import logging
import os
from typing import Optional

import httpx

from app.settings import get_settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.agora.io/dev/v2/project"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _basic_auth_header() -> str:
    """Build the Authorization header value from AGORA_CUSTOMER_ID/SECRET env vars."""
    settings = get_settings()
    customer_id = os.environ.get("AGORA_CUSTOMER_ID", "")
    customer_secret = os.environ.get("AGORA_CUSTOMER_SECRET", "")
    if not customer_id or not customer_secret:
        logger.warning(
            "AGORA_CUSTOMER_ID or AGORA_CUSTOMER_SECRET not set — RTM publish will likely fail. "
            "Get credentials from Agora Console → Developer Toolkit → RESTful API."
        )
    credentials = base64.b64encode(f"{customer_id}:{customer_secret}".encode()).decode()
    return f"Basic {credentials}"


async def publish(app_id: str, channel_name: str, message: str, sender_uid: Optional[str] = None) -> bool:
    """
    Publish a text message to an Agora RTM (Signaling) channel from the server.

    Args:
        app_id:       Agora App ID (from settings or passed explicitly).
        channel_name: Name of the RTM channel to publish to.
        message:      Text content to publish.
        sender_uid:   The UID to send as. Defaults to AGORA_RTM_SENDER_UID from settings.

    Returns:
        True on success, False on failure (errors are logged; never raised).
    """
    settings = get_settings()
    effective_app_id = app_id or settings.agora_app_id
    effective_uid = sender_uid or settings.agora_rtm_sender_uid

    if not effective_app_id:
        logger.error("RTM publish skipped: AGORA_APP_ID is not configured")
        return False

    url = f"{_BASE_URL}/{effective_app_id}/rtm/users/{effective_uid}/channel_messages"
    payload = {
        "channel_name": channel_name,
        "payload": message,
        "enable_historical_messaging": False,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": _basic_auth_header(),
                },
            )
            if resp.status_code == 200:
                logger.info("RTM publish OK → channel=%s msg=%s...", channel_name, message[:80])
                return True
            else:
                logger.error(
                    "RTM publish failed: HTTP %d — %s", resp.status_code, resp.text[:200]
                )
                return False
    except Exception:
        logger.exception("RTM publish exception for channel=%s", channel_name)
        return False
