"""
SSE streaming pass-through with Dify tool-call interception.

Flow for each /chat/completions request:
  1. Upstream LLM is called with Dify tools injected.
  2. All non-tool-call chunks are forwarded to the client as-is.
  3. When the LLM finishes with finish_reason == "tool_calls":
       a. Each Dify-registered tool call gets a synthetic tool-result chunk
          (the synthetic_ack text) emitted immediately.
       b. A background asyncio task is spawned to run the Dify workflow and
          deliver results via RTM + session memory.
       c. The LLM is called a SECOND TIME with the augmented messages so it
          produces a natural spoken acknowledgement ("One sec, checking…").
       d. Those second-pass chunks are forwarded to the client.
  4. Stream ends with "data: [DONE]\n\n".
"""

import asyncio
import json
import logging
import traceback
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from app import dify_client, rtm_publisher, session_store
from app.schemas import ChatCompletionRequest
from app.settings import get_settings
from app.tool_registry import ToolDef, registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context extracted from the incoming Agora ConvoAI request
# ---------------------------------------------------------------------------

class RequestContext:
    """Metadata extracted from the ChatCompletionRequest context field."""

    def __init__(self, req: ChatCompletionRequest) -> None:
        ctx: Dict[str, Any] = req.context or {}
        self.app_id: str = ctx.get("app_id", "") or ctx.get("appId", "")
        self.channel_name: str = ctx.get("channel_name", "") or ctx.get("channelName", "")
        self.user_id: str = ctx.get("user_id", "") or ctx.get("userId", "")

        if not self.app_id:
            logger.warning(
                "No app_id in request context — RTM delivery and session memory disabled. "
                "Agora ConvoAI should populate context.app_id; check your agent join config."
            )

    @property
    def session_key(self) -> str:
        return f"{self.app_id}:{self.channel_name}:{self.user_id}"

    @property
    def rtm_enabled(self) -> bool:
        return bool(self.app_id and self.channel_name)


# ---------------------------------------------------------------------------
# Background task: run Dify + deliver results
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


async def _run_dify_and_deliver(
    tool_def: ToolDef,
    llm_args: Dict[str, Any],
    ctx: RequestContext,
) -> None:
    """
    Background task (fire-and-forget) for async-mode tools.

    1. Calls the Dify workflow/chat endpoint.
    2. Publishes the result to RTM so the client receives it immediately.
    3. Appends a system note to session memory so the LLM knows on the next turn.
    """
    logger.info("Background: starting Dify task '%s' with args=%s", tool_def.name, llm_args)
    try:
        result = await _call_dify(tool_def, llm_args, ctx)

        rtm_message = f"{tool_def.rtm_prefix}{result}"

        # Deliver in parallel: RTM (client-facing) + session memory (LLM-facing)
        tasks = [session_store.store.append_tool_result(ctx.session_key, tool_def.name, result)]
        if ctx.rtm_enabled:
            tasks.append(rtm_publisher.publish(ctx.app_id, ctx.channel_name, rtm_message))
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("Background: Dify task '%s' complete, results delivered.", tool_def.name)

    except Exception as exc:
        logger.error("Background: Dify task '%s' failed: %s", tool_def.name, exc, exc_info=True)
        try:
            await session_store.store.append_task_failure(
                ctx.session_key, tool_def.name, str(exc)
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SSE generator
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
    result = []
    for msg in request.messages:
        d = msg.model_dump(exclude_none=True)
        result.append(d)
    return result


async def stream_with_dify_tools(
    request: ChatCompletionRequest,
    client: AsyncOpenAI,
) -> AsyncIterator[str]:
    """
    Async generator yielding SSE strings for the HTTP response.

    Wraps the upstream LLM stream, intercepts Dify tool calls, and handles
    the two-pass pattern (ack turn + assistant spoken turn).
    """
    settings = get_settings()
    ctx = RequestContext(request)

    # --- Session merge: inject any pending background-task results ---
    base_messages = _messages_as_dicts(request)
    if ctx.app_id:
        merged_messages = await session_store.store.merge_into(base_messages, ctx.session_key)
    else:
        merged_messages = base_messages

    # --- Build merged tool list ---
    dify_tools = registry.build_openai_tools()
    caller_tools: List[Dict[str, Any]] = [t.model_dump() for t in (request.tools or [])]
    all_tools = caller_tools + [t for t in dify_tools if t["function"]["name"] not in {ct["function"]["name"] for ct in caller_tools}]

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
    first_pass_chunks: List[Dict[str, Any]] = []
    finish_reason: Optional[str] = None
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

        # Accumulate content for session persistence
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
            # Merge id if we get it later
            if tc.get("id"):
                accumulated_tool_calls[idx]["id"] = tc["id"]
            if fn.get("name"):
                accumulated_tool_calls[idx]["function"]["name"] = fn["name"]
            accumulated_tool_calls[idx]["function"]["arguments"] += fn.get("arguments", "") or ""

        first_pass_chunks.append(chunk_dict)
        # Forward all chunks except the finish chunk (we handle that below)
        if reason != "tool_calls":
            yield f"data: {json.dumps(chunk_dict)}\n\n"

    # === HANDLE TOOL CALLS ===
    if finish_reason == "tool_calls" and accumulated_tool_calls:
        dify_calls = []
        non_dify_calls = []
        for tc in accumulated_tool_calls.values():
            name = tc["function"]["name"]
            if registry.is_dify_tool(name):
                dify_calls.append(tc)
            else:
                non_dify_calls.append(tc)

        # Forward the finish chunk for non-Dify tool calls (if any)
        # and add the assistant message to context
        assistant_msg_dict: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(assistant_content_parts) or None,
            "tool_calls": list(accumulated_tool_calls.values()),
        }

        second_pass_messages = list(merged_messages) + [assistant_msg_dict]

        # For each Dify tool call: branch on sync vs async mode
        for tc in dify_calls:
            tool_name = tc["function"]["name"]
            tool_call_id = tc["id"] or f"call_{uuid.uuid4().hex[:8]}"
            tool_def = registry.get_tool(tool_name)
            assert tool_def is not None

            # Parse LLM-provided arguments
            try:
                llm_args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
            except json.JSONDecodeError:
                llm_args = {}
                logger.warning("Could not parse tool args for '%s': %s", tool_name, tc["function"]["arguments"])

            if tool_def.mode == "sync":
                # Await Dify result inline; 2nd LLM call will speak the real answer
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
                # Async: emit synthetic ack immediately, deliver real result out-of-band
                yield _make_tool_result_chunk(tool_call_id, tool_name, tool_def.synthetic_ack)
                second_pass_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": tool_def.synthetic_ack,
                })
                asyncio.create_task(
                    _run_dify_and_deliver(tool_def, llm_args, ctx),
                    name=f"dify-{tool_name}-{uuid.uuid4().hex[:6]}",
                )
                logger.info("Spawned background Dify task for tool '%s'", tool_name)

        # === SECOND UPSTREAM CALL (spoken ack turn) ===
        try:
            second_response = await client.chat.completions.create(
                model=model,
                messages=second_pass_messages,
                tools=all_tools if all_tools else None,
                tool_choice="none",  # Don't call tools again in the ack turn
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
            # Don't re-raise; we already sent the synthetic ack

    yield "data: [DONE]\n\n"
