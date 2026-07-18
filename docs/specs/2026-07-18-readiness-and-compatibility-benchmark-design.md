# Readiness Refresh and Compatibility Benchmark Design

**Date:** 2026-07-18  
**Status:** Approved design  
**Scope:** Live chat-readiness propagation and evidence-based selection of a permissively licensed Compatibility model

## Goals

1. Make every mounted tutor panel react immediately when local-AI readiness changes.
2. Replace intuition-based Compatibility model selection with a repeatable local benchmark covering every chat-enabled app.
3. Consider only model weights licensed under Apache-2.0 or MIT.

## Readiness propagation

The launcher owns one browser event contract:

```text
window event: local-ai-status-changed
detail: { reason: ReadinessChangeReason }
```

`ReadinessChangeReason` is one of `profile_changed`, `runtime_started`,
`model_verified`, `model_removed`, or `manual_refresh`.

Each chat panel registers a listener when constructed. The listener calls its
existing `refreshChatReadiness()` function. Refreshing replaces setup/ready
status content but preserves completed conversation messages. A panel with a
request in flight defers the visual refresh until that request finishes.

The launcher dispatches the event only after a successful API response. Closing
settings without a state change does not dispatch. Repeated events are safe and
idempotent.

## Candidate policy

Candidates must:

- have model weights explicitly licensed under Apache-2.0 or MIT by the model
  publisher;
- have an official Ollama tag;
- fit the Compatibility target of approximately 8 GB system RAM;
- require no cloud service; and
- accept the existing Ollama chat message contract.

The benchmark compares:

| Candidate | Ollama tag | Parameters | Approx. download | License |
|---|---|---:|---:|---|
| Baseline | `qwen3:1.7b` | 1.7B | 1.4 GB | Apache-2.0 |
| Candidate A | `qwen3:4b` | 4B | 2.5 GB | Apache-2.0 |
| Candidate B | `phi4-mini` | 3.8B | about 2.5 GB | MIT |

License evidence is recorded in the generated report with direct publisher
URLs. Model licensing does not determine app-engine's repository license; the
repository should use Apache-2.0 separately because its explicit patent grant
is valuable for a developer-facing project.

## Benchmark case schema

The suite is stored as versioned JSON:

```text
BenchmarkSuite {
  version: integer,
  cases: BenchmarkCase[]
}

BenchmarkCase {
  id: string,
  app_id: string,
  category: "fact" | "procedure" | "hint" | "calculation",
  critical: boolean,
  system_prompt: string,
  question: string,
  required_all: string[],
  required_any: string[][],
  forbidden: string[],
  max_seconds: number,
  review_note: string
}
```

Matching is case-insensitive after whitespace normalization. `required_all`
contains facts that must all appear. Each group in `required_any` requires at
least one alternative phrase. Any `forbidden` phrase fails the case. Empty
criteria arrays are valid. Each chat-enabled app receives at least two cases,
and the suite fails validation if a discovered chat-enabled app has no case.

## Runner interface

```text
load_suite(path: Path) -> BenchmarkSuite
validate_suite(suite, app_manifests: AppManifest[]) -> ValidationResult
run_case(endpoint: URL, model_tag: str, case: BenchmarkCase) -> CaseResult
run_suite(endpoint: URL, candidate: Candidate, suite: BenchmarkSuite) -> BenchmarkResult
compare(results: BenchmarkResult[]) -> Comparison
```

`CaseResult` records normalized response text, factual pass/fail, missing
requirements, forbidden matches, elapsed seconds, first-token seconds, output
characters, and transport error. Raw prompts and responses are stored in the
local result artifact because the benchmark questions contain no personal data.

Concrete errors are `InvalidSuiteError`, `OllamaUnavailableError`,
`ModelMissingError`, `ResponseTimeoutError`, and `MalformedResponseError`.
Transport failures fail the case and do not abort remaining candidates.

## Scoring and selection

Selection is lexicographic:

1. A candidate must pass every designated `critical` case. Basic command facts,
   safety-critical instructions, arithmetic, and explicit hint-only behavior
   are critical.
2. Highest factual pass rate wins.
3. If factual pass rates differ by less than five percentage points, lowest
   median response latency wins.
4. If still tied, smaller download size wins.

Response verbosity is reported but is not a selection tiebreaker. A candidate
with a transport failure rate above five percent is ineligible. The baseline is
reported but cannot remain the default if it fails a critical case.

Automated phrase checks are deliberately conservative and supplemented by a
manual-review note. Benchmark results are evidence for this fixed educational
suite, not a general model-quality claim.

## Artifacts

- `benchmarks/tutor-cases.json`: reviewed, versioned factual suite.
- `scripts/benchmark_tutors.py`: local runner with candidate selection and JSON output.
- `benchmarks/results/YYYY-MM-DD.json`: raw machine result, including hardware and Ollama version.
- `benchmarks/results/YYYY-MM-DD.md`: readable comparison and recommendation.

Generated results are committed for the selection run. Later users can rerun
the suite locally without downloading every candidate unless explicitly chosen.

## Testing

- Launcher tests prove mounted setup cards become ready without page reload and
  conversations survive readiness events.
- Unit tests cover suite validation, normalization, required/forbidden matching,
  critical-case gates, latency tiebreaking, transport-error accounting, and
  deterministic report rendering.
- A fake-Ollama integration test covers streaming responses and timeouts.
- The real selection run downloads both candidates, executes all cases, records
  results, and manually reviews every automated failure and a sample of passes.

## Acceptance criteria

- Completing model verification enables every mounted chat panel without page
  reload or app switching.
- Profile switches and managed-model removal refresh mounted panels.
- Every currently discovered chat-enabled personal app has at least two cases.
- Candidate license evidence is Apache-2.0 or MIT from the publisher.
- Results include factual accuracy, critical failures, latency, output length,
  transport reliability, model size, hardware, and Ollama version.
- The Compatibility registry changes only when the selection gate identifies a
  clear winner.
