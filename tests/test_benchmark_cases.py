import json
from collections import Counter
from pathlib import Path

from app_engine.benchmark import load_suite


def test_suite_covers_every_chat_enabled_personal_app_twice():
    apps_dir = Path("/Users/yongkim/git/personal-apps")
    chat_apps = set()
    for manifest in apps_dir.glob("*/app.json"):
        data = json.loads(manifest.read_text())
        if data.get("chat_enabled"):
            chat_apps.add(data["id"])
    suite = load_suite(Path("benchmarks/tutor-cases.json"))
    counts = Counter(case.app_id for case in suite.cases)
    critical = {case.app_id for case in suite.cases if case.critical}
    assert set(counts) == chat_apps
    assert all(counts[app_id] >= 2 for app_id in chat_apps)
    assert critical == chat_apps
    assert all(case.required_all or case.required_any for case in suite.cases)
