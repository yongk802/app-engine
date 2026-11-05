# App-engine security audit — 2026-11-04

App-engine reviewed across the same 13 layers as Atrium's vault
(`atrium/docs/vault-security-audit.md`) and the standalone Vault app
(`personal-apps/vault/SECURITY-AUDIT.md`). Every vault finding was checked here
and marked **fixed**, **not applicable** or **accepted**. Findings that only
app-engine has follow. A pen test with open-source tools closes the review.

## Threat model

App-engine runs in two modes:

- **Loopback (the default).** No sign-in. Being able to reach `127.0.0.1:8770`
  is the credential.
- **Public origin.** It runs behind a local TLS proxy. Every request needs an
  owner or player session. Apps run at `<id>.<host>`.

That changes which attackers matter, compared with Atrium's vault:

- **T1 — stolen disk or backup**: state files under `APP_ENGINE_STATE_DIR`.
- **T2 — hostile web page.** In loopback mode it is the main threat, because
  no cookie stands in its way. It can:
  - send simple cross-site requests;
  - frame app pages;
  - with **DNS rebinding**, point its own name at 127.0.0.1 and read
    responses as if they were same-origin.
- **T3 — another local user or process**: it can reach the port and read files
  that other users may read.
- **T4 — malicious app or store package**: its code runs in the app's origin.
  In public subdomain mode it is same-site with the launcher, so the browser
  sends it the shared session cookie.
- **T5 — script in the launcher's origin**: holds the owner capability
  (`x-app-engine-admin`).

## Findings carried over from the vault audits

| # | Vault finding | App-engine status | Why |
|---|---|---|---|
| 1 | `esc()` left quotes unescaped | Not applicable | The launcher builds its DOM with `createElement` and `textContent`. It has no `innerHTML` sinks and no inline handlers. |
| 2 | Method delete worked while locked | Not applicable | There is no vault. Owner mutations need both the session and the launcher capability (`_require_owner_action`). |
| 3 | No `Host` check (DNS rebinding) | **Fixed** | Unlike Atrium, loopback mode has no session, so this one applies in full. Before the fix, a rebound name could read `/api/local-ai/status`, `/api/local-ai/diagnostics` and `/state`, which hold hardware, OS, model and endpoint details, and could drive `/api/app-chat`. The new `LocalHostGate` answers 421 to any Host other than `127.0.0.1`, `::1`, `localhost`, `*.localhost` or a name in `APP_ENGINE_ALLOWED_HOSTS`. |
| 4 | Settings writable while locked | Not applicable | There is no lock. Local-AI settings need the launcher capability. |
| 5 | Audit readable while locked | Not applicable | `audit.log` is created `0600` and is never served. |
| 6 | Chunked-body parser | Not applicable | uvicorn parses the framing. Bodies are capped per route (state 100 KB, multiplayer `MULTIPLAYER_MAX_BODY`). |
| 7 | Unpinned dependencies | Accepted | `uv.lock` pins with hashes. `pip-audit` and `npm audit` report 0 known CVEs (2026-11-04). |
| 8 | Weak key generator | Not applicable | — |
| 9 | SQLite kept freed pages | Not applicable | The state is JSON files. |
| 10 | Racing first enrolment | Not applicable | `Accounts.save` writes atomically (temp file, `fsync`, `os.replace`, `0600`). |
| 11 | `frame-ancestors *` | **Fixed** | In loopback mode, app pages sent `frame-ancestors *`, so any web page could frame an app on `<id>.localhost` for clickjacking. Framing is now limited to `'self'` and launchers on loopback (`http(s)://127.0.0.1:*` and `localhost:*`; CSP cannot name an IPv6 literal), plus `APP_ENGINE_FRAME_ANCESTORS`. |
| 12 | Failed unlocks lock everyone out | Accepted | `SignInLimiter` counts per address. Reaching it already needs the public origin. |
| 13 | Python `bytes` copies of secrets | Accepted | Inherent to Python. |
| 14 | No CSP `script-src` | **Fixed** | The launcher carries the owner capability. It now sends a per-response nonce CSP: `script-src 'self' 'nonce-…'`, `script-src-attr 'none'`, `object-src 'none'`, `base-uri 'none'`, `form-action 'self'`. It also sends `Referrer-Policy: no-referrer` and `nosniff`. |
| 15 | Pins without hashes | Not applicable | See #7. |
| 16 | Per-launch proxy token | **Fixed earlier** | `2f74d05`: each managed launch gets `APP_PROXY_TOKEN`, so an app can refuse local programs that skip the engine. |

## Findings new to app-engine

