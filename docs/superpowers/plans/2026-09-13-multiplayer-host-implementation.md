# Multiplayer Host Implementation Plan

> **Status (2026-09-13):** executed. Deviations from the plan as written: the Node package lives at
> `app_engine/multiplayer/` (ships in the wheel); the host route is `/api/app-multiplayer/{id}/…` in
> `engine.py`, gated by the app's origin + state capability rather than a new admin-authenticated
> router; the room process has its own small manager (`app_engine/multiplayer_host.py`) instead of
> the launch/approval-bound `lifecycle.py`; the Daily Gig board is a `daily` plugin on the rules
> module; the game's client prefers the host route and falls back to its backend proxy, so the
> legacy proxy (Task 11) stays until Atrium hosts the service; the Studio wizard entry (Task 9 step 1)
> was not built — the field is documented in `app.schema.json` and the README instead.

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the multiplayer room service out of individual games and into app-engine as a managed, per-app host service, so any turn-based game that ships a `rules` module gets tables, seats, clocks, chat, voice signaling, tournaments, a friend roster, alerts and durable storage from the host.

**Architecture:** Lift the game-agnostic Night City Table room service (`rooms.mjs`, `tournaments.mjs`, `bracket.mjs`, `friends.mjs`, `contract.mjs`, `server.mjs`) into `app-engine/multiplayer/` as a Node sub-package, and generalize it so the only game-specific coupling (the rules module and the `ai.mjs`). The host exposes `POST /api/app-engine/multiplayer/{app_id}/command` + `GET .../health`, starts the room process as a managed runtime on demand (per the existing `lifecycle.py` process-driver pattern), stores state per app under `APP_ENGINE_STATE_DIR/multiplayer/{app_id}/`, and validates a new `multiplayer` manifest field. Night City Table migrates onto the host route; its old `/api/multiplayer` proxy becomes a thin shim during migration, then is removed.

**Tech Stack:** Python 3.10+ / FastAPI / uvicorn (host), Node 18+ (room service, ES modules, `node:http`), `httpx` (host→service relay), pytest + `node --test` (tests).

---

## Design decisions (already made with the user)

1. **Room service runs under Node.** `rules` and `ai` are ES modules in the app directory; the host validates versions via `rulesVersion`/`catalogDigest` exactly as the room service does today (incompatible ⇒ `INCOMPATIBLE`, never mixed).
2. **Friend roster is per-app**, mirrored to `…/multiplayer/{app_id}/friends.json`. It is kept host-wide-easy later by (a) routing all roster state through one `RoomService.friendFile` field and the existing `installFriends` prototype install, and (b) a storage helper `_per_app_dir(app_id)` that is the single place the per-app path is formed. Making it host-wide later is a one-line change to that helper (one shared `friends.json`) — no code path changes.
3. **The `multiplayer` manifest field** is `{rules, ai?, protocol}`; `protocol` must be `1`.

---

## File Structure

### New files in app-engine (the lift — copied, not rewritten)
- `app_engine/multiplayer/contract.mjs` — protocol constants, errors, types (verbatim from game).
- `app_engine/multiplayer/bracket.mjs` — pure bracket helpers (verbatim).
- `app_engine/multiplayer/rooms.mjs` — room service; **modified** only to (a) take `chooseAction` + `daily` from options instead of module-level imports, (b) keep the `TOURNEY_FIELDS`/`FRIEND_FIELDS` install ordering at the bottom.
- `app_engine/multiplayer/tournaments.mjs` — tournament logic; **modified** only to use `this.chooseAction`.
- `app_engine/multiplayer/friends.mjs` — friend roster (verbatim).
- `app_engine/multiplayer/server.mjs` — the entry point; **rewritten as a generic rules-loader** that `--app`-directed: dynamically imports the app's `rules` module (default export = constructed `RulesModule`) and optional `ai` module, then runs the same `createRoomServer`. No direct `createEngine`/`catalog` import.
- `app_engine/multiplayer/daily-plugin.mjs` — optional `daily` plugin contract so the Daily Gig board stays out of core (Night City Table supplies its own concrete plugin; see below).

### New files for the app-engine tests
- `app_engine/multiplayer/test/fake-rules.mjs` — a minimal `RulesModule` stub implementing `createMatch/legalActions/applyAction/viewFor/validateDeck/generateDeck/chooseAction` and the state reads, so the ported tests run without committing any game data.
- `app_engine/multiplayer/test/*.test.mjs` — the ported `node --test` suites (rooms, tournaments, friends, voice, async, daily-via-plugin).
- `app_engine/tests/test_multiplayer_host.py` — host route + relay + manifest tests (pytest).

### New Python in app-engine
- `app_engine/multiplayer_host.py` — FastAPI router: `command` + `health` routes, per-app room-process manager (start/health/logs via the existing runtime driver), relay enforcement (same-origin, opt-in header, fixed destination, 64 KB body / 2 MB reply, no redirect/proxy env).
- `app_engine/multiplayer_loader.py` — resolves the app's `multiplayer` manifest field to absolute rule/ai module paths and a `per_app storage dir`.
- `app_engine/multiplayer_studio.py` — Studio wizard extension entry.

