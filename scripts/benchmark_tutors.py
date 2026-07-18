#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_engine.benchmark import (  # noqa: E402
    BenchmarkCandidate, compare_results, load_suite, render_markdown, run_candidate,
)
from app_engine.grounding import KnowledgeBase  # noqa: E402


CANDIDATES = (
    BenchmarkCandidate("qwen3-1.7b", "qwen3:1.7b", 1_400_000_000, "Apache-2.0",
                       "https://huggingface.co/Qwen/Qwen3-1.7B/blob/main/LICENSE"),
    BenchmarkCandidate("qwen3-4b", "qwen3:4b", 2_500_000_000, "Apache-2.0",
                       "https://huggingface.co/Qwen/Qwen3-4B/blob/main/LICENSE"),
    BenchmarkCandidate("phi4-mini", "phi4-mini", 2_500_000_000, "MIT",
                       "https://huggingface.co/microsoft/Phi-4-mini-instruct/blob/main/LICENSE"),
)


def ram_bytes() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        try:
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            return None


def ollama_version() -> str:
    try:
        return subprocess.check_output(["ollama", "--version"], text=True, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark strictly local tutor models through Ollama.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--cases", type=Path, default=ROOT / "benchmarks" / "tutor-cases.json")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks" / "results" / datetime.now().date().isoformat())
    parser.add_argument("--candidate", action="append", choices=[c.candidate_id for c in CANDIDATES])
    args = parser.parse_args()

    selected = tuple(c for c in CANDIDATES if not args.candidate or c.candidate_id in args.candidate)
    suite = load_suite(args.cases)
    knowledge = KnowledgeBase.load(ROOT / "knowledge" / "tutors.json")
    results = []
    async with httpx.AsyncClient(base_url=args.endpoint) as client:
        for candidate in selected:
            print(f"Running {candidate.candidate_id} ({len(suite.cases)} cases)...", flush=True)
            results.append(await run_candidate(client, candidate, suite.cases, knowledge=knowledge))
    comparison = compare_results(tuple(results))
    metadata = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "ram_bytes": ram_bytes(),
        "ollama_version": ollama_version(),
        "endpoint": args.endpoint,
        "suite_version": suite.version,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(json.dumps({
        "metadata": metadata,
        "comparison": asdict(comparison),
    }, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(render_markdown(comparison, metadata))
    print(f"Winner: {comparison.winner.candidate_id if comparison.winner else 'none'}")
    print(f"Wrote {args.output.with_suffix('.json')} and {args.output.with_suffix('.md')}")
    return 0 if comparison.winner else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
