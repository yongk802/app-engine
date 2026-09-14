# Public-Origin Mode Design

**Date:** 2026-09-13  
**Status:** Phases 1–2 implemented 2026-09-13 (`app_engine/public_origin.py`: `APP_ENGINE_PUBLIC_ORIGIN`, `APP_ENGINE_TRUST_PROXY`, `APP_ENGINE_ADMIN_SECRET`, owner sign-in at `/admin`, gate middleware, bind guard); phases 3–4 proposed  
**Scope:** Run one app-engine on a VPS so invited people can open the games it hosts and play each other; the owner keeps the single-user product they have today.  
**Depends on:** [Multiplayer Host Design](2026-09-13-multiplayer-host-design.md) (implemented).

## Goal

`app-engine` on `72.62.168.46` (or any server) serves Night City Table to a
handful of friends over the internet. They open one address, see the games
they are allowed to play, and meet at the same tables; their saves and rosters
are their own. The owner installs games, approves runtimes, manages players
and never exposes Local AI, App Studio or the machine to visitors.

## What stands in the way today (from the code)

| Fact | Where | Consequence for a public server |
|---|---|---|
| Every `/api/app-engine/*` route authenticates as a fixed admin (`_runtime_authenticate` returns `HostSubject("local", "admin")`); the launcher is served to any request from a loopback host with the admin capability embedded in its HTML | `engine.py:77,103-109,702-707`, `launcher.html:138` | binding to a public interface makes every visitor the administrator |
| App origins are `<id>.localhost[:port]`; `_app_host_id` rejects anything else; `/api/apps` builds app URLs from the request | `engine.py:224-233,665-670` | `*.localhost` resolves only on the same machine; a remote browser cannot open an app |
| App-state is one file per app, and the per-app capability is minted once per process and handed to whoever loads the launcher | `engine.py:214-247` | two visitors would share one save and one capability |
| Managed-app sessions belong to the subject that opened them; plan approval is a launcher-only, per-fingerprint act | `routes.py:95-221` | visitors cannot approve runtimes (correct) but also cannot open an app the owner has not warmed |
| Multiplayer route is gated by app origin + capability; the room service is per app and already keeps players apart by seat credentials | `engine.py` multiplayer section, `multiplayer_host.py` | this part is ready: it only needs the origin check to accept the public authority |
| Security headers allow framing from anywhere (`frame-ancestors *` on apps) and the engine trusts no proxy headers | `engine.py:549-552` | needs a TLS terminator in front and explicit proxy trust |

Nothing in the games needs to change: Night City's client already targets the
host route on the app's own origin and carries its credentials in headers.

## Product principles

- **Off by default, loopback by default.** Public-origin mode is an explicit
  configuration; without it the engine keeps binding `127.0.0.1` with today's
  single-user semantics. Setting `APP_ENGINE_HOST` to a non-loopback address
  without public-origin mode and an admin secret is refused at startup.
- **Two kinds of people.** The *owner* (admin) and *players*. No self-service
  sign-up: a player exists because the owner minted an invitation. Anonymous
  visitors get nothing but a sign-in page.
- **Apps are the only thing players touch.** The launcher for a player lists
  allowed apps and nothing else. App Studio, Local AI, runtime approval,
  configuration, logs and the app catalog's rejects stay owner-only, and the
  Local AI surface stays loopback-only even for the owner.
- **Per-player everything the app persists.** Saves (`/api/app-state`), app
  capabilities and multiplayer credentials are scoped to the player. Tables are
  shared on purpose: that is what a room service is for.
- **One TLS front door.** app-engine never terminates TLS itself. A reverse
  proxy (Caddy is the reference) owns the certificate for the base domain and
  the app wildcard and forwards to loopback.

## Design

### 1. Origins

New configuration:

| Var | Example | Meaning |
|---|---|---|
| `APP_ENGINE_PUBLIC_ORIGIN` | `https://play.example.com` | the launcher's public origin; apps live at `https://<id>.play.example.com` |
| `APP_ENGINE_TRUST_PROXY` | `1` | honour `X-Forwarded-Proto` / `X-Forwarded-Host` from the loopback proxy only |

