"""
SSE streaming pass-through with Dify tool-call interception.

Flow for each /chat/completions request:
  1. Inject completed/running background task states as a system message.
  2. Upstream LLM is called with Dify tools injected.
  3. All non-tool-call chunks are forwarded to the client as-is.
  4. When the LLM finishes with finish_reason == "tool_calls":

     Case A — only non-Dify tools (e.g. _publish_message):
       - Forward the finish chunk as-is to Agora ConvoAI.
       - Stream ends; Agora executes the tool and sends a follow-up request.

     Case B — only Dify tools:
       - Suppress finish chunk; emit synthetic ack tool-result chunk.
       - Sync tools: await Dify result inline; 2nd LLM call speaks the answer.
       - Async tools: fire background task, emit synthetic ack; 2nd LLM call
         speaks an acknowledgement ("One sec, checking…").

     Case C — mixed Dify + non-Dify tools:
       - Handle Dify tools per Case B (suppress finish chunk, 2nd LLM call).
       - Non-Dify tools are skipped this turn; the LLM will call them on the
         next turn after seeing the injected task results.

  5. Stream ends with "data: [DONE]\n\n".

Background task delivery (async mode):
  - Completed task results are stored in task_store keyed by session.
  - On the next turn, get_pending_injection() surfaces completed/running tasks
    as a system message; the LLM calls _publish_message to deliver results.
"""

import asyncio
import json
import logging
import traceback
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from app import dify_client, session_store, task_store
from app.schemas import ChatCompletionRequest
from app.settings import get_settings
from app.tool_registry import ToolDef, registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context extracted from the incoming Agora ConvoAI request
# ---------------------------------------------------------------------------

class RequestContext:
    """Agora ConvoAI flattens its `params` block into the request root, so
    app_id / channel_name / user_id arrive as top-level fields.  The metadata
    dict and legacy context dict are checked as fallbacks."""

    def __init__(self, req: ChatCompletionRequest) -> None:
        logger.info("=== top-level: app_id=%r channel_name=%r user_id=%r", req.app_id, req.channel_name, req.user_id)
        logger.info("=== metadata field: %s", req.metadata)
        logger.info("=== context field: %s", req.context)
        for msg in req.messages:
            if hasattr(msg, "metadata") and msg.metadata:
                logger.info("=== user message metadata: role=%s %s", msg.role, msg.metadata)

        meta: Dict[str, Any] = req.metadata or {}
        ctx: Dict[str, Any] = req.context or {}

        self.app_id: str = (
            req.app_id
            or meta.get("app_id", "") or meta.get("appId", "")
            or ctx.get("app_id", "") or ctx.get("appId", "")
        )
        self.channel_name: str = (
            req.channel_name
            or meta.get("channel_name", "") or meta.get("channelName", "")
            or ctx.get("channel_name", "") or ctx.get("channelName", "")
        )
        self.user_id: str = (
            req.user_id
            or meta.get("user_id", "") or meta.get("userId", "")
            or ctx.get("user_id", "") or ctx.get("userId", "")
        )

        if not self.app_id:
            logger.warning(
                "No app_id found in request — task store and session memory disabled. "
                "Agora ConvoAI should populate params.app_id in the agent join config."
            )

    @property
    def session_key(self) -> str:
        return f"{self.app_id}:{self.channel_name}:{self.user_id}"


# ---------------------------------------------------------------------------
# Background task: run Dify and store result in task store
# ---------------------------------------------------------------------------

async def _call_dify(
    tool_def: ToolDef,
    llm_args: Dict[str, Any],
    ctx: RequestContext,
) -> str:
    """Call the Dify workflow or chat endpoint and return the result text."""
    dify_inputs = tool_def.build_dify_inputs(llm_args)
    dify_user = tool_def.format_user(ctx.user_id, ctx.channel_name)

    if tool_def.dify_endpoint == "chat":
        query = llm_args.get("query") or json.dumps(llm_args)
        return await dify_client.run_chat(
            base_url=tool_def.dify_base_url,
            api_key=tool_def.dify_api_key,
            query=query,
            inputs=dify_inputs,
            user=dify_user,
        )
    else:
        return await dify_client.run_workflow(
            base_url=tool_def.dify_base_url,
            api_key=tool_def.dify_api_key,
            inputs=dify_inputs,
            user=dify_user,
        )