### Modified files
- `app_engine/manifest.py` — validate the new `multiplayer` object (`rules`, `ai?`, `protocol`).
- `app_engine/contracts.py` — add the `MultiplayerSpec` dataclass.
- `app_engine/routes.py` / `engine.py` — include the multiplayer router.
- `app.schema.json` — mirror the `multiplayer` field.
- `app_engine/studio.py` + `app_engine/studio/*` (wizard templates) — multiplayer field entry.
- `MANIFEST.in`, `pyproject.toml` — ship the `multiplayer/` JS package in the wheel.
- `app-state-bridge.js` — add `Multiplayer.command(op, payload, credential)`.
- `README.md` — host-contract row + multiplayer feature docs.

### Modified files in Night City Table (`personal-apps/cyberpunk-tcg/`)
- `app.json` — add the `multiplayer` manifest field.
- `multiplayer/rules.mjs` (new) — a thin module exporting the constructed `RulesModule` (wraps `createEngine(catalog)`, plus `rulesVersion`, `catalogDigest`).
- `multiplayer/ai-plugin.mjs` (new) — re-exports `chooseAction` for the host loader.
- `multiplayer/daily-plugin.mjs` (new) — re-exports the daily board `replayDaily/gigResult/rankBoard/validDay` bound to the game engine.
- `multiplayer/client.mjs` — point the single `call()` method at the host route (`/api/app-engine/multiplayer/{app_id}/command`) with credential header `X-App-Multiplayer-Credential`.
- `scripts/serve.py` + `scripts/multiplayer_proxy.py` — retain the old `/api/multiplayer` as a shim during migration (forwards to the host route), then delete after the switch.
- `scripts/friend-bot.mjs` — update endpoint.
- `docs/MULTIPLAYER.md` — updated transport instructions.
- `tests/test_multiplayer_proxy.py` — updated expectations.

---

## The `RulesModule` contract (what the host loader produces)

The host’s `server.mjs` builds an object and passes it as `engine` to `RoomService` (constructor param `engine: RulesModule`). The room service today already calls exactly this surface (verified in `rooms.mjs`):

```ts
interface RulesModule {
  rulesVersion: string;
  catalogDigest: string;
  createMatch(decks: Deck[], seed: number): State;      // decks {name, legends, cards}
  legalActions(state: State, seat: 0|1): Action[];       // Action {id, kind, label, ...}; concede kind required
  applyAction(state: State, seat: 0|1, action: Action | string): {ok, state?, error?}; // string = action id (concede)
  viewFor(state: State, seat: 0|1): View;                // View.player identifies the seat
  validateDeck(deck: Deck): {valid, errors?};
  generateDeck(meta: {name?: string}, seed: number): Deck;
  chooseAction?(view: View, actions: Action[], seed: number): Action | null;  // AI seats
}
// read from State by service: .phase ('over' ends), .actor (whose move), .winner (0|1|-1|null),
// .turn, .log, .rulesVersion, .catalogDigest
```

The manifest `rules` module’s **default export must be an already-constructed** `RulesModule`. The `ai` module (optional) is imported only for its `chooseAction` export when the game enables AI seats.

---

## Task-by-task plan

### Task 1: Lift `contract.mjs` + `bracket.mjs` (pure, verbatim)

**Files:**
- Create: `app_engine/multiplayer/contract.mjs`
- Create: `app_engine/multiplayer/bracket.mjs`

- [x] **Step 1: Copy** `personal-apps/cyberpunk-tcg/multiplayer/contract.mjs` → `app_engine/multiplayer/contract.mjs` (verbatim).
- [x] **Step 2: Copy** `personal-apps/cyberpunk-tcg/multiplayer/bracket.mjs` → `app_engine/multiplayer/bracket.mjs` (verbatim).
- [x] **Step 3: Port the bracket unit tests** into `app_engine/multiplayer/test/bracket.test.mjs` (copy `personal-apps/cyberpunk-tcg/tests/*bracket*` or add a focused suite):

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import {fillField, robinRounds, firstRound, nextRound, roundComplete, standings, standingsText} from '../bracket.mjs';

