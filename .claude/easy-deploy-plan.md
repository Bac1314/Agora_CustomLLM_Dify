# Plan: Faster Onboarding & One-Click Deploy

## Context

Today a user who wants to try this wrapper has to: `git clone`, create a venv, install requirements, copy `.env.example`, fill in 4+ API keys by hand, edit `config/tools.yaml`, then pick a deployment target from the Guide and follow provider-specific steps. The project already ships deploy *config files* (`Dockerfile`, `render.yaml`, `Procfile`, `deploy/custom-llm.service`, `docker-compose.yml`), but no one-click *trigger* and no interactive *configuration* step.

**Goal:** reduce time-to-first-successful-call for both audiences equally:
- **Agora customers / field demos** — want a working demo in minutes without cloning, ideally a button-click.
- **Developers evaluating the project** — want fewer manual steps to first call, and a polished local onboarding if they fork.

**Intended outcome:** a phased rollout — start with the highest-ROI / lowest-risk improvements (deploy buttons, published Docker image), layer on a CLI wizard, and reserve the built-in web setup UI for later.

---

## Recommended approach — phased

### Phase 1: One-click deploy buttons + published Docker image  *(highest ROI)*

Covers both audiences with minimal code. Non-devs click a button; devs `docker run`.

**1a. Deploy buttons in README.md**

Add a badge row near the top of `README.md` linking to each provider's deploy URL. Each provider reads its existing config file and prompts the user for secrets in its own dashboard.

| Provider | Config file | New file needed | Button URL pattern |
|---|---|---|---|
| Render | `render.yaml` (exists) | — | `https://render.com/deploy?repo=<GITHUB_URL>` |
| Railway | — | `railway.json` | `https://railway.app/new/template?template=<GITHUB_URL>` |
| Fly.io | `Dockerfile` (exists) | optional `fly.toml` | link to `fly launch` docs |
| DigitalOcean App Platform | — | `.do/app.yaml` | `https://cloud.digitalocean.com/apps/new?repo=<GITHUB_URL>` |
| Heroku-style (Heroku, Coolify, Dokku) | `Procfile` (exists) | `app.json` | `https://heroku.com/deploy?template=<GITHUB_URL>` |

**Files to add:**
- `app.json` — Heroku deploy button schema. Lists env vars from `.env.example` with `required: true` for `OPENAI_API_KEY`, optional for `WRAPPER_API_KEY`, `DIFY_WEATHER_API_KEY`, etc. Maps each to description + whether it's a secret.
- `railway.json` — Railway template config (build: Dockerfile, healthcheck: `/health`, same env-var list).
- `.do/app.yaml` — DigitalOcean spec (service type web, Dockerfile build, port 8000, same env vars).
- Optional: `fly.toml` seed so `fly launch --copy-config` works without prompts.

**Files to modify:**
- `README.md` — add badge row (right after the hero image) with deploy buttons. Also add a new "One-click deploy" section above the existing `## Deployment` table that lists provider buttons. Keep the existing table — buttons are the fast path, the table stays as the reference.
- `render.yaml` — extend `envVars` list to include the full `.env.example` set so Render prompts for all of them, not just `OPENAI_API_KEY` + `DIFY_WEATHER_API_KEY`. Use `sync: false` for secrets.

**1b. Publish pre-built image to GHCR**

Users who can already run Docker skip `git clone` and build entirely:

```bash
docker run -p 8000:8000 --env-file .env ghcr.io/<owner>/agora-custom-llm-dify:latest
```

**Files to add:**
- `.github/workflows/docker-publish.yml` — GitHub Action that builds `Dockerfile` and pushes to `ghcr.io/<owner>/agora-custom-llm-dify` on: (a) push to `main` → `:latest` + `:sha-<short>`, (b) tag `v*.*.*` → `:<version>` + `:latest`. Use `docker/build-push-action@v5`, multi-arch (amd64 + arm64) so Apple Silicon dev boxes work.
- `README.md` — add a "Run with Docker (pre-built image)" subsection under the Docker section.

**Verification (Phase 1):**
- Click the Render deploy button from a fresh GitHub repo → deploy completes, `/health` returns 200.
- `docker run --rm -p 8000:8000 -e OPENAI_API_KEY=... -e DIFY_WEATHER_API_KEY=... ghcr.io/<owner>/agora-custom-llm-dify:latest` → `/health` returns 200.
- Push a tag `v0.1.0` → verify GHCR has `:0.1.0` and `:latest` images for both architectures.

---

### Phase 2: Interactive CLI setup wizard  *(middle ground; dev-friendly)*

One command to turn a fresh clone into a working local install.

