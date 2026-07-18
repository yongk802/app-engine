# Tutor Compatibility Benchmark

## Environment

- **architecture:** arm64
- **endpoint:** http://127.0.0.1:11434
- **ollama_version:** ollama version is 0.31.1
- **platform:** macOS-26.5.1-arm64-arm-64bit
- **ram_bytes:** 68719476736
- **suite_version:** 1
- **timestamp_utc:** 2026-07-18T22:46:33.460863+00:00

## Comparison

| Candidate | Eligible | Factual pass | Critical failures | Transport failures | Median latency | Download |
|---|---:|---:|---:|---:|---:|---:|
| qwen3-1.7b | Yes | 100.0% | 0 | 0.0% | 0.46s | 1.40 GB |
| qwen3-4b | No | 80.0% | 2 | 0.0% | 0.95s | 2.50 GB |
| phi4-mini | No | 25.0% | 7 | 0.0% | 0.78s | 2.50 GB |

**Winner: `qwen3-1.7b`**

## Licenses

- `qwen3-1.7b`: [Apache-2.0](https://huggingface.co/Qwen/Qwen3-1.7B/blob/main/LICENSE)
- `qwen3-4b`: [Apache-2.0](https://huggingface.co/Qwen/Qwen3-4B/blob/main/LICENSE)
- `phi4-mini`: [MIT](https://huggingface.co/microsoft/Phi-4-mini-instruct/blob/main/LICENSE)

## Case results

### qwen3-1.7b

- **algo-binary-search: PASS** (0.94s) — missing: none; forbidden: none
- **algo-two-sum-hint: PASS** (0.30s) — missing: none; forbidden: none
- **coding-mutable-default: PASS** (0.62s) — missing: none; forbidden: none
- **coding-missing-await: PASS** (0.32s) — missing: none; forbidden: none
- **ctf-base64: PASS** (0.28s) — missing: none; forbidden: none
- **ctf-sql-injection: PASS** (0.34s) — missing: none; forbidden: none
- **ffmpeg-stream-copy: PASS** (0.58s) — missing: none; forbidden: none
- **ffmpeg-seek-placement: PASS** (0.56s) — missing: none; forbidden: none
- **film-dolly-zoom: PASS** (0.47s) — missing: none; forbidden: none
- **film-180-rule: PASS** (0.50s) — missing: none; forbidden: none
- **linux-chmod-755: PASS** (0.31s) — missing: none; forbidden: none
- **linux-signals: PASS** (0.86s) — missing: none; forbidden: none
- **llm-kv-cache: PASS** (0.45s) — missing: none; forbidden: none
- **llm-quantization: PASS** (0.58s) — missing: none; forbidden: none
- **math-dot-product: PASS** (0.29s) — missing: none; forbidden: none
- **math-softmax: PASS** (0.31s) — missing: none; forbidden: none
- **vim-ciw: PASS** (0.52s) — missing: none; forbidden: none
- **vim-dd: PASS** (0.28s) — missing: none; forbidden: none
- **vllm-paged-attention: PASS** (0.51s) — missing: none; forbidden: none
- **vllm-continuous-batching: PASS** (0.43s) — missing: none; forbidden: none

### qwen3-4b

- **algo-binary-search: PASS** (1.02s) — missing: none; forbidden: none
- **algo-two-sum-hint: PASS** (0.77s) — missing: none; forbidden: none
- **coding-mutable-default: FAIL** (1.18s) — missing: defined once | created only once | evaluated once; forbidden: none
- **coding-missing-await: PASS** (1.25s) — missing: none; forbidden: none
- **ctf-base64: PASS** (0.53s) — missing: none; forbidden: none
- **ctf-sql-injection: FAIL** (0.33s) — missing: data, code, separate | separates; forbidden: none
- **ffmpeg-stream-copy: PASS** (0.96s) — missing: none; forbidden: none
- **ffmpeg-seek-placement: PASS** (0.80s) — missing: none; forbidden: none
- **film-dolly-zoom: PASS** (0.94s) — missing: none; forbidden: none
- **film-180-rule: PASS** (0.70s) — missing: none; forbidden: none
- **linux-chmod-755: PASS** (0.65s) — missing: none; forbidden: none
- **linux-signals: PASS** (1.81s) — missing: none; forbidden: none
- **llm-kv-cache: FAIL** (1.09s) — missing: keys; forbidden: none
- **llm-quantization: PASS** (1.11s) — missing: none; forbidden: none
- **math-dot-product: PASS** (0.57s) — missing: none; forbidden: none
- **math-softmax: FAIL** (0.95s) — missing: probabilities, sum to 1 | sum is 1 | sum equals 1; forbidden: none
- **vim-ciw: PASS** (1.03s) — missing: none; forbidden: none
- **vim-dd: PASS** (0.55s) — missing: none; forbidden: none
- **vllm-paged-attention: PASS** (1.86s) — missing: none; forbidden: none
- **vllm-continuous-batching: PASS** (0.80s) — missing: none; forbidden: none

### phi4-mini

- **algo-binary-search: PASS** (1.02s) — missing: none; forbidden: none
- **algo-two-sum-hint: FAIL** (0.46s) — missing: hash map, complement; forbidden: none
- **coding-mutable-default: FAIL** (0.87s) — missing: defined once | created only once | evaluated once; forbidden: none
- **coding-missing-await: FAIL** (0.69s) — missing: not executed | doesn't run | does not run | instead of being executed | must be awaited or scheduled; forbidden: none
- **ctf-base64: FAIL** (0.36s) — missing: not encryption, reversible; forbidden: none
- **ctf-sql-injection: FAIL** (0.35s) — missing: data, code, separate | separates; forbidden: none
- **ffmpeg-stream-copy: FAIL** (0.61s) — missing: remux, no decode | does not decode | without decoding | no decoding | avoids decoding, no re-encode | does not re-encode | without re-encoding | no re-encoding | zero re-encoding | avoids decoding and re-encoding | avoids re-encoding; forbidden: none
- **ffmpeg-seek-placement: PASS** (0.50s) — missing: none; forbidden: none
- **film-dolly-zoom: PASS** (0.52s) — missing: none; forbidden: none
- **film-180-rule: FAIL** (0.61s) — missing: axis, screen direction, spatial | orientation; forbidden: none
- **linux-chmod-755: FAIL** (3.99s) — missing: r-x | read and execute; forbidden: none
- **linux-signals: PASS** (1.22s) — missing: none; forbidden: none
- **llm-kv-cache: FAIL** (0.60s) — missing: keys, values, previous tokens, avoid recomputing | avoids recomputing | not recompute; forbidden: none
- **llm-quantization: FAIL** (0.60s) — missing: lower precision, quality | accuracy; forbidden: none
- **math-dot-product: PASS** (4.07s) — missing: none; forbidden: none
- **math-softmax: FAIL** (3.15s) — missing: next token, sum to 1 | sum is 1 | sum equals 1; forbidden: none
- **vim-ciw: FAIL** (4.30s) — missing: anywhere in | inside the word | within the word | any position within; forbidden: none
- **vim-dd: FAIL** (1.71s) — missing: current line; forbidden: none
- **vllm-paged-attention: FAIL** (2.80s) — missing: kv cache; forbidden: none
- **vllm-continuous-batching: FAIL** (1.19s) — missing: gpu utilization; forbidden: none