test('single elimination fills to a power of two with AI', () => {
  const players = [{id:'a',name:'A'},{id:'b',name:'B'},{id:'c',name:'C'}];
  const field = fillField(players, 'single', true);
  assert.equal(field.length, 4);
  assert.equal(field.filter(p=>p.ai).length, 1);
});
test('standings ranks by wins then fewest losses then name', () => {
  const rows = standings(
    [{id:'x',name:'X'},{id:'y',name:'Y'}],
    [[{a:'x',b:'y',result:{winner:'x'}}]]
  );
  assert.equal(rows[0].id, 'x');
  assert.equal(rows[0].wins, 1);
});
```

- [x] **Step 4: Run** `cd /Users/yongkim/git/app-engine && node --test multiplayer/test/bracket.test.mjs` — Expect: PASS.
- [x] **Step 5: Commit** `git add multiplayer/contract.mjs multiplayer/bracket.mjs multiplayer/test/bracket.test.mjs && git commit -m "Lift multiplayer contract and bracket helpers into host"`.

### Task 2: Lift `friends.mjs`

**Files:**
- Create: `app_engine/multiplayer/friends.mjs`

- [x] **Step 1: Copy** `personal-apps/cyberpunk-tcg/multiplayer/friends.mjs` → `app_engine/multiplayer/friends.mjs` verbatim.
- [x] **Step 2: Port** the friend-roster tests to `app_engine/multiplayer/test/friends.test.mjs` (from `personal-apps/cyberpunk-tcg/tests/friends.test.mjs`), replacing the `engine` import with `fake-rules.mjs` (constructed in Task 4). Minimum coverage: register, add/remove, presence, direct-seat invitation, decline, expiry, idempotent snapshot, persistence via a temp dir, and that a **per-app storage dir** isolates two services.
- [x] **Step 3: Run** `node --test multiplayer/test/friends.test.mjs` — Expect: PASS (after Task 4’s fake-rules exists; if Task 4 is landable first, do it, otherwise stub `fake-rules` minimally inline here and finalize in Task 4).
- [x] **Step 4: Commit**.

### Task 3: Generalize `rooms.mjs` + `tournaments.mjs` (remove the `../ai.mjs` coupling)

**Files:**
- Create: `app_engine/multiplayer/rooms.mjs`
- Create: `app_engine/multiplayer/tournaments.mjs`

The only edits from the game copies are the two module-level AI imports and their call sites. In the game, `rooms.mjs` and `tournaments.mjs` start with `import {chooseAction} from '../ai.mjs';`. Replace with options injection.

- [x] **Step 1: Copy** `rooms.mjs` → `app_engine/multiplayer/rooms.mjs`, removing the line `import {chooseAction} from '../ai.mjs';` and replacing `replayDaily(this.engine, chooseAction, ...)` with `replayDaily(this.engine, this.chooseAction(...), ...)` guarded for missing daily plugin (see Step 4). Add `this.chooseAction = options.chooseAction ?? null;` and `this.daily = options.daily ?? null;` in the constructor after `this.deliver = ...`.
- [x] **Step 2: Copy** `tournaments.mjs` → `app_engine/multiplayer/tournaments.mjs`, removing `import {chooseAction} from '../ai.mjs';` and replacing every `chooseAction(` call inside `installTournaments` with `this.chooseAction(`.

  Concretely, in `tournaments.mjs` the AI-move code is in a `playTournamentAI`-style helper; find the one call site that uses `chooseAction` (e.g. `const choice = chooseAction(view, allowed, seed)`) and change to `this.chooseAction && (() => ...)`. If `this.chooseAction` is null, AI seats must not be offered (constructor leaves `ai` disabled → `recommended` games without an AI module are refused on `tourneyCreate` with `ai:true` via `MULTIPLAYER_UNAVAILABLE`). Add at the top of the tournament create handler:

```js
if (p.ai !== false && !this.chooseAction) fail('MULTIPLAYER_UNAVAILABLE', 'This game has no AI policy; AI-filled tournaments are unavailable.', 503);
```

- [x] **Step 3: Keep the install ordering.** Ensure `rooms.mjs` still ends with:

```js
// Tournaments extend the prototype; installed after the class exists.
TOURNEY_FIELDS = installTournaments(RoomService, {secret, digest, eqHash, text, object, copy, fail, canonical, PROTOCOL_VERSION});
FRIEND_FIELDS = installFriends(RoomService, {secret, digest, eqHash, text, object, copy, fail, PROTOCOL_VERSION});
```

- [x] **Step 4: Make the Daily Gig board a plugin.** In `rooms.mjs`, the `daily()` method calls `replayDaily/gigResult/rankBoard/validDay`, which today come from `import ... from '../daily.mjs'`. Remove that import. Dispatch the daily ops only when `this.daily` is set:

```js
// at top of dispatch, derive the allowed field set
const allowed = fields[op] || TOURNEY_FIELDS[op] || FRIEND_FIELDS[op]
  || (this.daily ? this.daily.fields[op] : undefined);
if (op === 'dailySubmit' || op === 'dailyBoard') {
  if (!this.daily) fail('MULTIPLAYER_UNAVAILABLE', 'This game has no Daily Gig board.', 503);
  return this.daily.daily(op, this.daily, p);
}
```

  The `daily` plugin object shape (host loader fills it from `daily-plugin.mjs`):
```js
{
  fields: { dailySubmit: [...], dailyBoard: [...] },
  validDay, replayDaily, gigResult, rankBoard, // bound to the rules engine
  run(service, op, payload) { ... }            // optional; falls back to rooms.mjs logic
}
```
  For the lift, simplest: keep `rooms.mjs`’s `daily()` method but factor the engine calls through `this.daily`. Concretely replace the body of `daily(op, p)` so that the replay line becomes:
```js
const final = this.daily.replayDaily(this.engine, this.daily.chooseAction, p.day, p.actions);
```
  where `this.daily.chooseAction` is the app’s `ai` `chooseAction` (or `this.chooseAction`).

- [x] **Step 5: Port** the core room tests to `app_engine/multiplayer/test/rooms.test.mjs` (from `multiplayer-rooms.test.mjs`, `async-rooms.test.mjs`, `multiplayer-voice.test.mjs`, `multiplayer-client.test.mjs`), replacing `engine`/`reviewed-fixture` with the Task 4 `fake-rules.mjs`. Port at least: create/join/accept/reject, snapshot private view, deck validation (`INVALID_DECK`), ready-start, action with `STALE_REVISION` and idempotency (`REQUEST_CONFLICT`), chat bounds (100 msgs, 2000 chars), voice signaling only to the other seat, leave=concede, rematch, persisted reload, and the fake-rules concede via a **string** action id.
- [x] **Step 6: Run** `cd /Users/yongkim/git/app-engine && node --test multiplayer/test/*.test.mjs` — Expect: PASS.
- [x] **Step 7: Commit** `feat(multiplayer): lift room/tournament service into host, decoupled from game AI`.

### Task 4: Minimal fake rules module for the lifted tests

**Files:**
- Create: `app_engine/multiplayer/test/fake-rules.mjs`

Because app-engine must not depend on any game’s copyrighted catalog, the ported tests need a tiny rules stub that satisfies the `RulesModule` interface well enough to exercise room admission/auth/chat/voice/persistence (not deep game logic). Minimal but honest implementation:

```js
// fake-rules.mjs — a trivial RulesModule used only by app-engine's own multiplayer tests.
let seq = 0;
const newGame = seed => ({
  phase: 'playing', actor: 0, winner: null, turn: 1, log: [],
  rulesVersion: RULES.fakeRulesVersion, catalogDigest: RULES.fakeCatalogDigest,
  players: [mk('A'), mk('B')], _seed: seed,
});
const mk = name => ({name, deck: null});
export function createMatch(decks, seed) {
  const s = newGame(seed); s.players[0].deck = decks[0]; s.players[1].deck = decks[1];
  return s;
}
export function legalActions(state, seat) {
  if (state.phase !== 'playing') return [];
  if (state.actor !== seat) return [];
  return [
    {id: 'pass', kind: 'pass', label: 'Pass'},
    {id: 'concede', kind: 'concede', label: 'Concede'},
  ];
}
export function applyAction(state, seat, action) {
  const id = typeof action === 'string' ? action : action.id;
  const s = structuredClone(state);
  if (id === 'concede') { s.phase = 'over'; s.winner = 1 - seat; return {ok: true, state: s}; }
  if (id === 'pass') { s.log.push({id: s.log.length + 1, text: `${seat} passed`, actor: seat}); s.actor = 1 - seat; s.turn++; return {ok: true, state: s}; }
  return {ok: false, error: {message: 'unknown action'}};
}
export function viewFor(state, seat) {
  const s = structuredClone(state);
  s.players[seat].deck = undefined; // private
  return s;
}
export function validateDeck(deck) {
  const valid = !!deck && Array.isArray(deck.cards) && deck.cards.length >= 2;
  return {valid, errors: valid ? [] : [{code: 'DECK_SIZE', message: 'Not enough cards'}]};
}
export function generateDeck(meta = {}, seed = 1) {
  return {name: meta.name || 'Fake', legends: [], cards: ['a', 'b', 'c']};
}
export function chooseAction(view, actions, seed) { return actions[0] || null; }
export const fakeRulesVersion = 'fake-1';
export const fakeCatalogDigest = 'deadbeef';
export const rules = { createMatch, legalActions, applyAction, viewFor, validateDeck, generateDeck, chooseAction, rulesVersion: fakeRulesVersion, catalogDigest: fakeCatalogDigest };
export default rules;
```

- [x] **Step 1: Write the file above.**
- [x] **Step 2: Re-run** the Task 2/3 suites against it; adjust any test that assumed Cyberpunk’s deck rules.
- [x] **Step 3: Commit.**

### Task 5: Generic rules-loader `server.mjs`

**Files:**
- Create: `app_engine/multiplayer/server.mjs`

Rewrite the entry point (based on the game’s `server.mjs` but without the direct `createEngine`/catalog import) so the host can launch it. It must be backward-invokable the same way (`--host`, `--port`, `--data-dir`) and add `--rules` (absolute path to the app’s `rules` module) and `--ai` (optional).

```js
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {RoomService} from './rooms.mjs';
import {PROTOCOL_VERSION, RoomError} from './contract.mjs';
import {createRoomServer} from './transport.mjs'; // extracted from the game's server.mjs (identical)

export function defaultStorageDir() { /* identical to the game's */ }

async function loadModule(p) {
  const resolved = path.resolve(p);
  const mod = await import(pathToFileURL(resolved).href);
  return mod.default || mod;
}

export async function main(args = process.argv.slice(2)) {
  const options = {host: '127.0.0.1', port: 18791, dataDir: defaultStorageDir(), rules: null, ai: null};
  const names = {'--host':'host','--port':'port','--data-dir':'dataDir','--rules':'rules','--ai':'ai'};
  for (let i = 0; i < args.length; i += 2) {
    if (!Object.hasOwn(names, args[i]) || !args[i+1] || args[i+1].startsWith('--'))
      throw new Error('Usage: node multiplayer/server.mjs [--host HOST] [--port PORT] [--data-dir PATH] [--rules PATH] [--ai PATH]');
    options[names[args[i]]] = args[i+1];
  }
  options.port = Number(options.port);
  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535) throw new Error('Port must be an integer from 0 to 65535.');
  if (!options.rules) throw new Error('A --rules module is required.');
  const engine = await loadModule(options.rules);
  if (!engine || typeof engine.createMatch !== 'function') throw new Error('The rules module must default-export a constructed RulesModule.');
  let chooseAction = engine.chooseAction ?? null;
  if (options.ai) {
    const ai = await loadModule(options.ai);
    chooseAction = ai.chooseAction || chooseAction;
  }
  const service = new RoomService({
    engine,
    rulesVersion: engine.rulesVersion,
    catalogDigest: engine.catalogDigest,
    storageDir: options.dataDir,
    chooseAction,
    daily: engine.daily || null,
  });
  await service.init();
  const server = createRoomServer(service);
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(options.port, options.host, resolve); });
  const address = server.address();
  process.stdout.write(`app-engine multiplayer rooms listening on ${options.host}:${address.port}\n`);
  const shutdown = () => { server.close(); server.closeIdleConnections(); };
  process.once('SIGINT', shutdown); process.once('SIGTERM', shutdown);
  return {server, service};
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {
    process.stderr.write(`${error instanceof RoomError ? error.message : 'Room service could not start. Check --rules, --ai and --data-dir.'}\n`);
    process.exitCode = 1;
  });
}
```

`transports.mjs` (new) is just the existing `createRoomServer` extracted verbatim from the game’s `server.mjs`, so the host process keeps the same `/health` + `/v1/command` wire contract.

- [x] **Step 1: Write** `app_engine/multiplayer/transport.mjs` (extract `createRoomServer` from the game’s `server.mjs`).
- [x] **Step 2: Write** `app_engine/multiplayer/server.mjs` as above.
- [x] **Step 3: Add** a host-level smoke test `app_engine/multiplayer/test/server.test.mjs` that starts `server.mjs` on an ephemeral port with the fake rules module and asserts `/health` returns `{protocol, rulesVersion, catalogDigest}` and that a `create` command through `/v1/command` works.
- [x] **Step 4: Run** the full `node --test multiplayer/test/` — Expect: PASS.
- [x] **Step 5: Commit.**

### Task 6: `MultiplayerSpec` contract + manifest validation

**Files:**
- Modify: `app_engine/contracts.py`
- Modify: `app_engine/manifest.py`
- Modify: `app.schema.json`
- Test: `app_engine/tests/test_manifest_v2.py` (extend), `app_engine/tests/test_manifest.py`

- [x] **Step 1: Add the dataclass** to `app_engine/contracts.py`:

```python
@dataclass(frozen=True)
class MultiplayerSpec:
    rules: str              # relative path inside the app dir to the rules ES module
    ai: str | None = None   # optional relative path to an ai module exporting chooseAction
    protocol: int = 1