| # | Finding | Status | Detail |
|---|---|---|---|
| A1 | Cross-site `text/plain` POST reached `/api/app-chat` | **Fixed** | `request.json()` parses any body, so a form post from any page drove the local model with no CORS preflight. The pen test reproduced this against the old build, which answered `data: {"type": "setup_required"}`. The route now requires `application/json`, refuses `Sec-Fetch-Site: cross-site` and rejects bodies that aren't objects. Before, a non-object body caused a 500. |
| A2 | Hosted app could post server chat as the player | **Fixed** | In subdomain mode the session cookie's domain covers every `<id>.<host>`. SameSite=Lax treats them as same-site, so an app's script could `POST /api/chat` with `text/plain` and the player's cookie. `/api/chat` POST now needs a JSON fetch. A cross-origin JSON fetch needs a CORS preflight, which the engine never grants. |
| A3 | State directory readable by other users | **Fixed** | `~/.config/app-engine/app-state` was `0755` with `0644` saves. Startup now creates it `0700` and strips the *other* bits from an existing tree. Group bits stay because the isolated Package Tracker broker traverses it by group (`6181223`). |
| A4 | `/api/local-ai/install-plan` answered in public mode | **Fixed** | It was the one local-AI route that didn't call `_deny_public_local_ai()`. |
| A5 | `/docs`, `/redoc`, `/openapi.json` published | **Fixed** | Nothing used them, and they handed out a full route map. |
| A6 | Re-invite leaves the player's old sessions and password live | Open | `regenerate_invite` mints a new code but doesn't revoke sessions. When a re-invite is meant to lock someone out, the owner should *disable* the player, which does clear sessions. Deferred since 2026-09-15. |
| A7 | Orphaned Node room service can double-write `rooms.json` | **Fixed** | An engine killed without stopping its Node room service left the service running, and the next engine started a second one on the same directory; each kept its own rooms in memory and overwrote the other's moves. The host now gives the service a stdin pipe and `APP_ENGINE_MULTIPLAYER_EXIT_WITH_PARENT=1`, so it exits when the engine's end closes. Every service also claims its data directory with `service.lock` (pid, token, 10 s heartbeat), refuses to start beside a live holder and takes over one that is dead or 30 s stale. Tests: `app_engine/multiplayer/test/lock.test.mjs`, `tests/test_multiplayer_host.py`. |
| A8 | App commands run as host subprocesses | Accepted (tracked) | `docs/superpowers/specs/2026-10-22-production-security-design.md` slice 3. `APP_ENGINE_PROCESS_ISOLATION=audit`/`enforce` and the isolated runner are the path. |

Regression tests: `tests/test_host_gate.py` covers A1–A5, #3, #11 and #14.
`tests/test_public_origin.py::test_loopback_mode_is_unchanged` asserts the new
framing policy. Each test was checked to fail on `origin/main`.

## The 13 layers

### 1. Threat model and trust boundaries
**Boundaries:**
- the browser and the launcher, `127.0.0.1` or the public origin;
- the launcher and each app, on a separate origin `<id>.localhost` or
  `<id>.<host>`;
- the engine and managed app processes, with a per-launch proxy token;
- the engine and stores, over HTTPS with optional mTLS;
- the engine and Atrium, over the mTLS `/api/mcp` gateway.

