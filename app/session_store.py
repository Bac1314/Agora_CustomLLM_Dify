"""
Per-session conversation memory.

Keyed by session_key = f"{app_id}:{channel_name}:{user_id}".
Stores additional messages produced by background Dify tasks so the upstream
LLM sees them on the next turn.

Storage: in-memory dict for v1.
  - Max 100 extra messages per session (matches server-custom-llm reference).
  - TTL: 24 hours since last activity.
  - Upgrade path: replace _sessions dict with a Redis/SQLite-backed store
    by implementing the same interface in a subclass.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TTL_SECONDS = 86_400   # 24 hours
_MAX_EXTRA_MESSAGES = 100


@dataclass
class _SessionState:
    extra_messages: List[Dict[str, Any]] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    """Thread-safe (asyncio) in-memory session store."""

    def __init__(self) -> None:
        self._sessions: Dict[str, _SessionState] = {}
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
            expired = [k for k, v in self._sessions.items() if now - v.last_active > _TTL_SECONDS]
            for k in expired:
                self._sessions.pop(k, None)
            if expired:
                logger.info("SessionStore: evicted %d expired sessions", len(expired))

    def _get_or_create(self, key: str) -> _SessionState:
        if key not in self._sessions:
            self._sessions[key] = _SessionState()
        state = self._sessions[key]
        state.last_active = time.monotonic()
        return state

    async def merge_into(self, incoming: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
        """
        Return a new message list that is `incoming` with any pending
        background-task results appended immediately after the last message
        that predates them.

        Extra messages are consumed (cleared) after merge so they are not
        injected twice.
        """
        state = self._get_or_create(key)
        async with state.lock:
            if not state.extra_messages:
                return incoming
            merged = list(incoming) + list(state.extra_messages)
            state.extra_messages.clear()
            logger.debug("SessionStore: merged %d extra messages into session %s", len(merged) - len(incoming), key)
            return merged

    async def append_system_note(self, key: str, text: str) -> None:
        """Append a system-role message that the LLM will see on the next turn."""
        state = self._get_or_create(key)
        async with state.lock:
            if len(state.extra_messages) >= _MAX_EXTRA_MESSAGES:
                logger.warning("SessionStore: session %s hit max extra messages, dropping oldest", key)
                state.extra_messages.pop(0)
            state.extra_messages.append({"role": "system", "content": text})
            logger.debug("SessionStore: appended system note to session %s", key)

    async def append_tool_result(
        self,
        key: str,
        tool_name: str,
        result_text: str,
    ) -> None:
        """
        Append a system note informing the LLM that a background Dify task completed.

        We use a system note rather than a tool message because:
        - OpenAI rejects duplicate tool_call_id tool messages.
        - System notes are model-agnostic and survive LLM provider switches.
        """
        note = f"[Background task '{tool_name}' completed] {result_text}"
        await self.append_system_note(key, note)

    async def append_task_failure(self, key: str, tool_name: str, error: str) -> None:
        """Let the LLM know a background task failed so it can apologize naturally."""
        note = f"[Background task '{tool_name}' failed] {error}"
        await self.append_system_note(key, note)


# Module-level singleton; initialised in app lifespan.
store = SessionStore()
