from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .grounding import (
    ANSWER_SCHEMA, KnowledgeBase, build_grounding_context, detect_misconceptions,
    extract_visible_answer,
)


class InvalidSuiteError(ValueError):
    pass


@dataclass(frozen=True)
class BenchmarkCandidate:
    candidate_id: str
    ollama_tag: str
    download_bytes: int
    license: str
    license_url: str


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    app_id: str
    category: str
    critical: bool
    system_prompt: str
    question: str
    required_all: tuple[str, ...]
    required_any: tuple[tuple[str, ...], ...]
    forbidden: tuple[str, ...]
    max_seconds: float
    review_note: str


@dataclass(frozen=True)
class BenchmarkSuite:
    version: int
    cases: tuple[BenchmarkCase, ...]


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    app_id: str
    critical: bool
    response: str
    factual_pass: bool
    missing_requirements: tuple[str, ...]
    forbidden_matches: tuple[str, ...]
    elapsed_seconds: float
    first_token_seconds: float | None
    output_characters: int
    transport_error: str | None


@dataclass(frozen=True)
class BenchmarkResult:
    candidate: BenchmarkCandidate
    cases: tuple[CaseResult, ...]


@dataclass(frozen=True)
class ComparisonRow:
    candidate_id: str
    eligible: bool
    factual_rate: float
    critical_failures: int
    transport_failure_rate: float
    median_seconds: float
    median_output_characters: float
    download_bytes: int


@dataclass(frozen=True)
class Comparison:
    rows: tuple[ComparisonRow, ...]
    winner: BenchmarkCandidate | None
    results: tuple[BenchmarkResult, ...]


def _phrases(values) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(isinstance(x, str) and x.strip() for x in values):
        raise InvalidSuiteError("phrase criteria must be non-empty strings")
    return tuple(values)


def load_suite(path: Path) -> BenchmarkSuite:
    try:
        raw = json.loads(Path(path).read_text())
        cases = tuple(BenchmarkCase(
            id=item["id"], app_id=item["app_id"], category=item["category"],
            critical=bool(item["critical"]), system_prompt=item["system_prompt"],
            question=item["question"], required_all=_phrases(item["required_all"]),
            required_any=tuple(_phrases(group) for group in item["required_any"]),
            forbidden=_phrases(item["forbidden"]) if item["forbidden"] else (),
            max_seconds=float(item["max_seconds"]), review_note=item["review_note"],
        ) for item in raw["cases"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidSuiteError(str(exc)) from exc
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise InvalidSuiteError("case IDs must be unique")
    if not cases:
        raise InvalidSuiteError("suite has no cases")
    return BenchmarkSuite(int(raw.get("version", 1)), cases)


def normalize(text: str) -> str:
    text = text.casefold().replace("\\times", "×")
    return re.sub(r"\s+", " ", text).strip()


def evaluate_response(case: BenchmarkCase, response: str, elapsed: float,
                      first_token: float | None, transport_error: str | None = None) -> CaseResult:
    normal = normalize(response)
    missing = [phrase for phrase in case.required_all if normalize(phrase) not in normal]
    for group in case.required_any:
        if not any(normalize(phrase) in normal for phrase in group):
            missing.append(" | ".join(group))
    forbidden = tuple(phrase for phrase in case.forbidden if normalize(phrase) in normal)
    passed = not transport_error and not missing and not forbidden and elapsed <= case.max_seconds
    return CaseResult(case.id, case.app_id, case.critical, response, passed,
                      tuple(missing), forbidden, elapsed, first_token, len(response), transport_error)


async def run_case(client: httpx.AsyncClient, ollama_tag: str, case: BenchmarkCase,
                   clock: Callable[[], float] = time.perf_counter,
                   knowledge: KnowledgeBase | None = None) -> CaseResult:
    started = clock()
    first_token: float | None = None
    chunks: list[str] = []
    error: str | None = None
    references = knowledge.retrieve(case.app_id, case.question) if knowledge else ()
    system_prompt = case.system_prompt + " Answer directly; do not show private chain-of-thought or planning."
    grounding = build_grounding_context(references)
    if grounding:
        system_prompt += f"\n\n{grounding}"
    payload = {
        "model": ollama_tag,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": case.question},
        ],
        "stream": True,
        "think": False,
        "format": ANSWER_SCHEMA,
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 384},
    }

    async def collect(request_payload: dict) -> list[str]:
        nonlocal first_token
        collected: list[str] = []
        async with client.stream("POST", "/api/chat", json=request_payload,
                                 timeout=httpx.Timeout(case.max_seconds)) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                content = str(event.get("message", {}).get("content", ""))
                if content:
                    if first_token is None:
                        first_token = clock() - started
                    collected.append(content)
        return collected
    try:
        chunks = extract_visible_answer(await collect(payload))
        misconceptions = detect_misconceptions("".join(chunks), references)
        if misconceptions:
            corrections = "\n".join(f"- {item.correction}" for item in misconceptions)
            retry_payload = dict(payload)
            retry_payload["messages"] = [*payload["messages"],
                {"role": "assistant", "content": "".join(chunks)},
                {"role": "user", "content": f"Correct the answer using these reviewed facts. Return only the corrected answer:\n{corrections}"},
            ]
            chunks = extract_visible_answer(await collect(retry_payload))
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.ConnectError:
        error = "connection"
    except httpx.HTTPStatusError as exc:
        error = f"http_{exc.response.status_code}"
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
        error = f"malformed_response: {type(exc).__name__}"
    elapsed = clock() - started
    return evaluate_response(case, "".join(chunks).strip(), elapsed, first_token, error)


