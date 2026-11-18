"""Tests that execute harmless fixtures explicitly select trusted host mode."""
import pytest


@pytest.fixture(autouse=True)
def trusted_fixture_processes(monkeypatch):
    monkeypatch.setenv('APP_ENGINE_PROCESS_ISOLATION', 'legacy')
