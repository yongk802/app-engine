# External chat panel and app tools

Approved by the owner on 2026-10-08: a collapsible right panel embeds a configured chat page; one authenticated MCP endpoint exposes installed apps without eagerly starting them; declared app actions run on demand and the external harness owns reasoning and tool orchestration.

The implementation belongs to standalone app-engine. Atrium's vendored package has no launcher, and Atrium already has an external MCP adapter. Existing per-app tutors and public player chat remain independent.

## Interfaces

The launcher loads `/assistant-assets/panel.js` and `panel.css` from package data. The script creates a Chat rail button and panel. It does no network work until opened, remembers the configured URL on the server, and creates an iframe only on explicit Connect. Collapsing preserves the active conversation; Disconnect removes the iframe and releases its memory. Settings include a normal external-page link when embedding is refused. Loading an iframe is not proof of MCP connectivity.

Owner-only REST endpoints require the existing launcher capability (`X-App-Engine-Admin`), launcher host, and owner session. `GET /api/assistant/settings` returns `{chat_url, mcp_url, enabled, last_connected_at}`; `PUT` accepts `{chat_url}`; `POST /api/assistant/key` rotates and returns `{key}` once; `DELETE` revokes it. Only a hash is persisted. HTTP is allowed for loopback chat pages; elsewhere HTTPS is required. No automatic credential transfer to embedded pages, query strings or postMessage.

`POST /api/mcp` serves MCP initialize, ping, tools/list and tools/call; notifications return 202 and GET/DELETE return 405. Stateless authenticated HTTP requests use a bearer key and validated Origin/Host. Revocation is immediate. Settings and keys are disabled for public-origin installations initially: the existing public gate requires cookie identity, and silently bypassing it would change public authority. The panel explains this limitation rather than pretending to connect.

`AppTools.list_tools() -> list[dict]` reads catalog snapshots and bounded declarative `app-tools.json` files only. Built-ins list apps, queue an app open, read app state, and replace state with a required revision check. Responses distinguish queued actions from completed ones. `GET /api/assistant/events?after=N` supplies a bounded queue to open panels; only visible panels poll. State uses existing standalone state files and a 100 KB limit, refuses symlinks, and retains app isolation from browser frames.

Apps may declare backend actions with `app-tools.json`: `{schema_version:1, tools:[{name,description,inputSchema,path}]}`. Tool names are namespaced; paths are strict relative API paths without traversal, queries, fragments or absolute URLs. Discovery never imports app code. Calling an action opens the app gateway on demand, sends JSON to its declared POST path, bounds execution/output, and always closes the lease. Managed runtimes still require the existing plan approval; MCP never grants approval itself. Static apps receive shared state and launcher controls; richer semantics require declared backend tools. Installed does not imply every conceivable action is available.

Errors use MCP error results for unavailable apps, unsupported tools, stale state, approval required and backend failures. Authentication and malformed transport requests use HTTP/JSON-RPC errors. A remote harness must be able to reach the engine; no automatic tunnel, exposure or firewall changes are part of this feature.

## Verification

Prove bearer enforcement, origin and host checks, key rotation/revocation, bounds, malformed JSON handling, lazy discovery, real app state round trips, declared backend action invocation and lease cleanup. Browser checks cover first configuration, connection, expand/collapse, disconnect, reload persistence and phone layout, with judged screenshots. Packaging tests prove panel assets ship in the wheel.
