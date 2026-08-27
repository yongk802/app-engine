# Tutor Compatibility Benchmark

## Environment

- **architecture:** arm64
- **endpoint:** http://127.0.0.1:11434
- **ollama_version:** ollama version is 0.31.1
- **platform:** macOS-26.5.1-arm64-arm-64bit
- **ram_bytes:** 68719476736
- **suite_version:** 1
- **timestamp_utc:** 2026-08-13T10:02:55.968060+00:00

## Comparison

| Candidate | Eligible | Factual pass | Critical failures | Transport failures | Median latency | Download |
|---|---:|---:|---:|---:|---:|---:|
| qwen3-1.7b | Yes | 96.2% | 0 | 0.0% | 0.45s | 1.40 GB |
| qwen3-4b | Yes | 96.2% | 0 | 0.0% | 0.82s | 2.50 GB |

**Winner: `qwen3-1.7b`**

## Licenses

- `qwen3-1.7b`: [Apache-2.0](https://huggingface.co/Qwen/Qwen3-1.7B/blob/main/LICENSE)
- `qwen3-4b`: [Apache-2.0](https://huggingface.co/Qwen/Qwen3-4B/blob/main/LICENSE)

## Case results

### qwen3-1.7b

- **algo-binary-search: PASS** (9.04s) — missing: none; forbidden: none
- **algo-two-sum-hint: PASS** (0.30s) — missing: none; forbidden: none
- **coding-mutable-default: PASS** (0.56s) — missing: none; forbidden: none
- **coding-missing-await: PASS** (0.30s) — missing: none; forbidden: none
- **ctf-base64: PASS** (0.27s) — missing: none; forbidden: none
- **ctf-sql-injection: PASS** (0.33s) — missing: none; forbidden: none
- **ffmpeg-stream-copy: PASS** (0.59s) — missing: none; forbidden: none
- **ffmpeg-seek-placement: PASS** (0.52s) — missing: none; forbidden: none
- **film-dolly-zoom: PASS** (0.44s) — missing: none; forbidden: none
- **film-180-rule: PASS** (0.46s) — missing: none; forbidden: none
- **linux-chmod-755: PASS** (0.28s) — missing: none; forbidden: none
- **linux-signals: PASS** (0.78s) — missing: none; forbidden: none
- **llm-kv-cache: PASS** (0.42s) — missing: none; forbidden: none
- **llm-quantization: PASS** (0.61s) — missing: none; forbidden: none
- **math-dot-product: PASS** (0.27s) — missing: none; forbidden: none
- **math-softmax: PASS** (0.29s) — missing: none; forbidden: none
- **vim-ciw: PASS** (0.48s) — missing: none; forbidden: none
- **vim-dd: PASS** (0.28s) — missing: none; forbidden: none
- **vllm-paged-attention: PASS** (0.47s) — missing: none; forbidden: none
- **vllm-continuous-batching: PASS** (0.40s) — missing: none; forbidden: none
- **aifs-gradient-direction: PASS** (0.60s) — missing: none; forbidden: none
- **aifs-overfitting: PASS** (0.61s) — missing: none; forbidden: none
- **gift-specific-evidence: PASS** (0.58s) — missing: none; forbidden: none
- **gift-live-claims: FAIL** (0.29s) — missing: local; forbidden: none
- **qc-born-rule: PASS** (0.34s) — missing: none; forbidden: none
- **qc-no-signaling: PASS** (0.65s) — missing: none; forbidden: none

### qwen3-4b

- **algo-binary-search: PASS** (0.40s) — missing: none; forbidden: none
- **algo-two-sum-hint: PASS** (0.67s) — missing: none; forbidden: none
- **coding-mutable-default: PASS** (1.09s) — missing: none; forbidden: none
- **coding-missing-await: PASS** (1.26s) — missing: none; forbidden: none
- **ctf-base64: PASS** (0.45s) — missing: none; forbidden: none
- **ctf-sql-injection: FAIL** (0.26s) — missing: data, code, separate | separates; forbidden: none
- **ffmpeg-stream-copy: PASS** (0.80s) — missing: none; forbidden: none
- **ffmpeg-seek-placement: PASS** (0.63s) — missing: none; forbidden: none
- **film-dolly-zoom: PASS** (0.79s) — missing: none; forbidden: none
- **film-180-rule: PASS** (0.57s) — missing: none; forbidden: none
- **linux-chmod-755: PASS** (0.51s) — missing: none; forbidden: none
- **linux-signals: PASS** (1.60s) — missing: none; forbidden: none
- **llm-kv-cache: PASS** (0.96s) — missing: none; forbidden: none
- **llm-quantization: PASS** (0.98s) — missing: none; forbidden: none
- **math-dot-product: PASS** (0.43s) — missing: none; forbidden: none
- **math-softmax: PASS** (0.76s) — missing: none; forbidden: none
- **vim-ciw: PASS** (0.85s) — missing: none; forbidden: none
- **vim-dd: PASS** (0.44s) — missing: none; forbidden: none
- **vllm-paged-attention: PASS** (1.55s) — missing: none; forbidden: none
- **vllm-continuous-batching: PASS** (0.69s) — missing: none; forbidden: none
- **aifs-gradient-direction: PASS** (0.90s) — missing: none; forbidden: none
- **aifs-overfitting: PASS** (1.29s) — missing: none; forbidden: none
- **gift-specific-evidence: PASS** (0.98s) — missing: none; forbidden: none
- **gift-live-claims: PASS** (1.01s) — missing: none; forbidden: none
- **qc-born-rule: PASS** (0.87s) — missing: none; forbidden: none
- **qc-no-signaling: PASS** (1.01s) — missing: none; forbidden: none
