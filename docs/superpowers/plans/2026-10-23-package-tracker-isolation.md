# Package Tracker Isolation Compatibility Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task by task. Track progress in Beads issue `atrium-slrr3.3.1`.

**Goal:** Run Package Tracker's mail, carrier, and page workflows under app-engine's enforced command isolation without giving a rendered page the mail credential or allowing private-network egress.

**Architecture:** A root-owned CONNECT proxy validates authenticated destinations and pins resolved public IPs. A distinct DynamicUser browser worker uses that proxy and exposes a bounded loopback lookup API; the app uses a separate fixed-host carrier proxy and mTLS mail route through the existing reverse tunnel. The broker grants only Package Tracker's start phase the needed credentials and mounts.

**Tech Stack:** Python 3.13, stdlib sockets and HTTP, Playwright Chromium, systemd transient and persistent units, nginx mTLS, pytest and unittest, `uv`, GitHub CI.

---

## Files and boundaries

- `app_engine/package_tracker_egress.py`: CONNECT authentication, authority and DNS validation, pinned-IP tunneling.
- `app_engine/isolation_broker.py`, `app_engine/isolated_units.py`: app/phase-specific secret injection and mTLS read-only mounts.
- `tests/test_package_tracker_egress.py`, `tests/test_isolated_units.py`, `tests/test_isolation_broker.py`: proxy and broker negative/positive gates.
- `scripts/install-package-tracker-runtime.sh`: root-owned pinned toolchain and service installation; preflight-only mode.
- `package-tracker/browser_worker.py`: secret-free lookup HTTP worker.
- `package-tracker/page_lookup.py`, `package-tracker/server.py`: remote lookup client and browser proxy configuration.
- `package-tracker/carriers.py`, `package-tracker/inbox.py`, `package-tracker/app.json`: explicit carrier proxy, verified mTLS mail, offline process start.
- `package-tracker/tests/test_security_boundaries.py`: malformed URLs, state modes, worker API and client gates.
- Mac gateway config: narrow read-only mTLS mail locations, tested before reload.

## Task 1: Authenticated destination proxy (app-engine)

- Add a failing pytest module that starts the real proxy on a loopback ephemeral port and checks missing credentials yield 407, denied private/mixed DNS and port 80 yield 403, and a pinned public test socket is selected from vetted `getaddrinfo` results. Model redirects and subresources as separate CONNECT requests. Use dependency injection only for DNS and dialing, not the policy itself.
- Run `.venv/bin/python -m pytest tests/test_package_tracker_egress.py -q` and confirm the expected missing-module/behavior failure.
- Implement `EgressPolicy.authorize(profile: str, authority: str) -> (hostname, ip, port)` and `serve_proxy(config_path: Path)` in `app_engine/package_tracker_egress.py`. Require HTTP Basic proxy auth, 443 only, ASCII DNS names or public IP literals, all DNS answers globally routable, no userinfo, no HTTP forwarding. Connect to the selected numeric IP, then tunnel bytes with bounded buffers and timeouts. Never log secrets or URLs with query strings.
- Run the focused pytest module, then the existing isolated-runner tests. Commit only the proxy and tests.

## Task 2: Broker capability scope (app-engine)

- Add failing tests in `tests/test_isolated_units.py` and `tests/test_isolation_broker.py`: only `package-tracker` phase `start` receives the two app credentials and the read-only mTLS directory mount; another app and install/build phases receive none; absent/unsafe root policy fails closed. Verify credentials never appear in `systemd-run` argv.
- Run the focused tests and observe the expected failure.
- Extend the root-owned runner policy with an optional Package Tracker capability file, validate regular root ownership and non-writable mode, inject only into the executor stdin plan, and build fixed read-only mounts for its certificate, key, and CA. Keep source secrets under root-only directories.
- Run focused tests, `python -m compileall`, and commit the broker change.

## Task 3: Browser worker and app caller (personal-apps)

- Add failing `package-tracker/tests/test_security_boundaries.py` cases for an unauthenticated worker request, oversized/malformed body, private/non-HTTPS top-level URL, valid carrier page result, and a caller that cannot receive arbitrary HTML or credentials. Check browser launch proxy options and service-worker blocking through the public Browser constructor.
- Run `python3 -m unittest discover package-tracker/tests -p 'test_security_boundaries.py'` and observe the expected failure.
- Implement `POST /lookup` in `browser_worker.py`, `RemoteBrowser.lookup(...)` in `page_lookup.py`, and selection in `server.py`. Keep the worker's environment free of Atrium and carrier secrets. The worker runs existing `lookup_page`/`lookup_status_url`, returns only normalized results and bounded errors, and uses Playwright's authenticated proxy, `--proxy-bypass-list=<-loopback>`, and `service_workers='block'`.
- Run focused and existing Package Tracker unit tests. Commit the worker and caller.

## Task 4: Carrier, mail, and private state (personal-apps)

- Add failing tests for explicit carrier proxy auth and exact fixed hosts, rejection of carrier redirects, verified Atrium CA/client certificate loading, no unverified loopback TLS, and mode 0600 for all written state files. Use an HTTPS local test server for TLS behavior rather than mocking SSLContext calls.
- Run the tests and confirm the expected failures.
- Change the manifest start to `python3 server.py`, wire carrier requests through the carrier proxy only when configured, deny redirects, and load an explicit Atrium mTLS SSLContext from mounted CA/cert/key paths. Make state writes atomic with mode 0600. Preserve local use with system-trusted Atrium TLS and optional installed Playwright.
- Run all Package Tracker tests and commit the app changes.

## Task 5: Installer, Mac gateway, and staged production gates

- Add a checked deployment script in app-engine that pins the Playwright Python version and Chromium revision, installs both in root-owned `/opt/package-tracker`, and installs separate proxy/worker systemd units with DynamicUser, read-only mounts, and denied direct external IPs. Add a `--check` mode that validates config and reports planned changes without applying them.
- Test the script's `--check` mode on a disposable directory, then install on the VPS after backing up unit files and current data. Keep `APP_ENGINE_BLOCKED_APP_IDS=package-tracker` and `APP_ENGINE_PUBLIC_APPS=cyberpunk-tcg` in force.
- Issue a distinct `package-tracker-client` certificate from the offline CA, install only its role files, add exactly three GET mail locations to the Mac nginx gateway, run `nginx -t`, reload that gateway, and verify wrong certificate/method/path fail while valid certificate plus existing scoped API key reads accounts and a message. Never expose the key in command output.
- Back up Mac Package Tracker data and transfer it to the VPS scoped data directory with mode 0600. Independently exercise live carrier, mail, and browser worker calls and negative private/metadata/redirect/subresource paths while the app launch block remains active.
- Recheck Night City Table, app-store, SMTP configuration, engine and mTLS health. Present the exact Package Tracker launch preview and fingerprint for owner approval. After approval, remove the block, activate Package Tracker, run integrated persistence and lookup gates, and add it to the public allowlist only if requested. Roll back units and data from the saved backup if any gate fails.

## Review and publication

- Review each diff for secrets, unsafe broad network access, path traversal, and accidental unrelated files. Run focused suites and repository CI on exact committed heads. Push app-engine and personal-apps PRs, attach them to this task, and merge only after checks. Record evidence and remaining launch approval in `atrium-slrr3.3.1`.
