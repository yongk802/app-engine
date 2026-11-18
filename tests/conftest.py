"""Tests that execute harmless fixtures explicitly select trusted host mode."""
import os

import pytest

# The test client addresses the engine as ``testserver``; only the suite may call that a local name.
os.environ["APP_ENGINE_TESTING"] = "1"


@pytest.fixture(autouse=True)
def trusted_fixture_processes(monkeypatch):
    monkeypatch.setenv('APP_ENGINE_PROCESS_ISOLATION', 'legacy')
