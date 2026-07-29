# app-engine

A minimal, standalone host runtime for **Atrium-style apps** — extracted so the
apps (and this engine) can be open-sourced without open-sourcing Atrium.

Free and open source ([MIT](LICENSE)), it runs entirely on your own computer,
including its optional local AI.

An "app" is a directory with an `app.json` manifest and an `index.html` (or an
`entry_point` backend). The engine discovers apps, serves them in sandboxed
iframes, and provides the small host contract they rely on.

## New here? Start with the getting-started guide

**[docs/getting-started.md](docs/getting-started.md)** walks you from nothing
installed — through installing **git**, cloning the repo, running the engine,
and setting up a **free local AI (Qwen via Ollama)** — with no prior git
experience assumed. The quick version is below.

## Run

Already have git and Python? Clone and run (see the
[getting-started guide](docs/getting-started.md) if you don't):

```bash
git clone https://github.com/yongk802/app-engine.git
cd app-engine
```

Then:

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

### Getting apps to run

`APP_ENGINE_APPS_DIR` can point at any directory whose subfolders are apps (see
[app.json](#appjson)). The reference companion collection these docs assume is
**personal-apps** — games, tools, and local-AI tutors written for exactly this
host contract (it also provides the shared `app-state-bridge.js`). Clone it
somewhere and point the engine at it:

```bash
APP_ENGINE_APPS_DIR=/path/to/personal-apps python engine.py
```

You can equally write your own apps — the only requirement is an `app.json`
manifest (and usually an `index.html`).

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

### Grounded tutoring

Tutor answers are grounded with the reviewed, app-specific facts in
[`knowledge/tutors.json`](knowledge/tutors.json). App-engine performs simple
local keyword retrieval—no embeddings, vector database, internet request, or
cloud service—and gives Ollama only the relevant excerpts. Responses use a
small structured-answer schema to prevent model planning text from appearing
in the chat. If an answer matches a narrowly defined known misconception,
app-engine retries once with the reviewed correction; it never loops.

Knowledge entries contain a stable ID, app ID, topic keywords, a concise fact,
a human-readable source note, and optional misconception patterns. Contributors
should keep patterns narrow enough to match a false claim rather than merely
the topic. CI requires at least two reviewed entries for every chat-enabled app.
This improves consistency but is not a guarantee that every generated claim is
correct; tutors are instructed to acknowledge when the local reference does not
cover a question.

## The host↔app contract

| Endpoint | Purpose |
|---|---|
| `GET /api/apps` | discovered app list (launcher metadata, incl. version/description/categories/screenshots/compatible) |
| `GET /api/engine` | `{name, version}` — engine identity, for `min_engine_version` gating |
| `GET /api/apps/rejected` | folders that failed manifest validation, with the reason (dev aid) |
| `GET /state` | `{csrf_token, session}` — single-user stub |
| `GET/PUT /api/app-state/{id}` | per-app JSON blob (a `localStorage` replacement, 100 KB) |
| `POST /api/app-chat` | typed SSE stream to the selected local Ollama model |
| `GET /api/local-ai/status` | hardware recommendation, readiness, and installed model state |
| `GET/POST /api/local-ai/*` | confirmed installation, model pull, verification, and diagnostics |
| `GET /apps/{id}/…` | app static files, **or** reverse-proxy to the app's `entry_point` |
| `GET /apps/{file.js}` | shared root files (e.g. `app-state-bridge.js`) |

The host also **posts lifecycle events** to each app's iframe via `postMessage`
as the user switches apps or the tab's visibility changes:

```js
// inside your app:
window.addEventListener('message', (e) => {
  if (e.data?.type === 'appengine:lifecycle') {
    // e.data.event is 'visible' or 'hidden'
  }
});
```

## app.json

The manifest is a shared contract honored by both app-engine and Atrium. Only
`label`, `icon`, and an entry point (an `index.html` **or** an `entry_point`
backend) are required; everything else is optional and older manifests keep
working.

```json
{
  "id": "my-app",
  "label": "My App",
  "icon": "✨",
  "version": "1.0.0",
  "description": "One line shown in the catalog.",
  "categories": ["tools"],
  "author": "Your Name",
  "screenshots": ["media/home.png"],
  "min_engine_version": "1.0.0",
  "permissions": ["microphone"],
  "sandbox": "allow-scripts allow-same-origin",
  "chat_enabled": false,
  "chat_system_prompt": "",
  "entry_point": ""
}
```

| Field | Purpose |
|---|---|
| `id` | Stable unique id (defaults to the folder name). `[a-z0-9][a-z0-9-]*`. |
| `label`, `icon` | **Required.** Display name + emoji shown in the launcher. |
| `version` | Semver; the `major.minor.patch` triple defines update ordering. |
| `description`, `categories`, `author`, `screenshots` | Listing metadata (catalog card, search, category grouping, info dialog). Screenshots are relative paths inside the app. |
| `min_engine_version` | Minimum engine required; older engines show the app as incompatible instead of serving it broken. |
| `permissions` | Browser capabilities the app requests (see below). |
| `sandbox` | iframe `sandbox` attribute (default `allow-scripts`). |
| `entry_point` | Backend URL to reverse-proxy `/apps/{id}/…` to (omit for static apps). |
| `chat_enabled`, `chat_system_prompt`, `chat_knowledge` | Opt into the grounded local-AI tutor panel. |

- **Pure-frontend app** — omit `entry_point`; persist via `AppState` (`app-state-bridge.js`) → `/api/app-state`.
- **App with its own backend** — set `entry_point` (e.g. `http://localhost:8550`); the engine reverse-proxies `/apps/{id}/…` to it and injects `<base href>`.
- **Tutor chat** — set `chat_enabled: true` + a `chat_system_prompt`; the launcher renders a chat panel wired to `/api/app-chat`.

Validate a manifest before shipping (schema in [`app.schema.json`](app.schema.json)):

```bash
python -m app_engine.manifest path/to/my-app
```

A malformed manifest is surfaced in the launcher (and `GET /api/apps/rejected`)
with the specific error, rather than the app silently vanishing.

### Permissions / capabilities

An app declares the sensitive **browser capabilities** it needs in
`permissions` (a shared field also honored by Atrium; a legacy Atrium `allow`
string is accepted too). Recognized values:

```
microphone · camera · display-capture · geolocation · midi
clipboard-read · clipboard-write · fullscreen · autoplay
```

The host delegates the granted set to the app iframe's `allow`
(Permissions-Policy) attribute, and the **browser** prompts for consent at the
point of use (the standard mic/camera/location prompt) — the engine adds no
prompt of its own. The launcher's info dialog (ⓘ) discloses what each app
requests. A powerful feature only actually works when the app's `sandbox`
includes `allow-same-origin` (an opaque origin can't be granted one); unknown
permission tokens are ignored with a warning.

**Host capabilities** are gated separately: local-AI model management
(`/api/local-ai/*` install / pull / start / remove / profile) is a launcher
action, **not** something an app can drive — those endpoints reject requests
originating from an app iframe. Read-only `GET /api/local-ai/status` stays open
so tutor panels can poll readiness.

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `APP_ENGINE_APPS_DIR` | `./apps` | directory whose children are apps |
| `APP_ENGINE_STATE_DIR` | `~/.config/app-engine/app-state` | per-app state storage |
| `APP_ENGINE_HOST` / `APP_ENGINE_PORT` | `127.0.0.1` / `8770` | bind address |
| `APP_ENGINE_APP_STATE_ISOLATION` | `referer` | per-app state isolation: `referer` \| `strict` \| `off` |

The Ollama endpoint is stored in `local-ai.json` under the state directory and
must resolve to loopback (`127.0.0.1`, `localhost`, or `::1`). Remote and cloud
inference endpoints are deliberately rejected.

### Per-app state isolation

Every app is served from one origin, so without a guard an app could
`fetch('/api/app-state/<other-app>')` and read or overwrite another app's data.
The engine gates `/api/app-state/{id}` by the request's **`Referer`** — the one
attribute a browser app's `fetch()` cannot forge (it is a forbidden header), so
a same-origin app iframe's requests reliably carry `/apps/<id>/…`:

- **`referer`** (default) — an identified **cross-app** request is denied (403);
  non-app callers (the top-level launcher, `curl`, tests) are unaffected. This
  blocks the real attack without breaking anything.
- **`strict`** — every request must carry a same-origin `/apps/<id>/` Referer
  matching the target id. Strongest, but only browser-loaded apps work (no `curl`).
- **`off`** — no check (the pre-1.0 behavior).

This is proportionate to app-engine's single-user, loopback scope: it stops a
buggy or casually-malicious app from touching another's state. Full isolation
against an app that suppresses its own Referer requires per-app **origins** —
which is exactly what Atrium does (separate app-server origin + a scoped
app-state token). app-engine stays single-origin by design.

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
just what an app touches at runtime. Apps that stay within the documented
host↔app contract above (`/api/app-state`, `/api/app-chat`, `/state`, and
static assets) run unmodified on either host. Apps that call Atrium-specific
backends beyond that contract (custom `/apps/{id}/api/…` endpoints, shared
Atrium static files, `/chat/brains`, `/config/…`) will load in app-engine but
those particular features won't function here.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
setup, git workflow, and guidelines.

## License

Released under the [MIT License](LICENSE). Free to use, modify, and distribute.
