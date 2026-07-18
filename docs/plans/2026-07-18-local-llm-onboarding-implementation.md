# Local LLM Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use test-driven development and execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a cross-platform, strictly local Ollama onboarding flow with hardware-based model recommendations, safe model management, actionable chat readiness, and responsive launcher UI.

**Architecture:** Split host inspection, registry policy, Ollama operations, and API orchestration into focused Python modules under `app_engine/`. Keep `engine.py` as the FastAPI composition root and use an injectable `httpx` transport for deterministic tests. The launcher consumes typed readiness and operation endpoints and never executes installer commands itself.

**Tech Stack:** Python 3.11+, FastAPI, httpx, pytest, vanilla HTML/CSS/JavaScript, Ollama HTTP API.

---

### Task 1: Typed model registry and hardware recommendation

**Files:**
- Create: `app_engine/__init__.py`
- Create: `app_engine/models.py`
- Create: `app_engine/registry.py`
- Create: `model-registry.json`
- Test: `tests/test_registry.py`

- [ ] Write tests asserting registry validation, Quality recommendation at 16 GB, Compatibility recommendation below 16 GB, disk warnings, and safe manual override.
- [ ] Run `/Users/yongkim/git/atrium/.venv/bin/python -m pytest tests/test_registry.py -q`; verify failure because modules do not exist.
- [ ] Implement immutable typed records, registry parsing, and deterministic recommendation rules. Bundle exactly one Quality and one Compatibility entry.
- [ ] Re-run the focused test and verify all assertions pass.

### Task 2: Cross-platform read-only system probe

**Files:**
- Create: `app_engine/system_probe.py`
- Test: `tests/test_system_probe.py`

- [ ] Write tests for platform normalization, RAM/disk results, loopback endpoint detection, executable/service states, and partial probe failures.
- [ ] Run the focused test and verify expected missing-module failures.
- [ ] Implement `SystemProbe.inspect()` using `platform`, `shutil`, and OS-native read-only APIs with injectable functions for tests.
- [ ] Re-run the focused test and verify it passes.

### Task 3: Ollama manager, confirmation boundary, and operations

**Files:**
- Create: `app_engine/ollama.py`
- Test: `tests/test_ollama.py`

- [ ] Write tests for health/list status, fixed OS installation plans, expiring single-use confirmations, pull progress, cancellation, managed-model deletion, verification, and error translation.
- [ ] Run the focused test and verify expected failures.
- [ ] Implement fixed adapters: macOS opens the official download, Windows launches the official installer page, and Linux opens the official install instructions; existing installations are started with fixed OS commands. Implement model pull/delete/verification through Ollama’s loopback API. Never interpolate manifest or request data into commands.
- [ ] Re-run the focused test and verify it passes.

### Task 4: Persistent local-AI configuration

**Files:**
- Create: `app_engine/config.py`
- Test: `tests/test_config.py`

- [ ] Write tests for defaults, atomic writes, managed model tracking, corrupt-file quarantine, endpoint loopback validation, and profile switching.
- [ ] Run the focused test and verify expected failures.
- [ ] Implement a schema-versioned JSON store under the configured state directory.
- [ ] Re-run the focused test and verify it passes.

### Task 5: Readiness and model-management HTTP API

**Files:**
- Modify: `engine.py`
- Test: `tests/test_api.py`

- [ ] Write FastAPI client tests for `/api/local-ai/status`, installation plans and confirmation, start, pull progress, cancellation, profile selection, verification, diagnostics, and managed deletion.
- [ ] Run the focused test and verify endpoint-not-found failures.
- [ ] Add dependency-injectable services and typed JSON/SSE responses. Validate all IDs against the bundled registry and all endpoints as loopback.
- [ ] Re-run the focused test and verify it passes.

### Task 6: Harden chat streaming around readiness

**Files:**
- Create: `app_engine/chat.py`
- Modify: `engine.py`
- Test: `tests/test_chat.py`

- [ ] Write tests for setup-required events, system-policy composition, split upstream SSE lines, cold-load status, timeout/protocol/malformed-stream errors, and terminal completion/error events.
- [ ] Run the focused test and verify expected failures.
- [ ] Implement `ChatRuntime` and route `/api/app-chat` through it while preserving the existing app contract.
- [ ] Re-run the focused test and verify it passes.

### Task 7: Guided setup and responsive launcher

**Files:**
- Modify: `launcher.html`
- Test: `tests/test_launcher.py`

- [ ] Write structural tests for safe manifest rendering, setup states, explicit confirmation, progress/cancel/retry controls, local privacy copy, settings controls, accessible labels, and responsive CSS.
- [ ] Run the focused test and verify expected failures.
- [ ] Implement Local AI settings, setup card/wizard, progress polling/SSE handling with chunk buffering, collapsible mobile navigation/chat, and friendly error actions. Use `textContent`, never manifest-driven `innerHTML`.
- [ ] Re-run the focused test and verify it passes.

### Task 8: Packaging, documentation, and full verification

**Files:**
- Modify: `requirements.txt`
- Modify: `README.md`
- Create: `tests/test_security.py`

- [ ] Write security tests for non-loopback rejection, unknown model rejection, confirmation replay, unrelated model deletion, and manifest HTML injection.
- [ ] Run the focused test and verify expected failures before any final fixes.
- [ ] Document the one-command setup, profiles, privacy boundary, supported platforms, and recovery workflow. Add pytest and pytest-asyncio to `requirements-dev.txt`.
- [ ] Run the complete suite: `/Users/yongkim/git/atrium/.venv/bin/python -m pytest tests/ -q`.
- [ ] Run syntax and repository checks: `/Users/yongkim/git/atrium/.venv/bin/python -m compileall -q app_engine engine.py`, `git diff --check`, and a browser smoke test at desktop and 390-pixel widths.
- [ ] Compare the implementation line-by-line with the approved design and record any intentionally deferred installer limitation in README.
