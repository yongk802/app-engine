from collections import Counter
from pathlib import Path

from app_engine.benchmark import load_suite
from tests.catalog import expected_chat_apps


def test_suite_covers_every_chat_enabled_personal_app_twice():
    chat_apps = expected_chat_apps()
    suite = load_suite(Path("benchmarks/tutor-cases.json"))
    counts = Counter(case.app_id for case in suite.cases)
    critical = {case.app_id for case in suite.cases if case.critical}
    assert set(counts) == chat_apps
    assert all(counts[app_id] >= 2 for app_id in chat_apps)
    assert critical == chat_apps
    assert all(case.required_all or case.required_any for case in suite.cases)