`LoopbackListenerGate` refuses any non-loopback listener, including a direct
`uvicorn` import. **New:** `LocalHostGate` refuses any unknown Host (#3).

### 2. Cryptographic primitives
- **Tokens:** `secrets.token_urlsafe(32)` for sessions, capabilities and
  invites.
- **Comparisons:** `hmac.compare_digest` and `secrets.compare_digest`
  throughout.
- **TLS:** handled by the proxy, with `OutboundTlsPolicy` for client
  certificates on exact origins only.

Nothing found.

### 3. Credential derivation
- **Passwords and admin secret:** PBKDF2-HMAC-SHA256, 200,000 rounds, per-user
  salt. The admin secret was unsalted SHA-256 at the 2026-09-15 review and is
  fixed.
- **Session and invite tokens:** stored only as SHA-256 digests, which suits
  256-bit random tokens.
- **Password minimum:** 8 characters.

### 4. Key and secret file handling
- `players.json`, `audit.log` and `chat` are written `0600` through `os.open`.
- `APP_ENGINE_MCP_PROXY_SECRET` must be at least 32 characters.
- The admin secret is printed once to the journal and kept only as a hash.
- `gitleaks` scanned 161 commits and found no secrets.

### 5. Storage at rest
State is plain JSON: app saves, `stores.json`, `players.json` and the config.
**A3 fixed:** the tree is closed to other users. Plaintext saves are accepted:
they belong to apps, not to secrets.

### 6. Sessions and memory
**Owner and player sessions:**
- 90-day sliding expiry;
- at most 20 owner sessions and 10 per player;
- revoked on sign-out;
- cleared when a player is disabled.

**Capabilities:**
- App-state capabilities live only for the engine process. They reach the app
  only in the URL fragment and never in a query or a log.

**Open:** a re-invite keeps old sessions (A6).

### 7. Authentication, authorisation and rate limits
**Loopback mode:**
- address plus Host (#3);
- owner mutations need the launcher capability;
- app state needs the app origin plus its capability.

**Public mode:**
- `OwnerGate` on every path except sign-in, join and the bearer API;
- players see only the apps they were granted, and anything else answers 404;
- `APP_ENGINE_PUBLIC_APPS` limits what is hosted at all.

**Rate limits:**
- sign-in, per address;
- `/api/players/`, `/api/app-multiplayer/`, `/api/app-state/` and `/api/chat`,
  per subject or address (`APP_ENGINE_RATE_LIMIT`).

### 8. HTTP surface: CSRF, CORS, rebinding and binding
- **CSRF:**
  - JSON is required on cookie- or address-authenticated POSTs (A1, A2);
  - owner mutations need a custom header;
  - multiplayer refuses `Sec-Fetch-Site: cross-site`.
- **CORS:** only `/api/players/` (bearer, no credentials) sends `*`.
- **Rebinding:** fixed (#3).
- **Binding:** loopback only. A public origin outside `.localhost` must be HTTPS.

### 9. Input validation and parsing
- **App ids:** `^[a-z0-9][a-z0-9-]*$`.
- **Paths:** static paths resolve under the app root, with no `..` and no
  absolute paths.
- **Store ZIPs:**
  - no symlinks, encrypted or duplicate entries;
  - no entries whose names differ between the two ZIP name fields, and no
    `.git` or `.venv` folders;
  - size and file-count caps;
  - each entry checked against its declared size;
  - extracted only into a fresh staging folder;
  - SHA-256 checked against the listing.

**Pen test:** Wapiti's injection, traversal, SSRF, XXE, CRLF and exec modules
found nothing.

### 10. Browser: XSS, CSP, framing
- The launcher has a nonce CSP (#14) and `frame-ancestors 'none'`.
- Apps get `nosniff` and framing limited to the launcher (#11).
- Apps run in iframes with their manifest `sandbox`, on their own origin.
- **Residual:** the store and studio pages have framing protection but no
  `script-src` policy.

### 11. Logging, audit and error leakage
- `Audit` records events without secrets, tokens or passwords.
- The Package Tracker mail resource paths are no longer logged (`2a60185`).
- **Residual:** `/api/local-ai/status` shows the Ollama binary path. Only local
  callers can reach it now (#3), so this is accepted.

### 12. Dependencies, packaging and deployment
- `uv.lock` pins with hashes.
- `pip-audit` and `npm audit` find 0 CVEs.
- bandit reports 2 medium findings, both accepted: the `exec` of the bundled
  `engine.py` in `__main__`, and `urlopen` on the configured Ollama endpoint,
  which `ConfigStore` limits to loopback.
- semgrep `p/python`, `p/javascript` and `p/secrets` report 0 findings.
- `tests/test_package.py::test_wheel_contains_importable_modules_and_standalone_assets`
  already fails on `origin/main`. That is a build-environment problem, not a
  security one.

### 13. Recovery, availability and operations
- Atomic JSON writes.
- An admin secret rotation signs every session out.
- Invites expire after 7 days.
- Player list and mail caps.
- Room services exit with their engine and lock their data directory (A7).
- **Tracked:** process isolation rollout (A8).

## Pen test (2026-11-04)

**Targets:**
- an isolated app-engine build on `127.0.0.1:8781` with temporary state and one
  static app;
- `origin/main` on `:8782` for before-and-after comparison;
- the live local Atrium on `:9900`, with non-intrusive checks only.

| Tool | What it does | App-engine result | Atrium result |
|---|---|---|---|
| Scripted probes (curl) | DNS rebinding, `text/plain` CSRF, framing | Before: rebound Host read `/api/local-ai/*` and `/state` (200), CSRF reached chat, `frame-ancestors *`. After: 421, 415, framing limited to loopback | — |
| [Wapiti 3.3.2](https://github.com/wapiti-scanner/wapiti) | Active DAST: SQLi, blind SQLi, XSS, exec, file, SSRF, XXE, CRLF, redirect | No injection or traversal findings. Header and plain-HTTP notes only (loopback by design) | not run against live |
| [Nuclei 3.11](https://github.com/projectdiscovery/nuclei) | Template scan, without `dos`, `fuzz`, `intrusive` or `bruteforce` | Info only. Flagged `/docs` and `/openapi.json` (now removed) and the missing launcher `script-src` (now added) | Info only. Main-page CSP keeps `script-src 'unsafe-inline'`. JARM "C2" matches are false positives on Python's TLS stack |
| [testssl.sh 3.2.4](https://github.com/testssl/testssl.sh) | TLS configuration | n/a (loopback HTTP; public mode uses the proxy's TLS) | TLS 1.2 and 1.3 only, forward secrecy, X25519MLKEM768, HSTS 1 year. **CBC suites still offered on TLS 1.2** (LUCKY13 "potentially"). The self-signed CA is expected because clients pin it |
| [OWASP ZAP](https://www.zaproxy.org/) | Spider plus active scan | Not run: the Docker VM disk was full, and Homebrew disabled the cask for failing Gatekeeper | Not run |

### Recommendations for Atrium from this pen test
1. Drop the TLS 1.2 CBC suites from the server's `SSLContext`, keeping only
   ECDHE+AESGCM and ChaCha20.
2. Move the main page to a nonce CSP, as the vault page did (vault #14). An XSS
   slip there is the vault audit's T5 attacker.
3. Run ZAP's active scan against a throwaway Atrium instance, such as the
   `tests/conftest.py` server fixture, never the live one. To do that, first
   free Docker VM space, or use ZAP's cross-platform JAR.