async def _run_dify_background(
    tool_def: ToolDef,
    llm_args: Dict[str, Any],
    ctx: RequestContext,
    task_id: str,
) -> None:
    """
    Background task (fire-and-forget) for async-mode tools.

    Calls the Dify workflow/chat endpoint and stores the result in the task
    store. On the next conversation turn, get_pending_injection() surfaces the
    result as a system message; the LLM then calls _publish_message to deliver
    it to the user's app.
    """
    logger.info("Background: starting Dify task '%s' (id=%s) with args=%s", tool_def.name, task_id, llm_args)
    try:
        result = await _call_dify(tool_def, llm_args, ctx)
        await task_store.store.complete_task(ctx.session_key, task_id, result)
        logger.info("Background: Dify task '%s' (id=%s) complete.", tool_def.name, task_id)
    except Exception as exc:
        logger.error("Background: Dify task '%s' (id=%s) failed: %s", tool_def.name, task_id, exc, exc_info=True)
        try:
            await task_store.store.fail_task(ctx.session_key, task_id, str(exc))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _make_tool_result_chunk(tool_call_id: str, tool_name: str, content: str) -> str:
    """Emit a tool-role message as an SSE chunk (same format OpenAI uses)."""
    chunk = {
        "id": f"chatcmpl-tool-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": content,
                },
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _messages_as_dicts(request: ChatCompletionRequest) -> List[Dict[str, Any]]:
    """Convert Pydantic message models to plain dicts for the OpenAI client."""
    return [msg.model_dump(exclude_none=True) for msg in request.messages]


def _build_task_injection_note(
    completed: list,
    running: list,
) -> Optional[str]:
    """
    Build a system message text describing background task states.
    Returns None if there is nothing to inject.
    """
    parts: List[str] = []

    if completed:
        result_lines = []
        for t in completed:
            if t.error:
                result_lines.append(f"- {t.tool_name}: FAILED — {t.error}")
            else:
                result_lines.append(f"- {t.tool_name}: {t.result}")
        parts.append(
            "The following background tasks have just completed:\n"
            + "\n".join(result_lines)
            + "\n\nYou MUST call the _publish_message tool to deliver each completed result "
            "to the user's app. Format the result clearly for the user. Then briefly "
            "confirm to the user that the results have been sent."
        )

    if running:
        in_progress_lines = [
            f"- {t.tool_name} (started {int(time.monotonic() - t.created_at)}s ago, still running)"
            for t in running
        ]
        parts.append(
            "The following background tasks are still in progress. "
            "If the user asks about them, let them know they are still being worked on:\n"
            + "\n".join(in_progress_lines)
        )

    return "\n\n".join(parts) if parts else None


# We need monotonic for the injection note
import time


# ---------------------------------------------------------------------------
# Main SSE generator
# ---------------------------------------------------------------------------

