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

## App Studio and managed apps

The standalone distribution includes the same App Studio Atrium embeds. After
starting app-engine, open
[`http://127.0.0.1:8770/api/app-engine/studio/`](http://127.0.0.1:8770/api/app-engine/studio/)
to create, import, configure, launch, and diagnose apps. The wizard ships with
static web, generic HTTP, Python/uv, Node.js, Go, and Rust starters. It previews
every filesystem or process plan before applying it and never overwrites an
existing project directory.

Managed apps are language-neutral. A version-2 `app.json` declares argv arrays,
not shell strings; app-engine assigns `PORT`, starts the process, waits for its
HTTP health endpoint, captures logs, and reuses unchanged builds and warm
processes:

```json
{
  "manifest_version": 2,
  "id": "hello-api",
  "label": "Hello API",
  "icon": "👋",
  "default_target": "web",
  "targets": {
    "web": {
      "kind": "web",
      "runtime": {
        "driver": "process",
        "scope": "per_user",
        "install": [{"argv": ["uv", "sync"]}],
        "start": {"argv": ["uv", "run", "python", "main.py"]},
        "health": {"kind": "http", "path": "/health"},
        "idle_timeout_seconds": 300
      }
    }
  }
}
```

The process must bind an HTTP server to `127.0.0.1:$PORT`; `GET /health` must
return success when it is ready. The provider registry deliberately keeps
`kind` open-ended, so future iOS and Android providers can add toolchains,
templates, validation, preparation, and launch behavior without changing the
core manifest parser.

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
| `POST /api/app-multiplayer/{id}/command` | room command for an app that declares `multiplayer`; the host runs the app's room service (see [Multiplayer](#multiplayer-tables-hosted-for-any-game)) |
| `GET /api/app-multiplayer/{id}/health` | `{protocol, rulesVersion, catalogDigest}` of that app's room service |
| `POST /api/players/sign-in` | public-origin mode: username + password → bearer token for a game running elsewhere |
| `POST /api/players/{id}/command`, `GET …/health` | the same room commands for a signed-in player's own game client (bearer token, CORS-open) |
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

## Multiplayer: tables hosted for any game

app-engine runs a **room service** for every app that names a rules module:

```json
{ "multiplayer": { "rules": "multiplayer/rules.mjs", "ai": "ai.mjs", "protocol": 1 } }
```

The service (`app_engine/multiplayer/`, Node.js 18+) is game-agnostic: private
tables with a two-seat lobby and 24 h invitations, moves validated against the
game's own `legalActions` with idempotent commands and a private view per seat,
optional 24 h/72 h turn clocks, presence, chat, WebRTC voice signaling, move
alerts by webhook, single-elimination and round-robin tournaments with AI fill,
a friend roster (shareable `ncf1:` codes, online/away, invitations that seat a
friend directly), and durable JSON storage. The game supplies only its rules.

`rules` is an ES module inside the app whose default export is a constructed
rules module:

```ts
{ rulesVersion, catalogDigest,            // compatibility: mismatched clients get INCOMPATIBLE
  createMatch(decks, seed), legalActions(state, seat), applyAction(state, seat, action),
  viewFor(state, seat), validateDeck(deck), generateDeck(meta, seed),
  chooseAction?(view, actions, seed),     // AI seats and tournament fill (or a separate `ai` module)
  daily?: {validDay, replayDaily, gigResult, rankBoard} }   // optional seeded challenge board
```

The first command for an app starts `node app_engine/multiplayer/server.mjs`
with that module on an ephemeral loopback port, waits for its health check,
keeps its output for diagnostics and restarts it if it dies. Storage is per app
under `APP_ENGINE_STATE_DIR/multiplayer/<id>/` (`rooms.json`, `tournaments.json`,
`friends.json`); that path is the one seam to change for a host-wide roster
later. The app reaches the service only through
`POST /api/app-multiplayer/<id>/command`, gated like `/api/app-state` (its own
origin plus its state capability) with the seat credential in
`X-App-Multiplayer-Credential`; `app-state-bridge.js` exposes it as
`Multiplayer.command(appId, op, payload, credential)`. Apps without the field,
without Node, or with a broken module get `MULTIPLAYER_UNAVAILABLE` and keep
working solo. `APP_ENGINE_MULTIPLAYER_SERVER=http://host:port` (or a JSON
object keyed by app id) relays to a shared service instead, which is how two
machines on a LAN or VPN meet at one table. Night City Table
(`personal-apps/cyberpunk-tcg`) is the reference game; its client prefers this
route and falls back to its own backend proxy on hosts without it.

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `APP_ENGINE_APPS_DIR` | `./apps` | directory whose children are apps |
| `APP_ENGINE_STATE_DIR` | `~/.config/app-engine/app-state` | per-app state storage |
| `APP_ENGINE_HOST` / `APP_ENGINE_PORT` | `127.0.0.1` / `8770` | bind address |
| `APP_ENGINE_MULTIPLAYER_SERVER` | *(unset: run one per app)* | relay room commands to a shared room service instead |
| `APP_ENGINE_PUBLIC_ORIGIN` | *(unset: loopback product)* | the launcher's public origin, e.g. `https://play.example.com`; turns on [public-origin mode](#public-origin-mode-reaching-your-engine-from-anywhere) |
| `APP_ENGINE_ADMIN_SECRET` | *(minted on first public start)* | the owner's sign-in secret; setting a new value rotates it and signs every session out |
| `APP_ENGINE_TRUST_PROXY` | `0` | honour `X-Forwarded-Proto`/`X-Forwarded-For` from a loopback reverse proxy |
| `APP_ENGINE_APP_ORIGINS` | `subdomain` | where apps live on a public server: `subdomain` (`<id>.<host>`, isolated, needs wildcard DNS + certificate) or `same` (`<host>/apps/<id>/`, for a single host name) |
| `APP_ENGINE_RATE_LIMIT` | `20,60` | requests per second and burst allowed per session or address on the player, multiplayer and app-state routes |
| `APP_ENGINE_MAX_PLAYERS` | `50` | how many players the owner may invite |
| `APP_ENGINE_PUBLIC_APPS` | *(unset: all, with a warning)* | comma-separated app ids a public server hosts; others are not listed, served or openable |
| `APP_ENGINE_SMTP_URL`, `APP_ENGINE_MAIL_FROM` | *(unset: draft only)* | let the server email invitations itself |
| `APP_ENGINE_SERVER_NAME` | the public host name | how the server introduces itself in invitations |
| `APP_ENGINE_MAIL_PER_HOUR`, `APP_ENGINE_MAIL_PER_DAY` | `20`, `100` | invitation email caps (plus three per recipient per day) |

The Ollama endpoint is stored in `local-ai.json` under the state directory and
must resolve to loopback (`127.0.0.1`, `localhost`, or `::1`). Remote and cloud
inference endpoints are deliberately rejected.

### Public-origin mode: reaching your engine from anywhere

By default app-engine is a single-user program on `127.0.0.1`: whoever reaches
the port is the administrator, and apps run on `<id>.localhost` origins that
only resolve on the same machine. Binding another address is refused for that
reason. **Public-origin mode** changes both, so one engine on a server can be
used by its owner from any browser — and, once player accounts land, by
invited friends who meet at the multiplayer tables it hosts.

```bash
APP_ENGINE_PUBLIC_ORIGIN=https://play.example.com \
APP_ENGINE_TRUST_PROXY=1 \
APP_ENGINE_APPS_DIR=/srv/personal-apps \
python engine.py
```

What changes when the variable is set:

- Apps are served at `https://<id>.play.example.com`, and the launcher builds
  app URLs from that origin, not from the request. Use the public names even on
  the server itself: the session cookie belongs to the public domain, so
  `127.0.0.1` and `<id>.localhost` are not signed in.
- **Every request needs the owner's session.** The engine mints an admin
  secret on first start (printed once, stored only as a hash in
  `players.json` under the state directory) or adopts `APP_ENGINE_ADMIN_SECRET`.
  Sign in at `https://play.example.com/admin`; the session cookie (`HttpOnly`,
  `Secure`, `SameSite=Lax`, domain-wide so app subdomains carry it) lasts 90
  days and is revoked by **Sign out** or by rotating the secret. HTML
  navigations without it are sent to `/admin`; API calls get `401`. Only
  `/api/engine` and the sign-in page are open. Wrong secrets are limited to
  five attempts per minute per address.
- App responses may be framed only by the public launcher
  (`frame-ancestors https://play.example.com`).
- **Local AI is off.** `/api/app-chat` and `/api/local-ai/*` answer `403`: the
  owner's models never leave the owner's computer.
- `APP_ENGINE_HOST` may now be a public address, but the intended layout is a
  TLS proxy on the same machine forwarding to loopback, with
  `APP_ENGINE_TRUST_PROXY=1` so the engine sees `https` and the real client
  address. Reference Caddy configuration (wildcard certificates need Caddy's
  DNS-challenge plugin for your DNS provider):

```caddyfile
play.example.com, *.play.example.com {
    tls you@example.com {
        dns cloudflare {env.CLOUDFLARE_API_TOKEN}
    }
    reverse_proxy 127.0.0.1:8770
}
```

DNS: `A play.example.com` and `A *.play.example.com` to the server. Install
Node.js 18+ for multiplayer, clone your apps collection (Night City Table
needs its card catalog imported on the server), and run the engine under a
service manager with the variables above.

A worked example for a Hostinger VPS, using the nginx already on the box and
per-name certificates, is in [docs/deploy-hostinger.md](docs/deploy-hostinger.md).

**Only one host name and no wildcard?** A hosting provider's name such as
`srv1242099.hstgr.cloud` has no wildcard, so `<id>.<host>` cannot resolve. Set
`APP_ENGINE_APP_ORIGINS=same`: apps are served under `https://<host>/apps/<id>/`
on the launcher's origin (they then share one browser origin — no per-app
isolation, which the engine says so in its startup log), and Caddy needs only
that one certificate:

```caddyfile
srv1242099.hstgr.cloud {
    reverse_proxy 127.0.0.1:8770
}
```

Players connecting from their own computers never need app subdomains: the
player API lives on the base origin. Prefer the subdomain layout whenever you
can point a domain of your own at the server; without a DNS-challenge plugin,
Caddy's on-demand TLS (`tls { on_demand }` with an `ask` endpoint that
allows `*.play.example.com`) issues each app's certificate over HTTP-01 as it
is first visited.

Abuse limits: the player, multiplayer and app-state routes are metered per
session or address (`APP_ENGINE_RATE_LIMIT`, 429 with `Retry-After`), sign-in
attempts are limited to five a minute per address, the owner may invite at
most `APP_ENGINE_MAX_PLAYERS`, and `audit.log` under the state directory
records sign-ins, invitations, player changes and plan approvals as JSON lines
(never secrets). The startup log warns about a public bind without a proxy,
an https origin without `APP_ENGINE_TRUST_PROXY`, and the shared-origin layout.

#### Players

The owner creates accounts; nobody signs up on their own. From the launcher's
**Players** panel (👥, **Create account**) or the shell:

```bash
app-engine-players invite --username rook --name "Rook" --apps cyberpunk-tcg
app-engine-players list | disable rook | enable rook | reinvite rook | remove rook
```