- `_app_host_id(request)` accepts `<id>.<public host>` when configured, in
  addition to `<id>.localhost` (the owner may still use the engine locally).
- `_is_launcher_host(request)` accepts the public host.
- `/api/apps` and the gateway's `entry_url` build app URLs from the configured
  origin, not from `request.url`. Ports disappear behind the proxy.
- Cookie domain for player sessions is the public host with a leading dot so
  the app subdomains carry it (see §2); `Secure`, `HttpOnly`, `SameSite=Lax`.
- `frame-ancestors` for app responses tightens to the public origin (the
  launcher) instead of `*`; the launcher stays `frame-ancestors 'none'`.
- The proxy must forward the `Host` header unchanged and set
  `X-Forwarded-Proto`. Reference `Caddyfile`:

```caddyfile
play.example.com, *.play.example.com {
    tls you@example.com          # ACME DNS challenge for the wildcard (Caddy DNS plugin)
    reverse_proxy 127.0.0.1:8770 {
        header_up X-Forwarded-Proto {scheme}
    }
}
```

DNS: `A play.example.com` and `A *.play.example.com` to the VPS. Nothing else
listens publicly; app-engine keeps `APP_ENGINE_HOST=127.0.0.1`.

### 2. Identity

A small `players.json` under `APP_ENGINE_STATE_DIR` (same atomic write
discipline as the room service; only hashes stored):

```json
{"version": 1,
 "admin": {"secret_hash": "…", "created": "…"},
 "players": [{"id": "p_7f3…", "name": "Rook", "invite_hash": "…", "invite_expires": "…",
              "sessions": [{"hash": "…", "created": "…", "last_seen": "…"}],
              "apps": ["cyberpunk-tcg"], "disabled": false}]}
```

- **Owner.** `APP_ENGINE_ADMIN_SECRET` (or generated on first start in
  public-origin mode, printed once, stored hashed). The owner signs in at
  `/admin` with the secret and receives an admin session cookie. The launcher
  HTML is only rendered for an admin session; the embedded launcher capability
  and every `Depends(authenticate)` route resolve the subject from that
  cookie. `HostSubject("local","admin")` remains the subject on loopback
  without public-origin mode, so nothing changes for today's users.
- **Players.** The owner mints an invitation from the launcher's new
  **Players** panel (or `app-engine players invite --name Rook --apps
  cyberpunk-tcg`). The invitation is a one-time link
  `https://play.example.com/join/<code>`, valid 7 days. Opening it sets a
  player session cookie (random 32 bytes, hashed at rest, 90-day sliding
  expiry) and lands on the player launcher. `authenticate` yields
  `HostSubject(player_id, "player")`.
- **Revocation.** Disable or delete a player; both invalidate sessions on the
  next request. Regenerating the admin secret invalidates the owner's
  sessions.
- **No passwords, no email.** The invitation link is the credential, as the
  table invitation and the friend code already are in the games. If a link
  leaks before use, the owner regenerates it.

### 3. Per-player scope

- **App capability.** `_app_capabilities` is keyed by `(subject_id, app_id)`
  and minted when that subject's launcher loads `/api/apps`. The fragment
  mechanism and `app-state-bridge.js` are unchanged; `_enforce_app_state_access`
  additionally resolves the subject from the cookie and checks that the
  capability belongs to it.
- **Saves.** `/api/app-state/{id}` reads and writes
  `app-state/players/<subject>/<id>.json`; the owner's own saves stay where
  they are (`app-state/<id>.json`) so an existing installation keeps its data.
