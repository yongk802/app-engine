# app-engine

A minimal, standalone host runtime for **Atrium-style apps** — extracted so the
apps (and this engine) can be open-sourced without open-sourcing Atrium.

An "app" is a directory with an `app.json` manifest and an `index.html` (or an
`entry_point` backend). The engine discovers apps, serves them in sandboxed
iframes, and provides the small host contract they rely on.

## Run

```bash
python -m venv .venv
# macOS/Linux: . .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
APP_ENGINE_APPS_DIR=/path/to/apps python engine.py
# open http://127.0.0.1:8770
```

On Windows PowerShell, set the apps directory with
`$env:APP_ENGINE_APPS_DIR = "C:\path\to\apps"` before starting the engine.

## Local tutor setup

Chat-enabled apps use [Ollama](https://ollama.com/) and remain strictly local:
prompts and responses never leave your computer. Open **⚙ Local AI settings**
in the app sidebar. App-engine will:

1. inspect memory, disk space, platform, and any existing Ollama installation;
2. recommend **Quality** (Qwen3 8B, about 5.2 GB) on computers with at least
   16 GB RAM, or **Compatibility** (Qwen3 1.7B, about 1.4 GB) for less capable
   computers;
3. open the official Ollama installer after you review and confirm the plan;
4. start an installed Ollama runtime when needed;
5. download the selected model with visible progress; and
6. enable chat only when the runtime and model are ready.

App-engine never downloads a model without confirmation. It only offers to
remove models that it downloaded itself and leaves all other Ollama models
untouched. Installation opens Ollama's official platform installer so normal
macOS, Windows, or Linux security and privilege prompts remain visible.

If setup is interrupted, reopen Local AI settings and retry. Ollama reuses
already downloaded layers. Choose Compatibility if Quality is too slow or runs
out of memory. **Check again** refreshes runtime and model detection without
changing the system.

## The host↔app contract

| Endpoint | Purpose |
|---|---|
| `GET /api/apps` | discovered app list (launcher metadata) |
| `GET /state` | `{csrf_token, session}` — single-user stub |
| `GET/PUT /api/app-state/{id}` | per-app JSON blob (a `localStorage` replacement, 100 KB) |
| `POST /api/app-chat` | typed SSE stream to the selected local Ollama model |
| `GET /api/local-ai/status` | hardware recommendation, readiness, and installed model state |
| `GET/POST /api/local-ai/*` | confirmed installation, model pull, verification, and diagnostics |
| `GET /apps/{id}/…` | app static files, **or** reverse-proxy to the app's `entry_point` |
| `GET /apps/{file.js}` | shared root files (e.g. `app-state-bridge.js`) |

## app.json

```json
{
  "id": "my-app",
  "label": "My App",
  "icon": "✨",
  "sandbox": "allow-scripts allow-same-origin",
  "chat_enabled": false,
  "chat_system_prompt": "",
  "entry_point": ""
}
```

- **Pure-frontend app** — omit `entry_point`; persist via `AppState` (`app-state-bridge.js`) → `/api/app-state`.
- **App with its own backend** — set `entry_point` (e.g. `http://localhost:8550`); the engine reverse-proxies `/apps/{id}/…` to it and injects `<base href>`.
- **Tutor chat** — set `chat_enabled: true` + a `chat_system_prompt`; the launcher renders a chat panel wired to `/api/app-chat`.

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `APP_ENGINE_APPS_DIR` | `./apps` | directory whose children are apps |
| `APP_ENGINE_STATE_DIR` | `~/.config/app-engine/app-state` | per-app state storage |
| `APP_ENGINE_HOST` / `APP_ENGINE_PORT` | `127.0.0.1` / `8770` | bind address |

The Ollama endpoint is stored in `local-ai.json` under the state directory and
must resolve to loopback (`127.0.0.1`, `localhost`, or `::1`). Remote and cloud
inference endpoints are deliberately rejected.

## Test

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

To compare permissively licensed local tutor models against every chat-enabled
app, install the candidate Ollama models and run:

```bash
ollama pull qwen3:4b
ollama pull phi4-mini
python scripts/benchmark_tutors.py
```

The runner uses only the loopback Ollama API and writes auditable JSON and
Markdown reports under `benchmarks/results/`. See `benchmarks/tutor-cases.json`
for the factual criteria. A model is eligible only when every critical case
passes and transport failures stay at or below 5%.

## Relation to Atrium

Atrium serves the same contract internally (`atrium/apps_routes.py`,
`atrium/apps_server.py`, `atrium/app_chat.py`). This engine is the decoupled,
dependency-free subset: no auth stack, no settings merge, no workspace tools —
just what an app touches at runtime. Apps written for one run unmodified on the
other.
