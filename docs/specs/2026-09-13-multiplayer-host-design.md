# Multiplayer Host Design

**Date:** 2026-09-13  
**Status:** Implemented 2026-09-13 (Node service in `app_engine/multiplayer/`, host route `/api/app-multiplayer/{id}`, manifest field `multiplayer`; Night City Table migrated). Decisions: Node for the service; friend roster per app with the storage path as the single seam for a host-wide roster.  
**Scope:** app-engine owns the multiplayer room service; games plug in a rules module

## Goal

Move the multiplayer server out of individual games and into app-engine, the
process that already hosts every app. Tables, seats, clocks, chat, voice
signaling, tournaments, the friend roster and durable storage are the same for
every turn-based game; only the rules differ. A game should ship its engine and
its UI, declare that it is multiplayer, and get everything else from the host.

The reference implementation is Night City Table's room service in
`personal-apps/cyberpunk-tcg/multiplayer/` (as of commit `d664d39`, which added
the friend roster). Its test suite is the specification: port the tests first.

## Product principles

- Multiplayer never blocks single-player. A game must open and play solo with
  no room service configured, exactly as today.
- Two installations play together when they point at the same room service.
  For app-engine that is the app-engine that started the app, or a shared one
  on a LAN/VPN; Atrium keeps pointing at the same service kind.
- The service never trusts a client: it validates every move against the
  game's own `legalActions`, sends each seat a private view, stores only
  credential hashes, and treats every mutation as idempotent by request ID.
- Rules modules run inside the service. The host must sandbox and version them
  the way it already versions managed runtimes: an incompatible rules or
  catalog version is refused with `INCOMPATIBLE`, never silently mixed.

## What is game-agnostic today

Everything in `multiplayer/rooms.mjs`, `tournaments.mjs`, `bracket.mjs`,
`friends.mjs`, `contract.mjs` and `server.mjs` is independent of Cyberpunk:

