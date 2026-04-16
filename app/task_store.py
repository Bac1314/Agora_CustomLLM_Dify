"""
Per-session background task state tracker.

Tracks Dify background tasks through their lifecycle:
  running → completed → reported

Keyed by session_key = f"{app_id}:{channel_name}:{user_id}".

When a task completes, get_pending_injection() returns its result and
atomically marks it as "reported" so it is injected exactly once into
the LLM context. The LLM then calls _publish_message to deliver the
result to the user's app.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_STALE_SECONDS = 600    # 10 minutes — clean up tasks older than this
_MAX_TASKS_PER_SESSION = 50


@dataclass
class BackgroundTask:
    task_id: str
    tool_name: str
    args: Dict[str, Any]
    status: str              # "running" | "completed" | "reported"
    result: Optional[str]
    error: Optional[str]
    created_at: float
    session_key: str


@dataclass
class _SessionTaskState:
    tasks: List[BackgroundTask] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TaskStore:
    """Thread-safe (asyncio) per-session background task state store."""

    def __init__(self) -> None:
        self._sessions: Dict[str, _SessionTaskState] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    def start_cleanup(self) -> None:
        """Start background TTL cleanup. Call once from app lifespan."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop_cleanup(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)  # run every hour
            now = time.monotonic()
            stale_sessions = [
                k for k, v in self._sessions.items()
                if now - v.last_active > _STALE_SECONDS * 6  # 1 hour session TTL
            ]
            for k in stale_sessions:
                self._sessions.pop(k, None)
            # Also prune old tasks within active sessions
            for state in self._sessions.values():
                async with state.lock:
                    state.tasks = [
                        t for t in state.tasks
                        if now - t.created_at <= _STALE_SECONDS or t.status == "running"
                    ]
            if stale_sessions:
                logger.info("TaskStore: evicted %d stale sessions", len(stale_sessions))

    def _get_or_create(self, key: str) -> _SessionTaskState:
        if key not in self._sessions:
            self._sessions[key] = _SessionTaskState()
        state = self._sessions[key]
        state.last_active = time.monotonic()
        return state

    async def create_task(
        self,
        session_key: str,
        tool_name: str,
        args: Dict[str, Any],
    ) -> str:
        """Create a task in 'running' state. Returns task_id."""
        task_id = uuid.uuid4().hex[:12]
        task = BackgroundTask(
            task_id=task_id,
            tool_name=tool_name,
            args=args,
            status="running",
            result=None,
            error=None,
            created_at=time.monotonic(),
            session_key=session_key,
        )
        state = self._get_or_create(session_key)
        async with state.lock:
            # Cap per-session tasks
            if len(state.tasks) >= _MAX_TASKS_PER_SESSION:
                # Drop oldest reported task first, then oldest completed
                for status in ("reported", "completed"):
                    for i, t in enumerate(state.tasks):
                        if t.status == status:
                            state.tasks.pop(i)
                            break
                    if len(state.tasks) < _MAX_TASKS_PER_SESSION:
                        break
            state.tasks.append(task)
        logger.debug("TaskStore: created task %s ('%s') for session %s", task_id, tool_name, session_key)
        return task_id

    async def complete_task(self, session_key: str, task_id: str, result: str) -> None:
        """Mark a task as 'completed' with the Dify result text."""
        state = self._get_or_create(session_key)
        async with state.lock:
            for t in state.tasks:
                if t.task_id == task_id:
                    t.status = "completed"
                    t.result = result
                    logger.debug("TaskStore: completed task %s ('%s')", task_id, t.tool_name)
                    return
        logger.warning("TaskStore: complete_task called for unknown task_id=%s session=%s", task_id, session_key)

    async def fail_task(self, session_key: str, task_id: str, error: str) -> None:
        """Mark a task as 'completed' (with error) so it gets reported to the LLM."""
        state = self._get_or_create(session_key)
        async with state.lock:
            for t in state.tasks:
                if t.task_id == task_id:
                    t.status = "completed"
                    t.error = error
                    logger.debug("TaskStore: failed task %s ('%s'): %s", task_id, t.tool_name, error)
                    return
        logger.warning("TaskStore: fail_task called for unknown task_id=%s session=%s", task_id, session_key)

    async def get_pending_injection(
        self,
        session_key: str,
    ) -> Tuple[List[BackgroundTask], List[BackgroundTask]]:
        """
        Return (completed_tasks, running_tasks) that need to be injected.

        completed_tasks: tasks with status="completed" — atomically transitioned
                         to "reported" so they are never returned again.
        running_tasks:   tasks with status="running" — status is unchanged.
        """
        state = self._get_or_create(session_key)
        async with state.lock:
            completed = [t for t in state.tasks if t.status == "completed"]
            running = [t for t in state.tasks if t.status == "running"]
            # Atomically mark completed as reported
            for t in completed:
                t.status = "reported"
        return completed, running


# Module-level singleton; start_cleanup() called in app lifespan.
store = TaskStore()
