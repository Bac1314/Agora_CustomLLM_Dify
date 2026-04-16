"""Tests for task_store.py."""

import asyncio
import pytest

from app.task_store import TaskStore


@pytest.fixture
def store():
    return TaskStore()


@pytest.mark.asyncio
async def test_create_task_returns_id(store):
    task_id = await store.create_task("app:chan:user", "web_search", {"query": "test"})
    assert isinstance(task_id, str)
    assert len(task_id) > 0
    completed, running = await store.get_pending_injection("app:chan:user")
    assert len(running) == 1
    assert running[0].task_id == task_id
    assert running[0].status == "running"
    assert running[0].tool_name == "web_search"
    assert len(completed) == 0


@pytest.mark.asyncio
async def test_complete_task_sets_result(store):
    key = "app:chan:user"
    task_id = await store.create_task(key, "web_search", {"query": "hello"})
    await store.complete_task(key, task_id, "Some result text")
    completed, running = await store.get_pending_injection(key)
    assert len(completed) == 1
    assert completed[0].result == "Some result text"
    assert completed[0].error is None
    assert len(running) == 0


@pytest.mark.asyncio
async def test_fail_task_stores_error(store):
    key = "app:chan:user"
    task_id = await store.create_task(key, "web_search", {"query": "hello"})
    await store.fail_task(key, task_id, "Connection timeout")
    completed, running = await store.get_pending_injection(key)
    assert len(completed) == 1
    assert completed[0].error == "Connection timeout"
    assert completed[0].result is None


@pytest.mark.asyncio
async def test_get_pending_injection_classification(store):
    key = "app:chan:user"
    id1 = await store.create_task(key, "tool_a", {})
    id2 = await store.create_task(key, "tool_b", {})
    id3 = await store.create_task(key, "tool_c", {})
    await store.complete_task(key, id1, "result_a")
    await store.complete_task(key, id2, "result_b")
    # id3 stays running
    completed, running = await store.get_pending_injection(key)
    assert len(completed) == 2
    assert len(running) == 1
    assert running[0].task_id == id3
    completed_ids = {t.task_id for t in completed}
    assert id1 in completed_ids
    assert id2 in completed_ids


@pytest.mark.asyncio
async def test_completed_transitions_to_reported(store):
    key = "app:chan:user"
    task_id = await store.create_task(key, "web_search", {"query": "hi"})
    await store.complete_task(key, task_id, "result")
    # First call: should return the completed task
    completed, _ = await store.get_pending_injection(key)
    assert len(completed) == 1
    # Second call: task is now reported, should not be returned again
    completed2, running2 = await store.get_pending_injection(key)
    assert len(completed2) == 0
    assert len(running2) == 0


@pytest.mark.asyncio
async def test_session_isolation(store):
    key_a = "app:chan:alice"
    key_b = "app:chan:bob"
    id_a = await store.create_task(key_a, "web_search", {"query": "A"})
    await store.complete_task(key_a, id_a, "result_a")
    # Bob's session should see nothing
    completed_b, running_b = await store.get_pending_injection(key_b)
    assert len(completed_b) == 0
    assert len(running_b) == 0
    # Alice's session should see her completed task
    completed_a, _ = await store.get_pending_injection(key_a)
    assert len(completed_a) == 1
    assert completed_a[0].result == "result_a"


@pytest.mark.asyncio
async def test_concurrent_operations_safe(store):
    key = "app:chan:user"
    # Create multiple tasks concurrently
    task_ids = await asyncio.gather(
        store.create_task(key, "tool_1", {}),
        store.create_task(key, "tool_2", {}),
        store.create_task(key, "tool_3", {}),
    )
    assert len(set(task_ids)) == 3  # all unique IDs
    # Complete them concurrently
    await asyncio.gather(*[store.complete_task(key, tid, f"result_{i}") for i, tid in enumerate(task_ids)])
    completed, running = await store.get_pending_injection(key)
    assert len(completed) == 3
    assert len(running) == 0


@pytest.mark.asyncio
async def test_complete_unknown_task_is_safe(store):
    """complete_task for an unknown task_id should not raise."""
    await store.complete_task("app:chan:user", "nonexistent-id", "result")  # should not raise


@pytest.mark.asyncio
async def test_running_tasks_not_marked_reported(store):
    key = "app:chan:user"
    task_id = await store.create_task(key, "web_search", {})
    # Running task should appear in multiple calls
    _, running1 = await store.get_pending_injection(key)
    assert len(running1) == 1
    _, running2 = await store.get_pending_injection(key)
    assert len(running2) == 1  # still running, not consumed