async def stream_with_dify_tools(
    request: ChatCompletionRequest,
    client: AsyncOpenAI,
) -> AsyncIterator[str]:
    """
    Async generator yielding SSE strings for the HTTP response.

    Wraps the upstream LLM stream, intercepts Dify tool calls, handles
    the two-pass pattern (ack turn + assistant spoken turn), and injects
    background task state from the task store on each turn.
    """
    settings = get_settings()
    ctx = RequestContext(request)

    # --- Session merge: inject any pending general notes ---
    base_messages = _messages_as_dicts(request)
    if ctx.app_id:
        merged_messages = await session_store.store.merge_into(base_messages, ctx.session_key)
    else:
        merged_messages = base_messages

    # --- Task store injection: completed/running background task states ---
    if ctx.app_id:
        completed_tasks, running_tasks = await task_store.store.get_pending_injection(ctx.session_key)
        injection_note = _build_task_injection_note(completed_tasks, running_tasks)
        if injection_note:
            merged_messages = list(merged_messages) + [{"role": "system", "content": injection_note}]
            logger.info(
                "Injected task states: %d completed, %d running",
                len(completed_tasks), len(running_tasks),
            )

    # --- Build merged tool list ---
    dify_tools = registry.build_openai_tools()
    caller_tools: List[Dict[str, Any]] = [t.model_dump() for t in (request.tools or [])]
    all_tools = caller_tools + [
        t for t in dify_tools
        if t["function"]["name"] not in {ct["function"]["name"] for ct in caller_tools}
    ]

    model = request.model or settings.openai_model

    # === FIRST UPSTREAM CALL ===
    try:
        first_response = await client.chat.completions.create(
            model=model,
            messages=merged_messages,
            tools=all_tools if all_tools else None,
            tool_choice="auto" if all_tools else None,
            modalities=request.modalities,
            audio=request.audio,
            response_format=request.response_format.model_dump() if request.response_format else None,
            stream=True,
            stream_options=request.stream_options,
        )
    except Exception as e:
        logger.error("Upstream LLM error: %s", e, exc_info=True)
        raise

    # Accumulate tool calls across streamed chunks
    accumulated_tool_calls: Dict[int, Dict[str, Any]] = {}
    finish_reason: Optional[str] = None
    finish_chunk_dict: Optional[Dict[str, Any]] = None
    assistant_content_parts: List[str] = []

    async for chunk in first_response:
        chunk_dict = chunk.model_dump()
        if not chunk_dict.get("choices"):
            continue
        choice = chunk_dict["choices"][0]
        delta = choice.get("delta") or {}
        reason = choice.get("finish_reason")
        if reason:
            finish_reason = reason

        # Accumulate content
        if delta.get("content"):
            assistant_content_parts.append(delta["content"])

        # Accumulate tool_calls deltas
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            idx = tc.get("index", 0)
            if idx not in accumulated_tool_calls:
                accumulated_tool_calls[idx] = {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": ""},
                }
            if tc.get("id"):
                accumulated_tool_calls[idx]["id"] = tc["id"]
            if fn.get("name"):
                accumulated_tool_calls[idx]["function"]["name"] = fn["name"]
            accumulated_tool_calls[idx]["function"]["arguments"] += fn.get("arguments", "") or ""

        if reason == "tool_calls":
            # Capture finish chunk; don't yield yet — decide below
            finish_chunk_dict = chunk_dict
        else:
            yield f"data: {json.dumps(chunk_dict)}\n\n"

    # === HANDLE TOOL CALLS ===
    if finish_reason == "tool_calls" and accumulated_tool_calls:
        dify_calls = []
        passthrough_calls = []
        for tc in accumulated_tool_calls.values():
            name = tc["function"]["name"]
            if registry.is_dify_tool(name):
                dify_calls.append(tc)
            else:
                passthrough_calls.append(tc)

        # Case A: only non-Dify tool calls (e.g. _publish_message)
        # Forward the finish chunk to Agora and let it handle execution.
        if passthrough_calls and not dify_calls:
            logger.info(
                "Case A: passthrough-only tool calls %s — forwarding finish chunk to Agora",
                [tc["function"]["name"] for tc in passthrough_calls],
            )
            if finish_chunk_dict:
                yield f"data: {json.dumps(finish_chunk_dict)}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Case B or C: Dify tools present — handle internally
        assistant_msg_dict: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(assistant_content_parts) or None,
            "tool_calls": list(accumulated_tool_calls.values()),
        }
        second_pass_messages = list(merged_messages) + [assistant_msg_dict]

        for tc in dify_calls:
            tool_name = tc["function"]["name"]
            tool_call_id = tc["id"] or f"call_{uuid.uuid4().hex[:8]}"
            tool_def = registry.get_tool(tool_name)
            assert tool_def is not None

            try:
                llm_args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
            except json.JSONDecodeError:
                llm_args = {}
                logger.warning("Could not parse tool args for '%s': %s", tool_name, tc["function"]["arguments"])

            if tool_def.mode == "sync":
                logger.info("Sync Dify call for tool '%s'", tool_name)
                result = await _call_dify(tool_def, llm_args, ctx)
                yield _make_tool_result_chunk(tool_call_id, tool_name, result)
                second_pass_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": result,
                })
            else:
                # Async: emit synthetic ack, fire background task
                yield _make_tool_result_chunk(tool_call_id, tool_name, tool_def.synthetic_ack)
                second_pass_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": tool_def.synthetic_ack,
                })
                if ctx.app_id:
                    task_id = await task_store.store.create_task(ctx.session_key, tool_name, llm_args)
                    asyncio.create_task(
                        _run_dify_background(tool_def, llm_args, ctx, task_id),
                        name=f"dify-{tool_name}-{task_id[:8]}",
                    )
                    logger.info("Spawned background Dify task '%s' (id=%s)", tool_name, task_id)
                else:
                    logger.warning(
                        "Async tool '%s' called but no session context — result will not be delivered",
                        tool_name,
                    )

        # === SECOND UPSTREAM CALL (spoken ack turn) ===
        try:
            second_response = await client.chat.completions.create(
                model=model,
                messages=second_pass_messages,
                tools=all_tools if all_tools else None,
                tool_choice="none",
                modalities=request.modalities,
                audio=request.audio,
                response_format=request.response_format.model_dump() if request.response_format else None,
                stream=True,
                stream_options=request.stream_options,
            )
            async for chunk in second_response:
                yield f"data: {json.dumps(chunk.model_dump())}\n\n"
        except Exception as e:
            logger.error("Second-pass upstream LLM error: %s", e, exc_info=True)

    yield "data: [DONE]\n\n"