An invitation is a one-time link (`/join/<code>`, valid seven days) where the
player chooses a password (stored as a salted PBKDF2 hash). A player then
signs in with username and password — at `/sign-in` for the launcher on the
server, or **from inside a game on their own computer**: games list the servers
they know in `app.json` (`multiplayer.servers`, name + https origin), call
`POST /api/players/sign-in`, and send room commands to
`POST /api/players/<app>/command` with the bearer token. That API is CORS-open
because tokens never travel on their own; cookies never reach it. Players see
and can open only the apps on their allow-list (everything else answers
`404`), their saves and app capabilities are their own, and managed apps open
for them once the owner has approved the launch plan — an approval now stands
until the plan changes. Disabling, removing or signing out ends sessions
immediately; the engine re-reads `players.json` when the CLI changes it.

**Inviting by email.** Beside each player, **Email invitation** mints a
fresh one-time link and either sends it from the server or, when the server
has no mail settings, opens your own mail program with the drafted message
(`app-engine-players email rook --game "Night City Table" --from-name Yong`
prints the same draft). To let the server send: `APP_ENGINE_SMTP_URL`
(`smtp://user:pass@host:587` for STARTTLS or `smtps://…:465`) and
`APP_ENGINE_MAIL_FROM`; `APP_ENGINE_SERVER_NAME` is how the server is
introduced. Only the owner can cause mail to leave the server — players have
no mail surface — and the owner is metered too: `APP_ENGINE_MAIL_PER_HOUR`
(20), `APP_ENGINE_MAIL_PER_DAY` (100) and three messages per recipient per
day, refused with `429`, every attempt in `audit.log`.

**Server chat.** A public engine has one text channel for everyone signed in,
shown as a collapsible dock at the bottom of the launcher under whichever app
is open. On the server itself it uses the session; on a player's own
app-engine the dock lists the servers the installed games know and signs in
with the player's username and password, then talks to
`GET/POST /api/players/chat` with the bearer token. Messages are capped at
500 characters, one per second and thirty a minute per person, the last 300
are kept in `chat.json`, and the bar shows who polled in the last 45 seconds.
When a game is in play mode or the browser is full screen, the dock leaves the
layout and waits behind a 💬 tab on the right edge of the screen: rest the
pointer on the tab (or click it) and the chat slides in over the game, and it
slides away when the pointer leaves unless a message is being typed. A new
message from someone else dings and peeks next to the tab for a few seconds;
the bell in the dock bar mutes the sound. Night City Table adds its own chimes
at the table (a friend's message, your move) with a switch under Move alerts.

On the room service, a signed-in player's friend roster is bound to their
account (signing in on another device brings it back), and
`queueJoin`/`queueStatus`/`queueLeave` pair two waiting players into a random
match. See [MULTIPLAYER.md in Night City Table](https://github.com/yongk802/personal-apps/blob/main/cyberpunk-tcg/MULTIPLAYER.md)
for the player's view.

### Per-app state isolation

Apps run on separate `<app-id>.localhost` origins. The launcher gives each app
an unguessable, app-scoped state capability in the URL fragment (fragments are
not sent in HTTP requests or server logs); `app-state-bridge.js` supplies it in
the state request header. A capability for one app cannot access another app's
state. Local-AI mutations use a separate launcher-only capability, so an app
cannot install, start, pull, verify, select, or remove models—even if it
suppresses its `Referer`. Keep app-engine bound to loopback, its default.

## Test

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

See [the three-platform release checklist](docs/release-testing.md) for clean
install and real Ollama testing. CI runs Python 3.10–3.13 on macOS, Windows, and
Linux. The cross-repo checks against `personal-apps` run locally, from a sibling
clone or `PERSONAL_APPS_DIR`, and skip in CI.

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

### Launcher regression tests

Run `npm ci` followed by `npm test` for the browser-launcher tests. These cover managed runtime launch, approval cancellation, browser permissions, and Night City Table focus messages. The Python API tests remain `python -m pytest tests/test_api.py`.
