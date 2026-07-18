# Readiness Refresh and Tutor Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use test-driven development and execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh mounted tutor panels immediately after local-AI state changes and select a stronger permissively licensed Compatibility model using a reproducible factual benchmark.

**Architecture:** Keep readiness propagation inside the launcher through one typed browser event. Add a standalone Python benchmark package that loads versioned JSON cases, talks directly to Ollama, scores deterministic factual criteria, and writes JSON/Markdown artifacts without depending on FastAPI.

**Tech Stack:** Vanilla JavaScript browser events, Python dataclasses, httpx, pytest, Ollama native chat API.

---

### Task 1: Mounted chat readiness propagation

**Files:**
- Modify: `launcher.html`
- Modify: `tests/test_launcher.py`

- [ ] Add a failing structural test requiring the `local-ai-status-changed` event, all five reason strings, a listener in `buildChat`, preservation of conversation nodes, and dispatch only after successful mutations.
- [ ] Run `python -m pytest tests/test_launcher.py -q` and verify the new test fails.
- [ ] Add `notifyAIStatusChanged(reason)`, make status nodes individually replaceable, subscribe mounted chat panels, defer refresh during an active request, and dispatch after profile/start/verify/remove/manual refresh.
- [ ] Re-run the launcher tests and verify they pass.

### Task 2: Benchmark domain and scoring

**Files:**
- Create: `app_engine/benchmark.py`
- Create: `tests/test_benchmark.py`

- [ ] Add failing tests for schema validation, case normalization, required-all/required-any/forbidden matching, critical gates, transport eligibility, factual-rate ordering, latency tie-breaking, and deterministic Markdown output.
- [ ] Run the focused tests and verify import failure.
- [ ] Implement typed candidate, case, case-result, benchmark-result, and comparison records plus pure validation/scoring/report functions.
- [ ] Re-run the focused tests and verify they pass.

### Task 3: Complete tutor case suite

**Files:**
- Create: `benchmarks/tutor-cases.json`
- Create: `tests/test_benchmark_cases.py`

- [ ] Add a failing test that discovers manifests from `APP_ENGINE_APPS_DIR`, requires exactly two or more cases per chat-enabled app, unique case IDs, at least one critical case per app, and non-empty factual criteria.
- [ ] Run the test against an absent suite and verify failure.
- [ ] Add 20 reviewed cases covering Algo Drill, Coding Challenges, CTF Arena, FFmpeg Lab, Film School, Linux Ops Academy, LLM Stack, Math for AI, Vim Dojo, and vLLM Stack.
- [ ] Re-run suite validation and verify it passes.

### Task 4: Local Ollama runner and reports

**Files:**
- Create: `scripts/benchmark_tutors.py`
- Modify: `app_engine/benchmark.py`
- Modify: `tests/test_benchmark.py`

- [ ] Add failing fake-Ollama tests for native NDJSON streaming, first-token and total latency, timeouts, malformed responses, continuation after transport failure, and hardware/version metadata.
- [ ] Run focused tests and verify failure.
- [ ] Implement CLI candidate selection, sequential case execution, result JSON, Markdown report rendering, and non-zero exit when no candidate is eligible.
- [ ] Re-run focused tests and a fake-Ollama CLI smoke test.

### Task 5: Real selection run and registry update

**Files:**
- Create: `benchmarks/results/2026-07-18.json`
- Create: `benchmarks/results/2026-07-18.md`
- Modify only if selected: `model-registry.json`, `README.md`, registry expectations in tests

- [ ] Verify publisher license evidence for `qwen3:4b` (Apache-2.0) and `phi4-mini` (MIT), and record URLs in the report.
- [ ] Pull both candidate tags through Ollama while preserving baseline and unrelated models.
- [ ] Run all 20 cases against `qwen3:1.7b`, `qwen3:4b`, and `phi4-mini`.
- [ ] Manually review all automated failures and at least two passes per candidate; amend only objectively incorrect phrase criteria, then rerun all candidates if criteria change.
- [ ] Apply the documented critical/factual/latency selection rule. Update Compatibility only for a clear eligible winner.
- [ ] Run `python -m pytest tests/ -q`, `python -m compileall -q app_engine engine.py scripts/benchmark_tutors.py`, `git diff --check`, and a browser test proving an open setup card becomes ready without reload.
