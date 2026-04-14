"""Tests for session_store.py."""

import asyncio
import pytest

from app.session_store import SessionStore


@pytest.fixture
def store():
    return SessionStore()


@pytest.mark.asyncio
async def test_merge_into_empty_session(store):
    incoming = [{"role": "user", "content": "hello"}]
    result = await store.merge_into(incoming, "app:chan:user")
    assert result == incoming


@pytest.mark.asyncio
async def test_system_note_injected_on_next_merge(store):
    key = "app:chan:user"
    incoming = [{"role": "user", "content": "hi"}]
    await store.append_system_note(key, "Task done: result=42")
    merged = await store.merge_into(incoming, key)
    assert len(merged) == 2
    assert merged[-1]["role"] == "system"
    assert "Task done" in merged[-1]["content"]


@pytest.mark.asyncio
async def test_system_note_consumed_after_merge(store):
    key = "app:chan:user"
    await store.append_system_note(key, "note1")
    await store.merge_into([{"role": "user", "content": "q"}], key)
    # Second merge should NOT include the note again
    second = await store.merge_into([{"role": "user", "content": "q2"}], key)
    assert len(second) == 1


@pytest.mark.asyncio
async def test_append_tool_result_formats_note(store):
    key = "app:chan:user"
    await store.append_tool_result(key, "lookup_order", "Order shipped today.")
    merged = await store.merge_into([], key)
    assert any("lookup_order" in m["content"] and "Order shipped" in m["content"] for m in merged)


@pytest.mark.asyncio
async def test_concurrent_appends_safe(store):
    key = "app:chan:user"
    await asyncio.gather(
        store.append_system_note(key, "note A"),
        store.append_system_note(key, "note B"),
        store.append_system_note(key, "note C"),
    )
    merged = await store.merge_into([], key)
    assert len(merged) == 3