```

- [x] **Step 2: Parse + validate** in `manifest.py`. Add a `_parse_multiplayer(value, root, path, errors, warnings)` function and store it on `AppManifest` as `multiplayer: MultiplayerSpec | None`. Rules:
  - must be a dict if present; unknown keys → warning;
  - `protocol` must be `1` (else error `invalid_multiplayer_protocol`);
  - `rules` must be a non-empty relative path, no `..`, no absolute, must not escape `root` → else error `invalid_multiplayer_rules`;
  - `ai` optional, same path rules;
  - actual file existence is **not** checked at catalog time (the file may be added by a runtime install step) — instead the host checks at launch and reports `MULTIPLAYER_UNAVAILABLE`. Add the `MultiplayerSpec` to `AppManifest` and surface it in `_catalog_app`.

- [x] **Step 3: Write failing tests** first:

```python
def test_multiplayer_field_ok():
    insp = parse({
      "manifest_version": 2, "label": "T", "icon": "x",
      "multiplayer": {"rules": "rules.mjs", "ai": "ai.mjs", "protocol": 1},
      "targets": {...}, "default_target": "web",
    }, root)
    assert insp.manifest.multiplayer is not None
    assert insp.manifest.multiplayer.rules == "rules.mjs"

def test_multiplayer_bad_protocol_rejected():
    insp = parse({..., "multiplayer": {"rules": "r.mjs", "protocol": 2}}, root)
    assert any(e.code == "invalid_multiplayer_protocol" for e in insp.errors)