async def run_candidate(client: httpx.AsyncClient, candidate: BenchmarkCandidate,
                        cases: tuple[BenchmarkCase, ...],
                        knowledge: KnowledgeBase | None = None) -> BenchmarkResult:
    results = []
    for benchmark_case in cases:
        results.append(await run_case(client, candidate.ollama_tag, benchmark_case, knowledge=knowledge))
    return BenchmarkResult(candidate, tuple(results))


def _row(result: BenchmarkResult) -> ComparisonRow:
    count = len(result.cases)
    factual_rate = sum(case.factual_pass for case in result.cases) / count if count else 0.0
    critical = sum(case.critical and not case.factual_pass for case in result.cases)
    failures = sum(case.transport_error is not None for case in result.cases)
    completed = [case.elapsed_seconds for case in result.cases if case.transport_error is None]
    lengths = [case.output_characters for case in result.cases if case.transport_error is None]
    failure_rate = failures / count if count else 1.0
    return ComparisonRow(
        result.candidate.candidate_id, critical == 0 and failure_rate <= 0.05,
        factual_rate, critical, failure_rate,
        statistics.median(completed) if completed else float("inf"),
        statistics.median(lengths) if lengths else 0.0,
        result.candidate.download_bytes,
    )


def compare_results(results: tuple[BenchmarkResult, ...]) -> Comparison:
    rows = tuple(_row(result) for result in results)
    eligible = [row for row in rows if row.eligible]
    winner = None
    if eligible:
        best_rate = max(row.factual_rate for row in eligible)
        contenders = [row for row in eligible if best_rate - row.factual_rate < 0.05]
        chosen = min(contenders, key=lambda row: (row.median_seconds, row.download_bytes, row.candidate_id))
        winner = next(result.candidate for result in results if result.candidate.candidate_id == chosen.candidate_id)
    return Comparison(rows, winner, results)


def render_markdown(comparison: Comparison, metadata: dict) -> str:
    lines = ["# Tutor Compatibility Benchmark", "", "## Environment", ""]
    for key in sorted(metadata):
        lines.append(f"- **{key}:** {metadata[key]}")
    lines += ["", "## Comparison", "", "| Candidate | Eligible | Factual pass | Critical failures | Transport failures | Median latency | Download |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for row in comparison.rows:
        lines.append(f"| {row.candidate_id} | {'Yes' if row.eligible else 'No'} | {row.factual_rate*100:.1f}% | {row.critical_failures} | {row.transport_failure_rate*100:.1f}% | {row.median_seconds:.2f}s | {row.download_bytes/1e9:.2f} GB |")
    lines += ["", f"**Winner: `{comparison.winner.candidate_id}`**" if comparison.winner else "**Winner: none (no eligible candidate)**", "", "## Licenses", ""]
    for result in comparison.results:
        c = result.candidate
        lines.append(f"- `{c.candidate_id}`: [{c.license}]({c.license_url})")
    lines += ["", "## Case results", ""]
    for result in comparison.results:
        lines.append(f"### {result.candidate.candidate_id}")
        lines.append("")
        for case in result.cases:
            detail = "PASS" if case.factual_pass else "FAIL"
            lines.append(f"- **{case.case_id}: {detail}** ({case.elapsed_seconds:.2f}s) — missing: {', '.join(case.missing_requirements) or 'none'}; forbidden: {', '.join(case.forbidden_matches) or 'none'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