| Area | Behaviour that moves to the host unchanged |
|---|---|
| Rooms | create/join/accept/reject, two seats plus a pending guest, 24 h invitations consumed on accept, leave = concede during play, rematch |
| Commands | `requestId` idempotency with `REQUEST_CONFLICT` on reuse, `expectedRevision` on moves with `STALE_REVISION`, bounded bodies |
| Views | `viewFor(state, seat)` and `legalActions(state, seat)` are the only things a client ever sees; the full state, seeds and decks never leave the service |
| Clocks | per-move 24 h/72 h timers; a player whose clock ran out concedes the next time anyone touches the room |
| Presence | `lastSeen` heartbeat, 30 s connected window, reset to unknown on restart |
| Chat / voice | last 100 messages, WebRTC signaling relayed only to the other accepted seat, no recording |
| Alerts | per-seat webhook `{url, token}` posted with `X-App-Notify-Token` when the turn passes to an absent player (this is app-engine's own `app-notify` route in Atrium) |
| Tournaments | single elimination / round robin, AI fill, a room per match with seats pre-seated, round clocks, standings |
| Friends | persistent identity per browser, `ncf1:` friend codes, mutual add/remove, presence, roster invitations that seat a friend directly |
| Storage | `rooms.json`, `tournaments.json`, `friends.json`, `daily.json`; write-to-temp, fsync, rename; one process per directory; friends survive a rules/catalog change, rooms do not |

## What stays in the game

The service reaches the game through a small **rules module**. These are the
exact calls the current service makes; nothing else about the engine is used.

```ts
interface RulesModule {
  rulesVersion: string;          // e.g. "cyberpunk-2026-09.table-4"
  catalogDigest: string;         // content hash of the card data the rules read
  createMatch(decks: Deck[], seed: number): State;     // decks carry {name, legends, cards}
  legalActions(state: State, seat: 0|1): Action[];      // Action has {id, kind, label, ...}; a "concede" kind must exist
  applyAction(state: State, seat: 0|1, action: Action | string): {ok: boolean, state?: State, error?: {message}};
  viewFor(state: State, seat: 0|1): View;               // redacted; View.player identifies the seat
  validateDeck(deck: Deck, mode?: string): {valid: boolean, ...};
  generateDeck(meta: {name?: string}, seed: number): Deck;   // AI seats and "random legal deck"
  chooseAction?(view: View, actions: Action[], seed: number): Action | null;  // AI seats; omit to disable AI fill
  // read by the service from State:
  //   state.phase ('over' ends the match), state.actor (whose move), state.winner (0 | 1 | -1 tie | null),
  //   state.turn, state.log (for AI seeding), state.rulesVersion, state.catalogDigest
}
```

Optional, game-specific extras that the host should treat as plugins, not core:
the **Daily Gig board** (`daily.mjs`: `replayDaily`, `gigResult`, `rankBoard`,
`validDay`) replays a recorded action list against a seeded match and ranks
results per day. Generalise it as a "seeded challenge board" hook or leave it
in the game.

The game keeps its client: `multiplayer/client.mjs` (`RoomClient`,
`TournamentClient`, `FriendClient`), `perspective.mjs`, `voice.mjs`, and the
lobby UI. Only the transport endpoint changes.

## Host contract

### Manifest

```json
{
  "manifest_version": 2,
  "id": "cyberpunk-tcg",
  "multiplayer": {
    "rules": "engine.mjs",
    "ai": "ai.mjs",
    "protocol": 1
  }
}
```

`rules` (and optional `ai`) are ES modules inside the app directory exporting
the interface above (`createEngine(catalog)` style factories need a documented
entry; simplest is a module whose default export is an already-constructed
`RulesModule`). The host loads them in the room-service process, keyed by app
id; a game whose rules fail to load is still playable solo and reports
`MULTIPLAYER_UNAVAILABLE` from the transport.

### Transport

Today each game backend proxies `POST /api/multiplayer` to a room service's
`POST /v1/command` and forwards `X-Night-City-Seat` as `Authorization: Bearer`.
In the host this becomes one route the iframe can reach same-origin:

| Endpoint | Purpose |
|---|---|
| `POST /api/app-multiplayer/{app_id}/command` (implemented beside `/api/app-state`, gated by the app's origin and state capability) | `{op, ...payload}`; seat, tournament or friend credential in `X-App-Multiplayer-Credential`; JSON `{ok, result}` or `{ok:false, error:{code, message}}` |
| `GET /api/app-multiplayer/{app_id}/health` | `{protocol, rulesVersion, catalogDigest}` for compatibility checks |

Rules the current proxy enforces and the host must keep: same-origin only
(reject `Sec-Fetch-Site: cross-site`), an explicit opt-in header, a fixed
destination (payload fields like `server`/`url` are rejected), bounded body
(64 KB) and reply (2 MB), no redirects, no inherited proxy environment.
`app-state-bridge.js` should grow a `Multiplayer.command(op, payload,
credential)` helper so games stop hand-rolling the fetch.

The operation set is the one in `contract.mjs` (`create`, `join`, `snapshot`,
`accept`, `reject`, `deck`, `ready`, `action`, `message`, `signal`, `leave`,
`rematch`, `notify`, the `tourney*` family, the `friend*` family, and the
optional `daily*` pair). Keep the names: every client and test already uses
them.

### Storage and configuration

- One storage directory per app id under `APP_ENGINE_STATE_DIR`, e.g.
  `…/multiplayer/cyberpunk-tcg/{rooms,tournaments,friends,daily}.json`.
  Friend rosters are per room service, not per app, so if the host wants one
  roster across games it lives one level up; the current code keys friends by
  service, which is the same thing when the service is app-engine.
- `APP_ENGINE_MULTIPLAYER_SERVER` (or the manifest) points an installation at a
  shared service instead of the local one; this replaces each game's
  `multiplayer/server.local.json` and `NIGHT_CITY_ROOM_SERVER`.
- The service is a managed runtime like any other: app-engine starts it, waits
  for `/health`, captures logs, and shows it in App Studio.

## Migration plan

1. **Lift, don't rewrite.** Copy `rooms.mjs`, `tournaments.mjs`, `bracket.mjs`,
   `friends.mjs`, `contract.mjs`, `server.mjs` into app-engine as a Node
   package (`app_engine/multiplayer/` or a sibling `multiplayer/` directory).
   Replace the direct `import {createEngine} from '../engine.mjs'` and
   `../ai.mjs` with the rules-module loader. Port
   `tests/multiplayer-rooms.test.mjs`, `async-rooms.test.mjs`,
   `tournaments.test.mjs`, `friends.test.mjs`, `tournament.test.mjs` with a
   fixture rules module; they must pass unchanged in behaviour.
2. **Add the host route** and the managed-runtime wiring; keep the game's
   `/api/multiplayer` proxy as a thin shim that forwards to the host route so
   nothing breaks mid-migration.
3. **Manifest field + Studio wizard** entry, `app-state-bridge.js` helper,
   README host-contract table row.
4. **Switch Night City Table** to the manifest field, delete
   `scripts/multiplayer_proxy.py` and `multiplayer/server.mjs` from the game,
   and point `scripts/friend-bot.mjs` at the host route (it is a second human
   seat driven from the terminal; useful for every game's smoke tests).
5. **Atrium** consumes the same package; its Friends panel already opens the
   game table via `night-city:open-multiplayer` and the app-notify webhook, so
   the only change is where the service runs.

## Open questions

- Node or Python for the service? The rules modules are JavaScript (they are
  the same engines the browser runs), which argues for Node: the host spawns
  it as a managed runtime. A Python port would need every game to ship a
  Python engine too.
- Should the friend roster be host-wide (one identity across games) or
  per app? Host-wide matches "friend each other"; the roster code is already
  game-agnostic, only the storage path decides.
- Voice relay (TURN) and public-internet exposure are still out of scope;
  a trusted LAN/VPN or HTTPS reverse proxy remains the deployment story.
