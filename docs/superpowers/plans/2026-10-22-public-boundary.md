# Public boundary hardening implementation plan

**Goal:** Prevent app backends from receiving engine credentials and prevent an
accidental plaintext public listener.

**Architecture:** Keep browser session authentication at the engine. Strip
engine-owned credentials at both legacy and managed proxy seams, while retaining
ordinary app cookies. Limit the standalone HTTP server to loopback and require
HTTPS for a real public origin. Preserve application redirects as responses.

**Tech stack:** FastAPI, httpx, pytest. Beads issue `atrium-slrr3` tracks work.

This plan implements slice 1 of the approved
`docs/superpowers/specs/2026-10-22-production-security-design.md`. Follow
test-driven-development: run each focused test red, change production code,
then run it green. Commit only after fresh verification.

## Task 1: Legacy proxy credentials and redirects

Files: `tests/test_public_origin.py`, `engine.py`, optionally a focused helper
in `app_engine/public_origin.py`.

1. Add a test that signs in as an owner, requests an `entry_point` app on its
   isolated origin, and captures the upstream request with `httpx.MockTransport`.
   Send `app_engine_session`, an app-owned cookie, `X-App-Engine-Admin`, and
   `X-App-State-Token`. Assert the upstream receives only the app-owned cookie
   and no engine control header. Use a backend response that returns `302` to
   `https://external.example/`; assert the engine returns the 302 and makes no
   second request.
2. Run `/Users/yongkim/git/app-engine/.venv/bin/python -m pytest tests/test_public_origin.py -k
   'proxy_credentials or proxy_redirect' -q` from the worktree through the
   standalone repository's Python environment. The expected failure is leaked
   credentials or a followed redirect.
3. Add a single header filtering helper with the contract
   `forward_app_headers(headers: Iterable[tuple[str,str]]) -> tuple[tuple[str,str], ...]`.
   Remove only the `app_engine_session` name from Cookie, strip
   `X-App-Engine-Admin` and `X-App-State-Token`, and preserve other cookies.
   Apply it to `_proxy_entry_point`; use a proxy client with
   `follow_redirects=False`.
4. Run the focused tests green, then the whole `test_public_origin.py` file.

## Task 2: Managed gateway credentials

Files: `tests/test_gateway_service.py`,
`app_engine/gateway.py`, and the same shared helper from Task 1.

1. Add a gateway test using the existing in-process upstream transport. Send
   the same Cookie and engine control headers in a `ProxyRequest`. Assert only
   app-owned cookies and ordinary headers reach the backend.
2. Run that single test red. The current `_strip_hop_headers` retains them.
3. Apply the shared helper after hop-header filtering on the request path,
   without filtering response Set-Cookie from the app backend.
4. Run the focused test green and the gateway route tests.

## Task 3: Startup and origin safety

Files: `tests/test_startup.py`, `tests/test_public_origin.py`,
`app_engine/public_origin.py`, `README.md`, `docs/deploy-hostinger.md`.

1. Add tests that a public `APP_ENGINE_HOST=0.0.0.0` or LAN address refuses
   startup even with an admin secret; a loopback public deployment with proxy
   trust still starts; a real `http://play.example.com` public origin refuses;
   `http://play.localhost:8770` remains available for a local rehearsal.
2. Run the four tests red. The current public bind and HTTP origin are allowed.
3. Make `bind_is_allowed` reject every non-loopback bind. Make
   `PublicOrigin.parse` require HTTPS unless the hostname ends in `.localhost`.
   Update the README and deployment guide to describe the loopback proxy
   requirement.
4. Run those tests green and both complete focused test files.

## Gate

Run `tests/test_security.py`, `tests/test_startup.py`,
`tests/test_public_origin.py`, and the gateway/route tests. Check
`git diff --check`, inspect the entire diff for unintended files, and use the
repository's review procedure. Commit only the exact tested files. The mTLS
and process isolation plans follow this gate; neither is implied by passing it.