**Files to add:**
- `setup.py` (or `scripts/setup.py`) — a single-file Python script using only stdlib (no deps required before install). Uses `argparse` + `input()` prompts. Flow:
  1. Check Python >= 3.11; create `.venv` if missing; install `requirements.txt` into it.
  2. Prompt for each variable in `.env.example`, showing the current value if `.env` already exists (edit mode). Mask secret inputs.
  3. **Validate keys live**: hit `${OPENAI_BASE_URL}/models` with the key; for each `DIFY_*_API_KEY`, hit the corresponding `base_url` from `tools.yaml` with a dry `/info` call (or `/parameters`). Print ✓ / ✗ per key.
  4. Write `.env` atomically (write to `.env.tmp`, rename). Chmod 600.
  5. Offer to enable/disable the commented-out example tools in `config/tools.yaml` interactively.
  6. Optionally run `./run.sh --reload` at the end.
- `README.md` — "Quick start" section at top that says `python setup.py` as the one-liner for local install.

**Verification (Phase 2):**
- From a fresh clone: `python setup.py` → produces a working `.env`, validates keys, starts the server; `curl localhost:8000/health` returns 200.
- Re-running `python setup.py` on an existing install edits in place without clobbering secrets.
- Supplying a wrong `OPENAI_API_KEY` → wizard reports ✗ and loops back for that field.

---

### Phase 3: Built-in web setup UI  *(highest polish; deferred)*

A `/setup` route inside the FastAPI app for editing `tools.yaml` and inspecting config live. Highest user value but biggest scope — defer until Phases 1–2 land and we see whether demand exists.

**Design sketch (not yet committed):**
- New module `app/setup_ui.py` mounts `/setup` (static HTML + tiny JS, no build step; use Jinja2 templates that FastAPI already supports).
- Endpoints:
  - `GET /setup` — renders the admin page (gated by `WRAPPER_API_KEY` if set, else localhost-only via a middleware check).
  - `GET /setup/config` — returns current `tools.yaml` + masked env-var presence map.
  - `POST /setup/tools` — validates + writes `tools.yaml`, then reloads the tool registry in-place (no process restart). Requires extending `app/tool_registry.py` with a `reload()` method.
  - `POST /setup/test-tool` — invokes a single Dify tool with user-supplied args for live testing.
  - `GET /setup/agora-snippet` — returns the Agora ConvoAI JSON pre-filled with the deployed wrapper URL + current `WRAPPER_API_KEY`.
- **Stateful config caveat:** writing `tools.yaml` at runtime conflicts with stateless/read-only container filesystems (Cloud Run, ECS Fargate with read-only root). Decide between:
  - (a) feature-flag the write endpoint off by default (`ENABLE_SETUP_UI=true`)
  - (b) persist overrides to a separate mutable path (`/data/tools.overrides.yaml`) that merges over the shipped YAML
  - Recommended: (a) for now — simpler, and read-only view of live config is still valuable on stateless deploys.
- Auth: reuse the existing `WRAPPER_API_KEY` bearer check from `app/main.py`; if unset, bind the route to `127.0.0.1` only.

**Verification (Phase 3):**
- Load `/setup` in a browser, edit a tool description, save → next `/chat/completions` call sees the new schema without restart.
- Click "Test tool" with valid args → Dify responds and the raw result is shown.
- Copy the pre-filled Agora ConvoAI JSON → paste into the Agora REST API → agent starts successfully.

---

## Critical files

**Existing (read, reuse, or extend):**
- `Dockerfile` — already platform-ready via `${PORT}` override; reused for all deploy buttons.
- `render.yaml` — extend env-var list for Phase 1.
- `.env.example` — canonical source of env-var list; each new deploy config file mirrors it.
- `config/tools.yaml` — schema source for Phase 2/3 wizards.
- `app/main.py` — lifespan + auth pattern reused for Phase 3 `/setup` route.
- `app/tool_registry.py` — `registry` singleton needs a `reload()` method in Phase 3.
- `README.md` — primary surface for deploy buttons + quick-start callout.

**New (by phase):**
- Phase 1: `app.json`, `railway.json`, `.do/app.yaml`, `.github/workflows/docker-publish.yml`, optional `fly.toml`.
- Phase 2: `setup.py`.
- Phase 3: `app/setup_ui.py`, `app/templates/setup.html`, registry `reload()` method, settings flag `ENABLE_SETUP_UI`.

---

## Scope recommendation

Do **Phase 1 first as its own PR** — it's self-contained, adds no runtime code, and unblocks field-demo usage. Revisit Phase 2 and Phase 3 as separate efforts based on feedback.

## Next steps

1. User reviews this plan.
2. On approval, move this file to the project's `.claude/` directory for durable reference.
3. Implement Phase 1, verify end-to-end against Render + GHCR, ship as a PR.
