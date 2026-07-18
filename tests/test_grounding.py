import json

from app_engine.grounding import (
    KnowledgeBase, build_grounding_context, detect_misconceptions,
)


def write_pack(tmp_path):
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"version": 1, "entries": [
        {"id":"vim-ciw","app_id":"vim-dojo","title":"Change inner word",
         "keywords":["vim","ciw","word"],
         "fact":"ciw changes the inner word and enters Insert mode.",
         "source_note":"Vim help: iword",
         "misconceptions":[{"pattern":"not (?:a )?standard","correction":"ciw is a standard Vim text-object command."}]},
        {"id":"vim-dd","app_id":"vim-dojo","title":"Delete line",
         "keywords":["vim","dd","line"], "fact":"dd deletes the current line.",
         "source_note":"Vim help: dd", "misconceptions":[]},
        {"id":"linux-kill","app_id":"linux-ops-academy","title":"Signals",
         "keywords":["sigkill"], "fact":"SIGKILL cannot be caught.",
         "source_note":"signal(7)", "misconceptions":[]}
    ]}))
    return path


def test_retrieve_is_app_scoped_and_ranked_by_keyword_overlap(tmp_path):
    knowledge = KnowledgeBase.load(write_pack(tmp_path))
    entries = knowledge.retrieve("vim-dojo", "In Vim, what does ciw do to a word?")
    assert [entry.id for entry in entries] == ["vim-ciw", "vim-dd"]
    assert all(entry.app_id == "vim-dojo" for entry in entries)


def test_empty_match_returns_no_context_instead_of_unrelated_facts(tmp_path):
    knowledge = KnowledgeBase.load(write_pack(tmp_path))
    assert knowledge.retrieve("vim-dojo", "How do cameras expose film?") == ()


def test_context_labels_reviewed_facts_as_authoritative(tmp_path):
    entry = KnowledgeBase.load(write_pack(tmp_path)).retrieve("vim-dojo", "ciw word", limit=1)[0]
    context = build_grounding_context((entry,))
    assert "REVIEWED LOCAL REFERENCE" in context
    assert "ciw changes the inner word" in context
    assert "Vim help: iword" in context


def test_detects_only_configured_misconceptions(tmp_path):
    entry = KnowledgeBase.load(write_pack(tmp_path)).retrieve("vim-dojo", "ciw", limit=1)[0]
    found = detect_misconceptions("ciw is not a standard command.", (entry,))
    assert [item.entry_id for item in found] == ["vim-ciw"]
    assert detect_misconceptions("ciw is a standard command.", (entry,)) == ()
