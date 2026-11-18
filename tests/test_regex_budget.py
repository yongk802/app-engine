"""Real hostile patterns run only in disposable workers, never the test process."""
import asyncio
import time
from dataclasses import replace

import pytest

from app_engine.grounding import (
    InvalidKnowledgePackError, KnowledgeBase, MisconceptionRule,
    detect_misconceptions, detect_misconceptions_async,
)
from test_grounding import write_pack


@pytest.mark.asyncio
async def test_pathological_pattern_has_deadline_and_event_loop_remains_responsive(tmp_path):
    entry = KnowledgeBase.load(write_pack(tmp_path)).entries[0]
    entry = replace(entry, misconceptions=(MisconceptionRule('(a+)+$', 'test'),))
    ticks = 0
    async def heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(.02)
    heartbeat_task = asyncio.create_task(heartbeat())
    started = time.monotonic()
    try:
        with pytest.raises(InvalidKnowledgePackError):
            await detect_misconceptions_async('a' * 10000 + '!', (entry,))
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
    assert time.monotonic() - started < 4
    assert ticks > 10
    # A timed out worker must release its slot and not poison subsequent checks.
    ordinary = KnowledgeBase.load(write_pack(tmp_path)).entries[0]
    assert detect_misconceptions('not a standard command', (ordinary,))[0].entry_id == ordinary.id


def test_invalid_pattern_and_oversized_pattern_are_rejected_in_worker(tmp_path):
    import json
    path = write_pack(tmp_path)
    raw = json.loads(path.read_text())
    for pattern in ('(', 'a' * 1025):
        raw['entries'][0]['misconceptions'][0]['pattern'] = pattern
        path.write_text(json.dumps(raw))
        with pytest.raises(InvalidKnowledgePackError):
            KnowledgeBase.load(path)


def test_regex_input_and_concurrency_budget(tmp_path):
    import app_engine.grounding as grounding
    entry = KnowledgeBase.load(write_pack(tmp_path)).entries[0]
    with pytest.raises(InvalidKnowledgePackError, match='answer budget'):
        detect_misconceptions('a' * 131073, (entry,))
    assert grounding._regex_slots.acquire(False)
    assert grounding._regex_slots.acquire(False)
    try:
        with pytest.raises(InvalidKnowledgePackError, match='busy'):
            detect_misconceptions('test', (entry,))
    finally:
        grounding._regex_slots.release()
        grounding._regex_slots.release()