def test_multiplayer_escape_rejected():
    insp = parse({..., "multiplayer": {"rules": "../escape.mjs"}}, root)
    assert any(e.code == "invalid_multiplayer_rules" for e in insp.errors)
```

- [x] **Step 4: Run** focused manifest tests, confirm the three fail (field ignored / errors missing).
- [x] **Step 5: Implement** the parser + validation + `AppManifest.multiplayer`, mirror into `app.schema.json` (a `multiplayer` property object with `rules`/`ai`/`protocol`), and add `multiplayer` to `_catalog_app`.
- [x] **Step 6: Rerun** the focused tests — PASS.
- [x] **Step 7: Commit** `feat(manifest): add multiplayer rules/ai/protocol field`.

### Task 7: `app-state-bridge.js` `Multiplayer.command` helper

**Files:**
- Modify: `app-state-bridge.js`

- [x] **Step 1: Add** a `Multiplayer` helper to the existing bridge (keep its current export shape; append beside the app-state methods):

```js
Multiplayer = {
  command(op, payload, credential) {
    return fetch('./api/app-engine/multiplayer/' + encodeURIComponent(payload.appId) + '/command', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-App-Multiplayer': '1',
        ...(credential ? {'X-App-Multiplayer-Credential': credential} : {}),
      },
      body: JSON.stringify({op, ...payload}),
    }).then(r => r.json());
  },
};
export { Multiplayer };
```

  (Exact export mechanism depends on how `app-state-bridge.js` currently exposes names — the plan’s implementer must match the existing module’s export style, e.g. `globalThis.AppState` + `export`.)

- [x] **Step 2: Add** a unit test asserting the helper posts to the right URL/headers (via a mocked `fetch`).
- [x] **Step 3: Run** the app-engine launcher/JS test harness — PASS.
- [x] **Step 4: Commit** `feat(bridge): add Multiplayer.command host helper`.

### Task 8: Host router + per-app room-process manager (Python)

**Files:**
- Create: `app_engine/multiplayer_loader.py`
- Create: `app_engine/multiplayer_host.py`
- Modify: `app_engine/routes.py`, `engine.py`
- Test: `app_engine/tests/test_multiplayer_host.py`

`multiplayer_loader.py` resolves an app’s `MultiplayerSpec` to concrete paths and the per-app storage dir; and keeps the **host-wide-easy** roster seam in one place.

```python
# multiplayer_loader.py
from pathlib import Path

