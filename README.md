# Agora ConvoAI Custom LLM Wrapper — Dify Edition

An OpenAI-compatible `/chat/completions` proxy that sits between Agora ConvoAI and your upstream LLM. It registers Dify workflows as LLM-callable tools and supports two execution modes per tool:

- **`async`** (default) — fires the Dify call in the background, speaks a synthetic ack immediately, delivers the real result via **Agora RTM** + **session memory** when Dify finishes.
- **`sync`** — awaits the Dify result inline so the LLM can speak the actual answer in the same turn. Ideal for quick-response tools like weather or time lookups.

```
User (RTC) → Agora ConvoAI → [this wrapper] → Upstream LLM
                                    │
                          async ────┤ on tool_call → Dify (background)
                                    │                      │
                                    │        RTM message ←┘ (client receives it)
                                    │        session note ←┘ (LLM knows next turn)
                                    │
                           sync ────┘ on tool_call → Dify (await)
                                                          │
                                         real result → 2nd LLM call → spoken aloud
```

## Requirements

- Python 3.11+
- An OpenAI-compatible LLM API (OpenAI, Azure, Groq, Ollama, …)
- A Dify account/deployment with at least one Workflow or Chatflow app
- An Agora account with App ID + Customer credentials (for RTM delivery)

## Setup

```bash
cd Agora_CustomLLM_Dify

# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env — fill in OPENAI_API_KEY, AGORA_*, DIFY_* keys

# 4. Configure tools
# Edit config/tools.yaml — see the comments and example entry
```

## Running locally

```bash
./run.sh           # production mode
./run.sh --reload  # auto-reload on code changes
```

Health check:
```bash
curl http://localhost:8000/health
# {"status":"ok","tools":["web_search_and_summarize"]}
```

Test with a sample Agora-shaped request:
```bash
curl -N -X POST http://localhost:8000/chat/completions \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/agora_request.json
```

## Running tests

```bash
pip install pytest pytest-asyncio respx
pytest tests/ -v
```

## Deploying with systemd

```bash
# On the server
mkdir -p /opt/custom-llm
cp -r . /opt/custom-llm/
cd /opt/custom-llm
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # then fill in production values

# Install and enable the service
sudo cp deploy/custom-llm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable custom-llm
sudo systemctl start custom-llm

# Tail logs
sudo journalctl -u custom-llm -f
```

## Pointing Agora ConvoAI at this wrapper

In your Agora ConvoAI agent join config, set:
```json
{
  "llm": {
    "vendor": "custom",
    "url": "https://your-server-ip-or-domain/chat/completions",
    "api_key": "any-non-empty-string",
    "model": "gpt-4o-mini"
  }
}
```

## Adding a new Dify tool

Edit `config/tools.yaml` — no code changes required:

```yaml
tools:
  - name: my_new_tool
    description: "Describe what this tool does and when the LLM should use it."
    parameters:
      type: object
      properties:
        my_arg: {type: string, description: "What this argument is"}
      required: [my_arg]
    dify:
      endpoint: workflow          # "workflow" or "chat"
      base_url: https://api.dify.ai/v1
      api_key_env: DIFY_MY_TOOL_KEY   # add this key to .env
      input_mapping:
        my_arg: my_arg            # LLM arg name → Dify input name
      user_field: "{user_id}"
    synthetic_ack: "I'm on it — I'll get back to you shortly."
    rtm_prefix: "[My Tool] "
    mode: async                   # "async" (default) or "sync"
```

**Choosing a mode:**

| `mode` | When to use | LLM behaviour |
|--------|-------------|---------------|
| `async` | Long-running tasks (search, analysis) | Speaks `synthetic_ack` immediately; real result arrives via RTM + session memory |
| `sync` | Quick lookups (weather, time, short queries) | Awaits Dify result, then speaks the actual answer in the same turn |

For `sync` tools, `synthetic_ack` and `rtm_prefix` are not used.

Then add to `.env`:
```
DIFY_MY_TOOL_KEY=app-xxxxxxxxxxxx
```

Restart the server. The new tool is immediately available to the LLM.

## Environment variables

| Variable | Description |
|---|---|
| `OPENAI_BASE_URL` | OpenAI-compatible base URL (default: `https://api.openai.com/v1`) |
| `OPENAI_API_KEY` | API key for upstream LLM |
| `OPENAI_MODEL` | Model name (default: `gpt-4o-mini`) |
| `AGORA_APP_ID` | Agora App ID (from Agora Console) |
| `AGORA_APP_CERTIFICATE` | Agora App Certificate |
| `AGORA_RTM_SENDER_UID` | UID used when publishing RTM messages (default: `custom-llm-wrapper`) |
| `AGORA_CUSTOMER_ID` | Customer ID for Agora REST API (from Console → Developer Toolkit) |
| `AGORA_CUSTOMER_SECRET` | Customer Secret for Agora REST API |
| `DIFY_*_API_KEY` | Per-tool Dify API keys (names referenced in `tools.yaml`) |
| `APP_HOST` | Bind address (default: `0.0.0.0`) |
| `APP_PORT` | Port (default: `8000`) |
| `TOOLS_CONFIG` | Path to tools YAML (default: `config/tools.yaml`) |
| `LOG_LEVEL` | Log level (default: `INFO`) |

## Architecture

### Key components

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app, `/chat/completions` endpoint, lifespan |
| `app/schemas.py` | Pydantic models (OpenAI-compatible request/response) |
| `app/settings.py` | Env-var configuration via pydantic-settings |
| `app/tool_registry.py` | Load YAML tools, build OpenAI schemas, dispatch to Dify |
| `app/stream_handler.py` | SSE pass-through, tool-call interception, 2-pass LLM flow, sync/async mode branching |
| `app/session_store.py` | Per-session memory: injects Dify results into next LLM turn |
| `app/dify_client.py` | Async HTTP calls to Dify `/workflows/run` and `/chat-messages` |
| `app/rtm_publisher.py` | Publishes results to Agora RTM channel via REST API |
| `app/llm_client.py` | Configurable AsyncOpenAI client |
| `config/tools.yaml` | Dify tool registry (add tools here, no code changes needed) |
