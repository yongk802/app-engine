from pathlib import Path

import pytest

from app_engine.grounding import KnowledgeBase, load_declared_knowledge
from tests.catalog import personal_apps_dir


def test_every_chat_app_has_two_reviewed_knowledge_entries():
    apps_dir = personal_apps_dir()
    if not apps_dir:
        pytest.skip("cross-repo knowledge validation needs PERSONAL_APPS_DIR or a sibling personal-apps checkout")
    import json
    for manifest_path in apps_dir.glob("*/app.json"):
        data = json.loads(manifest_path.read_text())
        if not data.get("chat_enabled"):
            continue
        declared = data.get("chat_knowledge")
        assert declared, f"{manifest_path.parent.name} must declare chat_knowledge"
        knowledge = load_declared_knowledge(manifest_path.parent, declared)
        app_id = data.get("id", manifest_path.parent.name)
        entries = [entry for entry in knowledge.entries if entry.app_id == app_id]
        assert len(entries) >= 2, f"{app_id} needs at least two reviewed knowledge entries"
        assert all(entry.source_note.strip() for entry in entries)