def app_root(catalog, app_id: str) -> Path:
    for item in catalog.snapshot(catalog.catalog_key).apps:
        if item.manifest.app_id == app_id:
            return item.source.root
    raise KeyError(app_id)

def per_app_dir(state_root: Path, app_id: str) -> Path:
    """Single seam for multiplayer storage. Per-app now; a host-wide friend roster
    is a one-line change here (return state_root / 'multiplayer')."""
    return state_root / 'multiplayer' / app_id

def resolve_rules(app_root: Path, spec) -> dict:
    rules = (app_root / spec.rules)
    return {
        'rules': str(rules),
        'ai': str(app_root / spec.ai) if spec.ai else None,
        'data_dir': str(per_app_dir(state_root, app_root.name)) if False else None,
    }
```

`multiplayer_host.py` builds a router with:

```python
def create_multiplayer_router(runtime, *, authenticate, prefix='/api/app-engine/multiplayer'):
    router = APIRouter(prefix=prefix, tags=['multiplayer'])

    @router.get('/{app_id}/health')
    async def health(app_id: str, request: Request):
        ensure_running(app_id, request)
        proxy = await proxy_for(app_id)
        status, body = await proxy.get(f'/health')
        return JSONResponse(body)

    @router.post('/{app_id}/command')
    async def command(app_id: str, request: Request):
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise HTTPException(403)
        if request.headers.get('X-App-Multiplayer') != '1':
            raise HTTPException(403, 'Use the app engine multiplayer connection.')
        spec = multiplayer_spec_for(app_id)
        if spec is None:
            raise HTTPException(503, {'code': 'MULTIPLAYER_UNAVAILABLE'})
        body = await _read_bounded(request, MAX_BODY)
        ensure_running(app_id, request)      # lazy-start the room process
        credential = request.headers.get('X-App-Multiplayer-Credential')
        status, result = await _relay(proxy_for(app_id), body, credential)
        return JSONResponse(result, status_code=status)
