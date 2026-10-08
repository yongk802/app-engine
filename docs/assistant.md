# External chat and app control

Open **Chat** at the right edge of the standalone launcher. Enter your chat
service's web page URL and select **Connect**. The page must permit embedding.
Sign in inside it as usual, or use **Open chat in new tab** if it blocks frames.
This is separate from per-app local tutors and multiplayer server chat.

Expand **Connection settings**, select **Generate key**, and copy the displayed
MCP endpoint and key into your chat harness's MCP server configuration:

```json
{
  "url": "http://127.0.0.1:8770/api/mcp",
  "headers": {"Authorization": "Bearer <your-generated-key>"}
}
```

The exact configuration format depends on the harness. It needs Streamable HTTP
MCP client support and a model that can call tools. A model endpoint alone is not
a chat page or an MCP client. App-engine does not run a model or agent loop for
this panel. The harness discovers tools and decides which calls to make.

The key controls the owner's installed apps, including writes. It is displayed
once; **Rotate key** replaces it and **Revoke key** disables it. Only its hash is
saved in `APP_ENGINE_STATE_DIR/.assistant/settings.json`. App-engine never sends
the key to the iframe, through its URL or through browser messages. Configure it
in the harness explicitly. Revocation blocks new requests; it cannot undo an
app action already dispatched.

The harness must be able to reach this engine. On another machine,
`127.0.0.1` means that machine, not yours: use an explicitly configured secure
port forward or run the harness locally. The endpoint validates its loopback
Host and any Origin header; a proxy must preserve the local engine authority.
No tunnel, public listener or firewall rule is created automatically. This
first version is available in local-owner mode; public-origin/player mode does
not expose the new assistant or MCP endpoint. Atrium already has its own
external MCP adapter; this feature is in the standalone launcher.

## Memory and lifetime

The panel fetches settings only when opened and loads the external page only
when you select Connect. Collapse keeps the conversation alive; **Disconnect**
removes its iframe. App-open event polling stops while collapsed. No hidden
chat page loads when the engine starts.

Tool discovery reads the engine's catalog and small declarative files. It never
imports app code or starts app processes. Calls to a backend tool open only that
app, with a 60-second deadline and a 256 KB result limit, and release the gateway
lease afterward. The app's declared `idle_timeout_seconds` determines when its
managed process stops; set it to a positive value (for example 300) to reclaim
memory after idle use. Explicit autostart apps still follow their manifest.

## Available controls

- `apps_list`: installed apps and their declared tool names, including apps with
  no custom tools. It does not start them.
- `apps_open`: queue an open request to expanded launcher chat panels. At least
  one panel must have polled recently. Delivery is best effort and expires after
  60 seconds; a queued result is not confirmation the app opened. Multiple open
  owner panels can receive it. Process-plan approval still appears in the UI.
- `apps_read_state`: read the app's existing shared JSON save and its revision.
- `apps_write_state`: replace that save using the revision just read, refusing
  stale writes and payloads over 100 KB. Reload the app to see the saved change.
  Backend-specific databases are not this shared state. Prefer custom tools
  where the app provides them.

Apps do not gain arbitrary semantic controls merely by being installed.
Backend apps can declare their own actions; static apps get the shared controls
above. For example, a notes app can expose a typed `add_note` action that writes
its own database. No general shell or arbitrary URL request tool is exposed.

## Declaring app actions

Place `app-tools.json` beside `app.json`:

```json
{
  "schema_version": 1,
  "tools": [{
    "name": "add_note",
    "description": "Add a note to this app's notebook.",
    "inputSchema": {
      "type": "object",
      "properties": {"text": {"type": "string", "maxLength": 10000}},
      "required": ["text"],
      "additionalProperties": false
    },
    "path": "/api/notes"
  }]
}
```

For app ID `notes`, MCP offers `app__notes__add_note`. A call sends its arguments
as a JSON POST to `/api/notes` on the app's default web target, which must have
an `entry_point` or managed process backend. The endpoint returns JSON. Paths
allow slash-separated letters, numbers, underscores and hyphens; absolute URLs,
query strings, fragments, encoded paths and traversal are refused. Normal engine
loopback restrictions apply to the app's backend. A managed process plan must
be approved through the launcher before a tool can start it; the tool cannot
grant that approval. Local approvals are forgotten when the engine restarts.

The declaration supports up to 32 tools and 256 KB per file. Each input is a
closed object schema. Supported schema keys: `type`, `properties`, `required`,
`additionalProperties` (false), `items`, `enum`, `description`, `minimum`,
`maximum`, `minLength`, `maxLength`, `minItems`, `maxItems`. Supported types:
object, array, string, integer, number, boolean and null. No `$ref` or schema
composition. Names and schemas are checked before offering tools; invalid
files are skipped. Discovery rereads declarations when tools are listed or
called. Installing apps through the store refreshes the catalog; externally
added directories require the engine catalog to be refreshed or restarted.

Tool errors report unavailable apps, stale state, required approval, malformed
arguments, backend failure or timeout. After an uncertain write failure, inspect
the app before retrying: disconnect or timeout does not undo an executed action.

The MCP adapter implements initialize, ping, tools/list (100 per page) and
tools/call with protocol versions 2025-11-25, 2025-06-18 and 2025-03-26. It is
stateless; GET/DELETE return 405 and accepted notifications return 202. The
transport follows [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
