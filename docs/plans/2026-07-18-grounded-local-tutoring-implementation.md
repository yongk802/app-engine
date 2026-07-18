# Grounded Local Tutoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve factual tutoring quality with reviewed local knowledge, deterministic misconception checks, and one corrective retry while preserving strictly local operation.

**Architecture:** A dependency-free `KnowledgeBase` loads reviewed JSON entries and ranks them by normalized keyword overlap. `ChatRuntime` injects the highest-ranked app-specific entries, buffers the local response, checks only explicit known misconceptions, retries once when needed, and emits the final response through the existing typed stream.

**Tech Stack:** Python 3.12, dataclasses, JSON, regular expressions, httpx, pytest, Ollama native chat API.

---

### Task 1: Knowledge retrieval and misconception detection

**Files:**
- Create: `app_engine/grounding.py`
- Create: `tests/test_grounding.py`

- [ ] Write failing tests for app isolation, keyword ranking, empty-query fallback, context formatting, and regular-expression misconception detection.
- [ ] Run `python -m pytest tests/test_grounding.py -q` and confirm import/API failures.
- [ ] Implement immutable `KnowledgeEntry`, `Misconception`, and `KnowledgeBase` types plus `build_grounding_context` and `detect_misconceptions`.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Reviewed tutor knowledge packs

**Files:**
- Create: `knowledge/tutors.json`
- Create: `tests/test_knowledge_packs.py`

- [ ] Write a failing validation test requiring every chat-enabled personal app to have at least two reviewed entries and every critical benchmark topic to be represented.
- [ ] Run the test and confirm it fails because the knowledge pack is missing.
- [ ] Add concise reviewed facts, keywords, source notes, and narrowly scoped misconception patterns for all ten tutor apps.
- [ ] Run the validation test and confirm it passes.

### Task 3: Grounded chat and one corrective retry

**Files:**
- Modify: `app_engine/chat.py`
- Modify: `engine.py`
- Modify: `tests/test_chat.py`

- [ ] Write failing chat tests proving app-specific context is injected, `think` is disabled, output is bounded, a known misconception triggers exactly one retry, a good answer does not retry, and retry failure returns the original usable answer.
- [ ] Run focused tests and confirm the new expectations fail.
- [ ] Inject `KnowledgeBase` into `ChatRuntime`, add `app_id` to `stream`, buffer native Ollama chunks, validate, and retry once with correction text.
- [ ] Wire the repository knowledge pack from `engine.py` and preserve actionable transport errors.
- [ ] Run focused chat and server tests and confirm they pass.

### Task 4: Benchmark the grounded path

**Files:**
- Modify: `app_engine/benchmark.py`
- Modify: `scripts/benchmark_tutors.py`
- Modify: `tests/test_benchmark.py`

- [ ] Write failing tests that benchmark prompts include retrieved app-specific facts and corrections use the same deterministic detector.
- [ ] Implement a benchmark adapter around the grounded generation path so production and evaluation do not diverge.
- [ ] Rerun the three installed models and write a dated grounded JSON/Markdown report without changing defaults unless an eligible winner emerges.

### Task 5: Documentation and verification

**Files:**
- Modify: `README.md`

- [ ] Document local knowledge packs, privacy, contribution format, retry behavior, and limitations.
- [ ] Run `python -m pytest tests/ -q`.
- [ ] Run `python -m compileall -q app_engine engine.py scripts/benchmark_tutors.py`.
- [ ] Run `git diff --check` and inspect the final diff.
- [ ] Commit the implementation and benchmark evidence.