- **Multiplayer.** No change to the route; the service stays one per app so
  players meet. The relay adds `X-App-Player: <subject_id>` to commands so a
  future roster version can bind friend identities to accounts instead of
  browsers (today's browser-held roster identity keeps working).
- **Managed apps.** A player opening a managed app gets their own `AppSession`
  (the session map is already keyed by subject) over the shared runtime. If
  the runtime's plan is not yet approved, the player sees "not available yet";
  the owner approves it once, from their launcher, exactly as today. The
  `autostart` runtime flag becomes the recommended setting for hosted games so
  the process is warm before the first visitor.

### 4. What players can reach

| Surface | Owner (loopback or public) | Player |
|---|---|---|
| `/` launcher | full | player launcher: allowed apps, no Studio/AI/settings |
| `/api/apps`, `/api/apps/categories` | all apps | allowed apps only |
| `/apps/<id>/…`, session proxy, assets | yes | allowed apps only |
| `/api/app-state/<id>` | own scope | own scope |
| `/api/app-multiplayer/<id>/…` | yes | allowed apps only |
| `/api/app-chat`, `/api/local-ai/*` | loopback only | never |
| App Studio, plan approval, launch logs, configuration | yes | never (404, not 403, so the surface is not enumerable) |
| `/api/apps/rejected`, `/api/engine` | yes | `/api/engine` only |

### 5. Abuse limits

- Per-session token bucket on `/api/app-multiplayer` and `/api/app-state`
  (default 20 requests/s burst 60), 429 with `Retry-After`.
- Body limits already exist (64 KB commands, 100 KB saves, 2 MB replies).
- Room-service capacity limits already exist (100 open rooms, 1000 players on
  a roster); expose them in the owner's Players panel.
- Player count cap (`APP_ENGINE_MAX_PLAYERS`, default 50) and a startup
  warning when the engine is public without a proxy in front (no
  `X-Forwarded-Proto` seen in the first request).
- An append-only `audit.log` under the state dir: sign-ins, invitations,
  approvals, player changes.

### 6. Content the operator serves

Night City Table's `data/` (card catalog and art) is publisher material the
game imports for personal local use and is deliberately not in Git. Hosting
the game for other people publishes it to them. This is the operator's call
and licence question, not the engine's; the design only notes that the
Players panel should show which apps serve imported data so the owner decides
knowingly. app-engine itself ships nothing but MIT code.

## Threats considered

- **Visitor becomes admin.** Admin routes require the admin cookie; the
  launcher capability is never rendered into a player's page; Local AI stays
  loopback-only regardless of role.
- **Player reaches another player's save or capability.** Scoped keys and
  files; capability checked against the cookie's subject.
- **Player reaches an app not granted to them.** Allow-list checked on the
  app's own origin routes, not only in the listing.
- **Cross-site requests from a hostile page.** `SameSite=Lax` cookies, the
  `Sec-Fetch-Site` check the multiplayer route already does, `frame-ancestors`
  limited to the launcher origin.
- **Leaked invitation link.** One-time, expiring, revocable; owner regenerates.
- **Room service exposure.** It listens on loopback on an ephemeral port and is
  reachable only through the gated relay; that is unchanged.
- **Proxy header spoofing.** `X-Forwarded-*` honoured only when
  `APP_ENGINE_TRUST_PROXY=1` and the peer is loopback.

## Phases

1. **Origins + proxy trust** — `APP_ENGINE_PUBLIC_ORIGIN`, `APP_ENGINE_TRUST_PROXY`,
   origin-aware `_app_host_id`/`_is_launcher_host`/URL building, tightened
   `frame-ancestors`, the Caddy recipe. Still single-user: lets the owner reach
   their own VPS remotely. Refuse non-loopback binding without the next phase.
2. **Owner sign-in** — `players.json` admin record, `/admin` sign-in, cookie
   subject in `authenticate`, launcher rendered only for admins. This is the
   gate that makes a public bind safe.
3. **Players** — invitations, player sessions, allow-lists, per-player
   capability and saves, player launcher, Players panel and CLI.
4. **Hardening** — rate limits, audit log, player cap, startup warnings,
   `X-App-Player` on the relay, a release-testing checklist entry for the
   public mode (two browsers on two networks play a full match).

Each phase ships behind the configuration flag and keeps the loopback product
byte-for-byte in behaviour; the existing pytest suites run in both modes.

## Open questions

- Should the public launcher be a separate, smaller page (`player.html`) or
  the same `launcher.html` with owner panels removed server-side? A separate
  page is simpler to reason about for the threat model.
- Player-bound friend rosters: bind on first `friendRegister` from a signed-in
  player, or keep browser identities and add "link to account" later?
- Do players need a display name at the engine level, or is the game's own
  name field enough? (The invitation carries a name; games can read
  `X-App-Player`-derived info later if wanted.)
