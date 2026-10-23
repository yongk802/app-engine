from app_engine.process_isolation import IsolationMode, ProcessIsolationPolicy


def test_audit_mode_can_enforce_one_pilot_without_changing_other_apps():
    policy = ProcessIsolationPolicy(IsolationMode.AUDIT,
                                    frozenset({"cyberpunk-tcg"}))
    assert policy.decide("start", "cyberpunk-tcg").execution_class == "isolated_unit"
    other = policy.decide("start", "package-tracker")
    assert other.mode is IsolationMode.AUDIT
    assert other.execution_class == "host_process"


def test_pilot_list_from_environment_is_strict(monkeypatch):
    monkeypatch.setenv("APP_ENGINE_PROCESS_ISOLATION", "audit")
    monkeypatch.setenv("APP_ENGINE_ISOLATED_APP_IDS", "cyberpunk-tcg")
    policy = ProcessIsolationPolicy.from_environment()
    assert policy.decide("start", "cyberpunk-tcg").mode is IsolationMode.ENFORCE
    monkeypatch.setenv("APP_ENGINE_ISOLATED_APP_IDS", "../other")
    try:
        ProcessIsolationPolicy.from_environment()
    except ValueError as exc:
        assert "APP_ENGINE_ISOLATED_APP_IDS" in str(exc)
    else:
        raise AssertionError("invalid pilot ID accepted")
