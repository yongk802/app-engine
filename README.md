# app-engine

A minimal, standalone host runtime for **Atrium-style apps** — extracted so the
apps (and this engine) can be open-sourced without open-sourcing Atrium.

An "app" is a directory with an `app.json` manifest and an `index.html` (or an
`entry_point` backend). The engine discovers apps, serves them in sandboxed
iframes, and provides the small host contract they rely on.

## Run

```bash
pip install -r requirements.txt
APP_ENGINE_APPS_DIR=/path/to/apps python engine.py
# open http://127.0.0.1:8770
```

## The host↔app contract

| Endpoint | Purpose |
|---|---|
| `GET /api/apps` | discovered app list (launcher metadata) |
| `GET /state` | `{csrf_token, session}` — single-user stub |
| `GET/PUT /api/app-state/{id}` | per-app JSON blob (a `localStorage` replacement, 100 KB) |
| `POST /api/app-chat` | SSE proxy to any OpenAI-compatible LLM (opt-in via manifest) |
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
| `APP_ENGINE_LLM_BASE` | `http://localhost:11434` | OpenAI-compatible chat endpoint |
| `APP_ENGINE_LLM_MODEL` | `gemma3:4b` | chat model |
| `APP_ENGINE_HOST` / `APP_ENGINE_PORT` | `127.0.0.1` / `8770` | bind address |

## Relation to Atrium

Atrium serves the same contract internally (`atrium/apps_routes.py`,
`atrium/apps_server.py`, `atrium/app_chat.py`). This engine is the decoupled,
dependency-free subset: no auth stack, no settings merge, no workspace tools —
just what an app touches at runtime. Apps written for one run unmodified on the
other.