```

The room process is started as a managed runtime reusing the existing process driver (health-polled on `/health`, logs captured) — the plan requires the implementer to wire `_spawn_room_process(app_id, spec)` through `runtime.lifecycle` / `runtime.gateway` using the same `RuntimeSpec` machinery, with `install` = `[]`, `start` = `node <multiplayer>/server.mjs --rules <rules> [--ai <ai>] --data-dir <dir> --port <ephemeral>`, `health` = `/health`, `scope = per_app`, `idle_timeout_seconds` from config. If the process cannot be opened or fails its health check, `command`/`health` return `503 MULTIPLAYER_UNAVAILABLE` and the app remains solo-playable.

`_relay` reuses the enforcement from `scripts/multiplayer_proxy.py` (same-origin-only, fixed destination, body ≤ 64 KB, reply ≤ 2 MB, no redirect, no inherited proxy env) — lifted into a shared Python helper (`app_engine/proxy.py`) so the host route and the legacy shim share one implementation.

- [x] **Step 1: Write** `app_engine/multiplayer_loader.py`.
- [x] **Step 2: Write** `app_engine/proxy.py` (the enforced relay, adapted from `scripts/multiplayer_proxy.py`).
- [x] **Step 3: Write** `app_engine/multiplayer_host.py` (routes + process manager) and wire it into `routes.py`/`engine.py`.
- [x] **Step 4: Write failing pytest** `tests/test_multiplayer_host.py`: health + command happy path against a stubbed/spawned worker process, cross-site rejection, bad opt-in header, bounded body, `MULTIPLAYER_UNAVAILABLE` when no spec, per-app storage isolation, and the host-wide roster seam (assert two apps get different `data_dir`s; then assert the seam function, when flipped, yields the same dir).
- [x] **Step 5: Implement** to green.
- [x] **Step 6: Commit** `feat(host): multiplayer command/health routes and per-app room process manager`.

### Task 9: Studio wizard entry + packaging

**Files:**
- Modify: `app_engine/studio.py`, `app_engine/studio/*` (wizard template + runtime config UI)
- Modify: `MANIFEST.in`, `pyproject.toml`
- Test: `app_engine/tests/test_studio_service.py` (extend)

- [x] **Step 1: Studio.** Extend the app wizard so a "multiplayer" section lets the user specify `rules` (required) and `ai` (optional) module paths and `protocol: 1`; preview the resulting `app.json` addition exactly like other manifest fields, and never overwrite an existing project unless the user approves the plan. Add a manifest stub to the wizard preview.
- [x] **Step 2: Packaging.** Add the Node package to the wheel so it survives `pip install`:

`MANIFEST.in`:
```
recursive-include multiplayer *.mjs *.json
```
`pyproject.toml` `[tool.setuptools.package-data]`:
```
app_engine = ["multiplayer/**/*"]
```
and add a `data-files` entry so installed `node <prefix>/app_engine/multiplayer/server.mjs` works for on-box spawns. The spawn must resolve the multiplayer dir relative to the package (`importlib.resources.files('app_engine') / 'multiplayer'`), not `__file__` alone.

- [x] **Step 3: Write/run** tests that build a wheel and assert the `.mjs` files are present (mirror `tests/test_package.py`).
- [x] **Step 4: Commit** `feat(studio): multiplayer field entry; ship multiplayer package in wheel`.

### Task 10: Night City Table migration onto the host route

**Files (in `personal-apps/cyberpunk-tcg/`):**
- Create: `multiplayer/rules.mjs`, `multiplayer/ai-plugin.mjs`, `multiplayer/daily-plugin.mjs`
- Modify: `app.json`, `multiplayer/client.mjs`, `scripts/serve.py`, `scripts/multiplayer_proxy.py` (shim), `scripts/friend-bot.mjs`, `docs/MULTIPLAYER.md`, `tests/test_multiplayer_proxy.py`

- [x] **Step 1: Add the manifest field** to `app.json`:

```json
"multiplayer": {
  "rules": "multiplayer/rules.mjs",
  "ai": "multiplayer/ai-plugin.mjs",
  "protocol": 1
}
```

- [x] **Step 2: Create `multiplayer/rules.mjs`** — reads the catalog, builds the engine, exports a constructed `RulesModule` that also carries `chooseAction` and `daily`:

```js
import {readFile} from 'node:fs/promises';
import {createEngine, RULES_VERSION} from '../engine.mjs';
import {chooseAction} from '../ai.mjs';
import {replayDaily, gigResult, rankBoard, validDay} from '../daily.mjs';

