"""Tests for dify_client.py — mock HTTP, assert request shape."""

import pytest
import httpx
import respx

from app import dify_client


@respx.mock
@pytest.mark.asyncio
async def test_run_workflow_success():
    respx.post("https://api.dify.ai/v1/workflows/run").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"outputs": {"result": "Order shipped today."}, "status": "succeeded"}},
        )
    )
    result = await dify_client.run_workflow(
        base_url="https://api.dify.ai/v1",
        api_key="app-testkey",
        inputs={"order_id": "ORD-123"},
        user="tester",
    )
    assert "Order shipped" in result


@respx.mock
@pytest.mark.asyncio
async def test_run_workflow_http_error():
    respx.post("https://api.dify.ai/v1/workflows/run").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    result = await dify_client.run_workflow(
        base_url="https://api.dify.ai/v1",
        api_key="bad-key",
        inputs={},
    )
    assert "401" in result or "error" in result.lower() or "HTTP" in result


@respx.mock
@pytest.mark.asyncio
async def test_run_chat_success():
    respx.post("https://api.dify.ai/v1/chat-messages").mock(
        return_value=httpx.Response(200, json={"answer": "Here is your answer.", "conversation_id": "abc"})
    )
    result = await dify_client.run_chat(
        base_url="https://api.dify.ai/v1",
        api_key="app-testkey",
        query="What is the status?",
        user="tester",
    )
    assert "Here is your answer" in result


@respx.mock
@pytest.mark.asyncio
async def test_request_includes_bearer_auth():
    route = respx.post("https://api.dify.ai/v1/workflows/run").mock(
        return_value=httpx.Response(200, json={"data": {"outputs": {"r": "ok"}, "status": "succeeded"}})
    )
    await dify_client.run_workflow(
        base_url="https://api.dify.ai/v1",
        api_key="my-key",
        inputs={},
    )
    assert route.called
    sent_headers = route.calls[0].request.headers
    assert sent_headers.get("authorization") == "Bearer my-key"
