# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (Python 3.11+ required)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the server (reads .env automatically)
./run.sh             # production
./run.sh --reload    # auto-reload on code changes

# Run all tests
pip install pytest pytest-asyncio respx
pytest tests/ -v

# Run a single test file
pytest tests/test_tool_registry.py -v

# Run a single test by name
pytest tests/test_session_store.py::test_system_note_injected_on_next_merge -v

# Health check (server must be running)
curl http://localhost:8000/health

# Test with a sample Agora ConvoAI payload
curl -N -X POST http://localhost:8000/chat/completions \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/agora_request.json
```

## Architecture

This is an OpenAI-compatible `/chat/completions` SSE proxy that sits between Agora ConvoAI and an upstream LLM. When the LLM invokes a registered Dify tool via function calling, the wrapper executes the Dify call in one of two modes controlled by the `mode` field in `tools.yaml`:

- **`async`** (default) — fires the Dify workflow in a background task, returns a synthetic acknowledgement so the LLM can speak immediately. The result is stored in the **task store**. On the next turn, the LLM sees the completed result in its context and calls Agora's built-in **`_publish_message`** tool to deliver it to the user's app.
- **`sync`** — awaits the Dify result inline, feeds the real result to the 2nd LLM call so the LLM speaks the actual answer immediately.

### Request flow

```
POST /chat/completions (from Agora ConvoAI)
  │
  ├─ Extract session key from request (app_id:channel_name:user_id)
  ├─ Merge pending session notes from SessionStore into message list
  ├─ Inject completed/running task states from TaskStore as system message
  ├─ Inject Dify tool schemas from ToolRegistry (merged with caller-supplied tools)
  │
  ├─ [1st upstream LLM call]
  │   ├─ Pass-through chunks → client SSE stream
  │   └─ On finish_reason == "tool_calls":
  │       ├─ Case A — only non-Dify tools (e.g. _publish_message):
  │       │   └─ Forward finish chunk as-is → Agora executes tool natively
  │       ├─ Case B — only Dify tools:
  │       │   ├─ mode == "async":
  │       │   │   ├─ Emit synthetic tool-result chunk (tool_def.synthetic_ack)
  │       │   │   ├─ asyncio.create_task(_run_dify_background)  ← fire-and-forget
  │       │   │   └─ [2nd upstream LLM call] → "One sec, I'm checking…" turn → client
  │       │   └─ mode == "sync":
  │       │       ├─ await _call_dify(...)  ← blocks stream until Dify responds
  │       │       ├─ Emit real tool-result chunk
  │       │       └─ [2nd upstream LLM call] → LLM speaks the real answer → client
  │       └─ Case C — mixed Dify + non-Dify tools:
  │           └─ Handle Dify tools per Case B; non-Dify calls deferred to next turn
  │
  └─ "data: [DONE]\n\n"

Background task (_run_dify_background)  [async mode only]:
  ├─ await _call_dify(...)  → Dify /workflows/run or /chat-messages (up to 120s)
  └─ task_store.complete_task(session_key, task_id, result)

Next turn (when user speaks again):
  ├─ task_store.get_pending_injection() → surfaces completed result as system message
  ├─ System message instructs LLM to call _publish_message with the result
  └─ LLM calls _publish_message → finish chunk forwarded to Agora → Agora delivers to user app
```

### Key components

- **`app/stream_handler.py`** — The core logic. Accumulates tool_call deltas across chunks, handles the 2-pass LLM flow. Implements Case A/B/C tool call routing. `_call_dify()` executes the Dify HTTP call; `_run_dify_background()` is the fire-and-forget task for async-mode tools. Most complexity lives here.
- **`app/task_store.py`** — In-memory per-session background task tracker keyed by `app_id:channel_name:user_id`. Tracks task lifecycle: `running → completed → reported`. `get_pending_injection()` surfaces completed results atomically (marking them reported so they are injected exactly once). 10-min stale task TTL.
- **`app/tool_registry.py`** — Loads `config/tools.yaml` at startup into `ToolDef` objects. Builds OpenAI-format tool schemas. `registry` is a module-level singleton loaded in `main.py` lifespan.
- **`app/session_store.py`** — In-memory dict keyed by `app_id:channel_name:user_id`. Stores general system notes; the next request's `merge_into()` consumes and clears them. 24-hour TTL, 100 extra messages per session max.
- **`app/dify_client.py`** — Async httpx for Dify. `endpoint: workflow` → `/workflows/run` (blocking); `endpoint: chat` → `/chat-messages` (blocking). Returns a string result or error message; never raises.
- **`config/tools.yaml`** — The only file you need to edit to add a new Dify tool. See the inline schema comments. `api_key_env` references an env var name; never put secrets in YAML.

### Adding a Dify tool

1. Add an entry to `config/tools.yaml` — follow the schema in the existing example.
2. Set `mode: sync` if the LLM should speak the Dify result immediately; omit (or set `mode: async`) for fire-and-forget with `_publish_message` delivery.
3. Add the Dify API key env var to `.env` (key name must match `api_key_env` in YAML).
4. Restart the server. No code changes needed.

### Agora ConvoAI integration

Point the agent at this wrapper by setting in the ConvoAI join config:
```json
{ "llm": { "vendor": "custom", "url": "https://your-host/chat/completions", "api_key": "any-non-empty-string" } }
```

The request must contain `app_id`, `channel_name`, and `user_id` (Agora populates these from `params`) for task store and session memory to work. If those are absent, the wrapper still proxies the LLM but logs a warning.

Agora ConvoAI's predefined `_publish_message` tool must be enabled in the agent config for async result delivery to work. When the LLM calls `_publish_message`, the wrapper forwards the tool call to Agora ConvoAI, which executes it natively to push the message to the user's app.

### Environment variables

See `.env.example` for the full list. Critical ones:
- `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` — upstream LLM (any OpenAI-compatible endpoint)
- `AGORA_APP_ID` — used for session keying (optional but recommended)
- `DIFY_*_API_KEY` — one per tool, name referenced in `config/tools.yaml` via `api_key_env`