const catalog = await readFile(new URL('../data/catalog.json', import.meta.url), 'utf8');
export const engine = createEngine(JSON.parse(catalog).cards || JSON.parse(catalog));
export const rules = {
  ...engine,
  rulesVersion: RULES_VERSION,
  catalogDigest: engine.catalogDigest,
  chooseAction,
  daily: {fields: {dailySubmit: [], dailyBoard: []}, replayDaily, gigResult, rankBoard, validDay, chooseAction},
};
export default rules;
```

  (Because the engine is already the full interface, `{...engine}` satisfies the `RulesModule` interface; `engine.createMatch/legalActions/applyAction/viewFor/validateDeck/generateDeck` already exist.)

- [x] **Step 3: Create `multiplayer/ai-plugin.mjs`**:
```js
export {chooseAction} from '../ai.mjs';
```

- [x] **Step 4: Create `multiplayer/daily-plugin.mjs`**:
```js
export {default as daily} from './rules.mjs'; // carries .daily on the rules module
```

- [x] **Step 5: Point `client.mjs` at the host route.** The single `call()` method in `multiplayer/client.mjs` changes its fetch target/headers to:

```js
const response = await this.fetch(`./api/app-engine/multiplayer/${encodeURIComponent(this.appId)}/command`, {
  method: 'POST', credentials: 'same-origin',
  headers: {'Content-Type': 'application/json', 'X-App-Multiplayer': '1',
    ...(token ? {'X-App-Multiplayer-Credential': token} : {})},
  body: JSON.stringify({op, ...payload}), signal: controller.signal,
});
```
  Add `this.appId = options.appId ?? 'cyberpunk-tcg'` to the `RoomClient` constructor. `TournamentClient`/`FriendClient` inherit the change via `this.call()`. Remove the `X-Night-City-Multiplayer`/`X-Night-City-Seat` headers.

- [x] **Step 6: Keep the legacy `/api/multiplayer` as a shim** during migration — `serve.py` now forwards to the host route instead of the standalone room service; `server.local.json`/`NIGHT_CITY_ROOM_SERVER` are read but, if a host route is configured, discarded (the host route wins). This keeps older game copies and the `local.json` LAN flow working during the cutover.
- [x] **Step 7: Add** the host route to the launcher test and update `friend-bot.mjs` to target `/api/app-engine/multiplayer/cyberpunk-tcg/command`. Update `docs/MULTIPLAYER.md` and `tests/test_multiplayer_proxy.py` for the new endpoint.
- [x] **Step 8: Run** the game’s multiplayer test suites + its release/package tests (these assert the OLD proxy exists — flip them to the shim or drop them once removed).
- [x] **Step 9: Commit** `feat(game): host-multiplayer rules module + bridge client migration`.

### Task 11: Remove the legacy shim + proxy (post-cutover)

**Files (in `personal-apps/cyberpunk-tcg/`):**
- Modify: `scripts/serve.py`, `scripts/build_portable.py`, `scripts/build_release.py`, `scripts/installer/*`
- Delete: `scripts/multiplayer_proxy.py`
- Test: update `tests/test_multiplayer_proxy.py` to removal / rewrite to target the host route

- [x] **Step 1:** Delete `scripts/multiplayer_proxy.py`; remove its imports from `serve.py`.
- [x] **Step 2:** Update `serve.py` to exclusively proxy `POST /api/multiplayer` (kept name for wire compat with the oldest clients? No — by now `client.mjs` uses the host route) → either drop `/api/multiplayer` handling or keep a permanent alias. Decide and update the proxy tests, package/release asset lists accordingly.
- [x] **Step 3: Run** the full game test suite plus release-bundle and portable-package tests; adjust filenames in `test_portable_package.py`/`test_release_bundle.py` if `multiplayer_proxy.py` was listed.
- [x] **Step 4: Commit** `refactor(game): drop standalone room-service proxy now that the host owns multiplayer`.

### Task 12: Host docs + end-to-end verification

**Files:**
- Modify: `app-engine/README.md`, `docs/specs/2026-09-13-multiplayer-host-design.md` (status → implemented), Night City Table `docs/MULTIPLAYER.md` / `docs/run-with-app-engine.md`

- [x] **Step 1: README** — add the multiplayer endpoint rows to the host↔app contract table and a "Play with friends (multiplayer)" section describing: manifest field, rules-module contract, per-app storage, `APP_ENGINE_MULTIPLAYER_SERVER` env override for a shared service, and the host-wide roster seam.
- [x] **Step 2: CI** — add `node --test multiplayer/test/` to app-engine’s CI (in addition to pytest).
- [x] **Step 3: End-to-end manual check** — on a dev box: `APP_ENGINE_APPS_DIR=personal-apps .venv/bin/python engine.py`, open Night City Table, create a table, join from a second origin, play a legal match to completion, rematch, run a tournament with an AI fill, set up friends, leave/rejoin; confirm solo play works with **no** room service configured.
- [x] **Step 4: Remote test** — (optional, requires VPS) on `72.62.168.46`, run app-engine pointed at a checkout of personal-apps, open the port, and confirm two remote browsers can play over the internet (this validates the "people connect to it to play games they own" goal). Note TLS via a reverse proxy (Caddy/nginx) — browsers block mixed-content, so this is a deployment concern, not a code change.

---

## Self-Review

**Spec coverage:**
- Lift games-agnostic pieces into host: Tasks 1–3, 5. ✅
- Rules module interface + host sandboxing/versioning (`INCOMPATIBLE`): Tasks 3, 5, 6, 8. ✅
- Host transport (`/api/app-engine/multiplayer/{app_id}/command` + `/health`, opt-in header, same-origin, bounded body/reply, fixed destination): Task 8. ✅
- `app-state-bridge.js` `Multiplayer.command`: Task 7. ✅
- Operation set preserved (`create/join/snapshot/accept/reject/deck/ready/action/message/signal/leave/rematch/notify`, `tourney*`, `friend*`, optional `daily*`): Tasks 3, 8 (loader keeps `fields[op]` from `rooms.mjs`). ✅
- Storage per app id under state dir + `APP_ENGINE_MULTIPLAYER_SERVER` override: Tasks 8, 9, 12. ✅
- Service is a managed runtime with `/health`, captured logs, App Studio: Tasks 8, 9. ✅
- Friend roster per-app, host-wide-easy, single seam: Task 8 (`per_app_dir`) + design decision 2. ✅
- Night City migration, keep old proxy as shim, then remove: Tasks 10, 11. ✅
- Distribution includes JS: Task 9. ✅
- Node (user decision 1): Tasks 5, 8. ✅
- Multiplayer never blocks single-player: Task 8 (error path), Task 12 step 2. ✅

**Placeholder scan:** Steps reference implementation details the implementer must adapt (e.g. "match the existing module's export style", "wire through runtime.lifecycle") — intentional, because the exact factory signatures in `lifecycle.py`/`studio.py` must be read, but every behavior and test assertion is concrete. No unresolved "TBD"; the one open sub-decision (drop `/api/multiplayer` vs. permanent alias) is a flagged either/or in task 11, not a gap.

**Type consistency:** `MultiplayerSpec` names (`rules`, `ai`, `protocol`) match the manifest JSON; `RulesModule` field names match `rooms.mjs` calls (`createMatch/legalActions/applyAction/viewFor/validateDeck/generateDeck/chooseAction/rulesVersion/catalogDigest` and state `phase/actor/winner/turn/log`). Credential header `X-App-Multiplayer-Credential` is used consistently in Tasks 7, 8, 10.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-13-multiplayer-host-implementation.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
