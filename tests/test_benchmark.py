import json

import httpx
import pytest
from pathlib import Path

from app_engine.benchmark import (
    BenchmarkCandidate, BenchmarkCase, BenchmarkResult, CaseResult,
    InvalidSuiteError, compare_results, evaluate_response, load_suite,
    render_markdown, run_candidate, run_case,
)
from app_engine.grounding import KnowledgeBase


def case(**overrides):
    values = dict(id="vim-ciw", app_id="vim-dojo", category="fact", critical=True,
                  system_prompt="Vim tutor", question="What does ciw do?",
                  required_all=("change", "inner", "word"),
                  required_any=(("ciw", "c i w"),),
                  forbidden=("not a standard", "isn't a standard"),
                  max_seconds=10.0, review_note="Must identify the standard command.")
    values.update(overrides)
    return BenchmarkCase(**values)


def result(model, passed, critical=True, elapsed=1.0, transport_error=None):
    c = case(id=f"{model}-{elapsed}", critical=critical)
    return CaseResult(c.id, c.app_id, c.critical, "answer", passed, (), (),
                      elapsed, 0.2, 6, transport_error)


def test_evaluate_response_checks_required_and_forbidden_phrases():
    passed = evaluate_response(case(), "CIW means change inner word in Vim.", 1.0, 0.2)
    assert passed.factual_pass is True
    failed = evaluate_response(case(), "ciw isn't a standard Vim command", 1.0, 0.2)
    assert failed.factual_pass is False
    assert "change" in failed.missing_requirements
    assert "isn't a standard" in failed.forbidden_matches


def test_load_suite_rejects_duplicate_ids(tmp_path):
    item = {"id":"x","app_id":"a","category":"fact","critical":True,
            "system_prompt":"s","question":"q","required_all":["a"],
            "required_any":[],"forbidden":[],"max_seconds":5,"review_note":"r"}
    path = tmp_path / "cases.json"; path.write_text(json.dumps({"version":1,"cases":[item,item]}))
    with pytest.raises(InvalidSuiteError):
        load_suite(path)


def test_comparison_disqualifies_critical_failure():
    a = BenchmarkResult(BenchmarkCandidate("a","a:1",1,"apache-2.0","u"), (result("a",False),))
    b = BenchmarkResult(BenchmarkCandidate("b","b:1",2,"mit","u"), (result("b",True),))
    comparison = compare_results((a,b))
    assert comparison.winner.candidate_id == "b"
    assert comparison.rows[0].eligible is False


def test_comparison_uses_latency_when_pass_rates_are_within_five_points():
    slow = BenchmarkResult(BenchmarkCandidate("slow","s",1,"apache-2.0","u"),
                           (result("slow",True,elapsed=4.0), result("slow",True,elapsed=5.0)))
    fast = BenchmarkResult(BenchmarkCandidate("fast","f",2,"mit","u"),
                           (result("fast",True,elapsed=1.0), result("fast",True,elapsed=2.0)))
    assert compare_results((slow,fast)).winner.candidate_id == "fast"


def test_transport_failure_over_five_percent_is_ineligible():
    cases = tuple(result("bad", True, critical=False, elapsed=i) for i in range(18)) + tuple(
        result("bad",False,critical=False,elapsed=i,transport_error="timeout") for i in (19,20))
    row = compare_results((BenchmarkResult(BenchmarkCandidate("bad","b",1,"mit","u"),cases),)).rows[0]
    assert row.eligible is False


def test_markdown_report_is_deterministic():
    benchmark = BenchmarkResult(BenchmarkCandidate("a","a:1",123,"apache-2.0","https://license"),
                                (result("a",True),))
    report = render_markdown(compare_results((benchmark,)), {"ollama_version":"1", "ram_bytes":16})
    assert "| a | Yes | 100.0% |" in report
    assert "https://license" in report
    assert "Winner: `a`" in report


@pytest.mark.asyncio
async def test_run_case_streams_ollama_response_and_records_first_token():
    body = b'{"message":{"content":"CIW means change "},"done":false}\n' \
           b'{"message":{"content":"inner word in Vim."},"done":true}\n'
    def handler(request):
        payload = json.loads(request.content)
        assert payload["options"]["num_ctx"] == 4096
        assert payload["options"]["num_predict"] == 384
        return httpx.Response(200, content=body)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:11434",
    )
    ticks = iter((10.0, 10.25, 11.0))
    measured = await run_case(client, "qwen3:4b", case(), clock=lambda: next(ticks))
    await client.aclose()
    assert measured.factual_pass is True
    assert measured.response == "CIW means change inner word in Vim."
    assert measured.first_token_seconds == pytest.approx(0.25)
    assert measured.elapsed_seconds == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_run_candidate_continues_after_transport_error():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, content=b'{"message":{"content":"change inner word ciw"},"done":true}\n')
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://local")
    candidate = BenchmarkCandidate("qwen3-4b", "qwen3:4b", 1, "Apache-2.0", "u")
    benchmark = await run_candidate(client, candidate, (case(id="one"), case(id="two")))
    await client.aclose()
    assert len(benchmark.cases) == 2
    assert benchmark.cases[0].transport_error == "timeout"
    assert benchmark.cases[1].factual_pass is True


@pytest.mark.asyncio
async def test_run_case_uses_grounding_and_corrective_retry():
    payloads = []
    def handler(request):
        payloads.append(json.loads(request.content))
        answer = "ciw is not a standard command" if len(payloads) == 1 else "ciw means change inner word anywhere in the word"
        return httpx.Response(200, content=(json.dumps({"message":{"content":answer},"done":True})+"\n").encode())
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://local")
    knowledge = KnowledgeBase.load(Path("knowledge/tutors.json"))
    measured = await run_case(client, "qwen3:4b", case(), knowledge=knowledge)
    await client.aclose()
    assert "REVIEWED LOCAL REFERENCE" in payloads[0]["messages"][0]["content"]
    assert len(payloads) == 2
    assert measured.factual_pass is True
