# Adding a Dify Tool

This guide walks through creating a new Dify workflow and wiring it into this project as a callable tool. No application code changes are needed — only YAML config and an env var.

---

## Overview

This project intercepts Agora ConvoAI requests and injects Dify workflows as LLM-callable tools via OpenAI function calling. Two delivery modes are available:

| Mode | Behaviour |
|------|-----------|
| `sync` | LLM waits for the Dify result and speaks it immediately |
| `async` | LLM says the `synthetic_ack` immediately; real result stored in task store and injected on the next turn, then delivered via `_publish_message` |

Use **sync** for fast lookups (weather, short queries). Use **async** for slow operations (web search, order lookup, anything that may take several seconds).

---

## Part 1: Build the Workflow in Dify

All steps below happen in the Dify web UI.

### Step 1 — Create a new Workflow app

1. Log in to your Dify instance
2. Click **Create from Blank** → select **Workflow**
3. Give it a descriptive name (e.g. `Get Current Weather`)

### Step 2 — Define input variables in the Start node

1. Click the **Start** node
2. Add one input variable per parameter your tool needs:
   - **Name**: must match the Dify input name you'll use in `input_mapping` (see Part 2)
   - **Type**: Short Text for strings, Number for numeric inputs

> The wrapper sends these inputs from `input_mapping` in `tools.yaml`. Whatever name you give the variable here is what you reference on the **right-hand side** of `input_mapping`.

### Step 3 — Build the workflow logic

Add nodes for your logic. Common patterns:
- **Tool node** — use a built-in Dify plugin (weather, calculator, etc.)
- **HTTP Request node** — call an external REST API directly
- **LLM node** — have a second LLM process or reformat the data
- **Code node** — run Python/JavaScript for data transformation

Connect nodes and pass variables between them using Dify's reference syntax (`{{node.output}}`).

### Step 4 — Add an End node with a string output

1. Add an **End** node as the final step
2. Define at least one **string** output variable (e.g. `result`)
3. Map it to the final text you want returned to the LLM

> The wrapper (`app/dify_client.py`) reads `data.outputs` from the Dify response and joins all non-None string values into a single string. The variable name does not matter — any string output is picked up.

### Step 5 — Test in the Dify UI

Click **Run** in the top-right, fill in your input variables, and confirm the output looks right before publishing.

### Step 6 — Publish and copy the API key

1. Click **Publish**
2. Navigate to the app's **API Access** section
3. Copy the API key — it looks like `app-xxxxxxxxxxxxxxxxxxxxxxxx`

---

## Part 2: Wire It Into the Project

### Step 7 — Add the API key to `.env`

```dotenv
DIFY_YOUR_TOOL_API_KEY=app-xxxxxxxxxxxxxxxxxxxxxxxx
```

The env var name is arbitrary — you'll reference it by name in `tools.yaml`. Convention: `DIFY_<TOOL_NAME>_API_KEY`.

### Step 8 — Add the tool entry to `config/tools.yaml`

Add a new entry under `tools:`. Full schema:

```yaml
  - name: your_tool_name          # snake_case; this is the OpenAI function name the LLM calls
    description: >
      One or two sentences describing what the tool does and when the LLM
      should use it. Be specific — this is what guides the LLM's decision.
    parameters:
      type: object
      properties:
        param_one:
          type: string
          description: "What this parameter means. Include an example value."
        param_two:
          type: string
          description: "Another parameter if needed."
      required:
        - param_one            # list parameters the LLM must always provide
    dify:
      endpoint: workflow       # "workflow" (/workflows/run) or "chat" (/chat-messages)
      base_url: https://api.dify.ai/v1
      api_key_env: DIFY_YOUR_TOOL_API_KEY   # env var name from Step 7
      input_mapping:
        param_one: dify_input_name   # LLM arg name → Dify workflow input variable name
        param_two: another_dify_var  # add one entry per parameter
      user_field: "{user_id}"        # supports {user_id} and {channel_name}
    synthetic_ack: "I'm on it — I'll have an answer for you shortly."   # async only
    mode: sync                      # "sync" or "async" (default: async)
```

**`input_mapping` rules:**
- Key = LLM argument name (left side of `parameters.properties`)
- Value = Dify workflow input variable name (from the Start node in Dify)
- If both sides have the same name, you still must list the mapping explicitly for clarity
- If `input_mapping` is omitted entirely, LLM args are forwarded to Dify as-is

**Choosing `mode`:**

```
Fast response expected (< 3s)?  →  sync
Slow operation or best-effort?  →  async
Want LLM to speak real answer?  →  sync
Want LLM to acknowledge first?  →  async
```

### Step 9 — Restart the server

```bash
./run.sh --reload
```

The registry reloads `tools.yaml` on startup. No code changes are required.

---

## Part 3: Verify

### Health check

```bash
curl http://localhost:8000/health
```

### End-to-end test via curl

```bash
curl -N -X POST http://localhost:8000/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "YOUR TEST PROMPT HERE"}],
    "stream": true,
    "context": {"app_id": "test", "channel_name": "test", "user_id": "test-user"}
  }'
```

**Sync mode:** the SSE stream should include the real Dify result in the LLM's spoken response.

**Async mode:** the SSE stream should include the `synthetic_ack` text; the real result is stored in the task store and injected into the LLM context on the next turn. The LLM then calls `_publish_message` to deliver it to the user's app.

---

## Worked Example: Weather Tool

The `get_current_weather` tool in `config/tools.yaml` is a complete reference. It uses:
- `mode: sync` — LLM speaks the answer immediately
- One input parameter (`location`) mapped directly to the Dify workflow input of the same name
- Dify's built-in weather plugin as the workflow node

To build a similar tool with a different data source, follow the same pattern and swap the Dify workflow internals.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| Tool never called by LLM | `description` is too vague — make it more specific about when to invoke |
| Dify returns empty result | Check `data.outputs` in Dify logs; ensure the End node has a string output variable |
| `api_key_env` warning in server logs | The env var isn't set in `.env` or wasn't exported before starting |
| `input_mapping` mismatch error | Right-hand side value must match the exact variable name in the Dify Start node |
| Sync tool times out | Dify call exceeded 120s limit — consider switching to `async` mode |
