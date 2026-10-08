# App-engine chat panel implementation plan

**Goal:** Embed an external chat UI and give its harness authenticated, lazy MCP app access.

**Architecture:** A package-owned panel and owner settings router accompany a stateless MCP transport. App tools use catalog metadata, standalone state and the existing runtime gateway; no model loop is added.

**Tech stack:** Python/FastAPI/httpx, browser JavaScript/CSS, pytest and Playwright. Work tracked in beads atrium-ms6rf, not document checkboxes. Execute using subagent-driven-development for the separate UI task and independent reviews.

## Transport and settings

Create `app_engine/assistant.py` for persistent settings, key hashing and HTTP routes. Create `tests/test_assistant.py` first using `TestClient(engine.app)` in a temporary state directory. Assert settings are owner/capability gated; PUT saves a validated URL; POST key returns a bearer credential; unauthenticated MCP returns 401; initialize returns protocol 2025-11-25; rotation invalidates the old key. Run `.venv/bin/python -m pytest tests/test_assistant.py -q`, observe missing routes, then implement and rerun. Wire a router in `engine.py` near store routes, preserving independent chat/tutor code.

## App operations

Create `app_engine/app_tools.py`. Write `tests/test_app_tools.py` first with real temporary static and HTTP backend fixtures. Prove `apps_list` sees installed apps without gateway.open; `apps_read_state` and `apps_write_state` round trip state using revision preconditions; `apps_open` enqueues a visible launcher action. Add an `app-tools.json` fixture with a note action and validate actual POST arguments/results, bounded output, path refusal and lease cleanup. Use runtime preview/authorize only when existing standing approval permits; otherwise return approval-required. Discovery uses the immutable catalog and bounded sidecars, with no code import or backend request.

## Panel

Create package assets `app_engine/assistant_ui/panel.js` and `panel.css`. Hook external script/style into `launcher.html` and expose a narrow host bridge that maps app ID to existing `openApp` behavior after catalog refresh. A Chat button opens setup; settings request occurs only then. Save URL, Connect, Collapse, Disconnect and Settings work with keyboard-accessible controls. Setup offers MCP URL plus explicit key generation/revocation. Key appears once and never goes to iframe. Poll events only while expanded. Add DOM tests before implementation. Add package-data patterns in `pyproject.toml` and `MANIFEST.in` if needed.

## Integration and verification

Document setup, harness reachability, embedding requirements, authentication, lazy behavior, app-tools declaration and public-origin limitation in `docs/assistant.md`. Add README link. Run focused pytest, JavaScript tests, then repository suite if bounded. Launch an isolated standalone engine with fixture apps and a fixture chat page; use native Playwright to configure/connect, issue MCP calls, inspect actual app effects and capture desktop/mobile screenshots. Independently review spec compliance then code quality. Commit only owned files and push the standalone repository branch/main per existing authority, preserving the shared checkout's speech edits.
