"""
Async HTTP client for Dify AI.

Supports two endpoint types (per tool config):
  - "workflow"  → POST /workflows/run
  - "chat"      → POST /chat-messages  (blocking mode; streaming not needed here
                   since results are delivered out-of-band via RTM)

All calls are fire-and-forget from the caller's perspective; exceptions are
caught and returned as error strings so the caller can log/report them without
crashing.
"""

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


async def run_workflow(
    base_url: str,
    api_key: str,
    inputs: Dict[str, Any],
    user: str = "agora-wrapper",
) -> str:
    """
    Call Dify /workflows/run (blocking mode).

    Returns the text output from the workflow, or an error string on failure.
    Dify workflow response schema:
      { "data": { "outputs": { "<key>": "<value>", ... }, "status": "succeeded" | ... } }
    We join all string output values as the result text.
    """
    url = base_url.rstrip("/") + "/workflows/run"
    payload: Dict[str, Any] = {
        "inputs": inputs,
        "response_mode": "blocking",
        "user": user,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            outputs = data.get("data", {}).get("outputs", {})
            # Extract text from outputs (join all string values)
            result_parts = [str(v) for v in outputs.values() if v is not None]
            result = " ".join(result_parts) if result_parts else str(data)
            logger.info("Dify workflow succeeded: %s", result[:200])
            return result
    except httpx.HTTPStatusError as e:
        msg = f"Dify workflow HTTP error {e.response.status_code}: {e.response.text[:200]}"
        logger.error(msg)
        return msg
    except Exception as e:
        msg = f"Dify workflow error: {e}"
        logger.error(msg, exc_info=True)
        return msg


async def run_chat(
    base_url: str,
    api_key: str,
    query: str,
    inputs: Optional[Dict[str, Any]] = None,
    conversation_id: str = "",
    user: str = "agora-wrapper",
) -> str:
    """
    Call Dify /chat-messages (blocking mode).

    Returns the answer text, or an error string on failure.
    Dify chat response schema (blocking):
      { "answer": "<text>", "conversation_id": "...", ... }
    """
    url = base_url.rstrip("/") + "/chat-messages"
    payload: Dict[str, Any] = {
        "inputs": inputs or {},
        "query": query,
        "response_mode": "blocking",
        "conversation_id": conversation_id,
        "user": user,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            answer = data.get("answer", str(data))
            logger.info("Dify chat succeeded: %s", answer[:200])
            return answer
    except httpx.HTTPStatusError as e:
        msg = f"Dify chat HTTP error {e.response.status_code}: {e.response.text[:200]}"
        logger.error(msg)
        return msg
    except Exception as e:
        msg = f"Dify chat error: {e}"
        logger.error(msg, exc_info=True)
        return msg
