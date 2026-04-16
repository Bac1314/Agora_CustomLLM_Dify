# Concurrency and Architecture Notes

## Session Isolation — Users Don't See Each Other's Data

Every request from Agora ConvoAI carries `app_id`, `channel_name`, and `user_id`. These three fields are combined into a **session key**:

```
session_key = "{app_id}:{channel_name}:{user_id}"
# e.g. "myapp:room1:alice"  vs  "myapp:room2:bob"
```

Both `task_store` and `session_store` are dictionaries keyed by this string. When User B's request arrives, `get_pending_injection("myapp:room2:bob")` only looks in the `"myapp:room2:bob"` bucket — it has no access to Alice's tasks or notes.

```
task_store._sessions = {
    "myapp:room1:alice": [task_abc123, task_def456],  # Alice's tasks
    "myapp:room2:bob":   [],                           # Bob's tasks (empty)
}
```

User B asking "any updates?" with no pending tasks gets an empty injection — the LLM has no knowledge of Alice's background work.

---

## Asyncio Concurrency — Multiple Users, One Thread

The server runs on a single-threaded `asyncio` event loop (via `uvicorn`). It handles many requests concurrently through **cooperative multitasking**: a coroutine runs until it hits an `await`, at which point it suspends and the event loop picks up another coroutine.

```
Event loop tick:
  ├─ Alice's coroutine runs ... hits await client.chat.completions.create() ... suspends
  ├─ Bob's request arrives → new coroutine starts → runs ...
  ├─ Bob's coroutine hits await ... suspends
  ├─ Alice's LLM response arrives → Alice's coroutine resumes → streams chunk to Alice
  └─ ... and so on
```

Since no CPU-bound work or blocking I/O exists (all HTTP calls use `httpx.AsyncClient`), the event loop stays responsive for all users regardless of how many requests are in flight.

---

## Sync Tasks — Only the Requesting User Is Stalled

For `mode: sync` tools (e.g. `get_current_weather`), `_call_dify()` is awaited inline inside the request handler:

```python
result = await _call_dify(tool_def, llm_args, ctx)  # blocks this coroutine
```

This suspends **only Alice's coroutine**. The event loop is free to handle Bob, Carol, and anyone else. Alice's SSE stream simply goes silent until Dify responds (up to the 120s timeout), then the 2nd LLM call runs and her stream continues.

```
Alice:  request → 1st LLM → tool_calls → await _call_dify() ←──── stalled (10s)
                                                   │                         │
Bob:    request → 1st LLM → streams response  ◄───┤ event loop still free   │
Carol:  request → streams response             ◄───┤                         │
                                                   │                         │
Alice:  ◄── Dify responds ───────────────────────────────────────────────────┘
             → 2nd LLM call → streams answer to Alice
```

If sync Dify latency is unacceptable for the user experience, switch the tool to `mode: async` and let `_publish_message` deliver the result on the next turn.

---

## Two-Pass LLM Flow — One HTTP Request, Two Internal LLM Calls

Agora ConvoAI sends a single `POST /chat/completions` and receives a single SSE stream. It has no visibility into the fact that two LLM calls happen internally.

```
Agora → POST /chat/completions ──────────────────────────────────────────────────────┐
                                                                                      │
         stream_with_dify_tools() (one async generator):                             │
                                                                                      │
         ├─ 1st LLM call → tool_calls chunk suppressed internally                   │
         ├─ synthetic ack chunk  ──────────────────────────────────────────► SSE out │
         ├─ spawn background task                                                     │
         ├─ 2nd LLM call (tool_choice="none")                                        │
         │   "One sec, I'm searching the web for that..."  ────────────────► SSE out │
         └─ "data: [DONE]\n\n"  ────────────────────────────────────────────► SSE out│
                                                                                      │
Agora ← one continuous SSE stream ───────────────────────────────────────────────────┘
```

The first and second LLM calls are connected because they share local variables (`client`, `merged_messages`, `second_pass_messages`) within a single function invocation. No session lookup or request matching is needed.

---

## Deployment Caveat — Single Instance Only

Both `task_store` and `session_store` are **in-memory Python dicts**. This works correctly when the server runs as a single process:

- All requests hit the same process
- Background tasks write to the same dict their spawning request read from
- Session keys reliably identify the same user across turns

With **multiple instances behind a load balancer**, this breaks:

```
Request 1 (Alice, triggers web_search) → hits Instance A → task stored in A's memory
Request 2 (Alice, next turn)           → hits Instance B → B's task_store is empty → no injection
```

For multi-instance deployments, replace the in-memory dicts with a shared external store (Redis is the standard choice). The `TaskStore` and `SessionStore` interfaces are already structured to make this swap straightforward — only the storage backend needs to change.
