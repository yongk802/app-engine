import json
from collections import Counter
from pathlib import Path

from app_engine.grounding import KnowledgeBase


APPS = Path("/Users/yongkim/git/personal-apps")


def test_every_chat_app_has_two_reviewed_knowledge_entries():
    chat_apps = set()
    for manifest in APPS.glob("*/app.json"):
        data = json.loads(manifest.read_text())
        if data.get("chat_enabled"):
            chat_apps.add(data.get("id", manifest.parent.name))
    knowledge = KnowledgeBase.load(Path("knowledge/tutors.json"))
    counts = Counter(entry.app_id for entry in knowledge.entries)
    assert set(counts) == chat_apps
    assert all(counts[app_id] >= 2 for app_id in chat_apps)
    assert all(entry.source_note.strip() for entry in knowledge.entries)
