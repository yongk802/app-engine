from app_engine.system_probe import SystemProbe


def test_probe_reports_injected_host_capabilities(tmp_path):
    probe = SystemProbe(
        os_name=lambda: "Darwin", architecture=lambda: "arm64",
        ram_bytes=lambda: 16 * 1024**3, free_disk_bytes=lambda _: 50 * 1024**3,
        which=lambda name: "/usr/local/bin/ollama" if name == "ollama" else None,
        ollama_check=lambda endpoint: True,
    )
    result = probe.inspect(tmp_path, "http://127.0.0.1:11434")
    assert result.os == "macos"
    assert result.acceleration == "metal"
    assert result.ollama_service_state == "running"
    assert result.ollama_executable == "/usr/local/bin/ollama"


def test_probe_distinguishes_installed_but_stopped(tmp_path):
    probe = SystemProbe(
        os_name=lambda: "Windows", architecture=lambda: "AMD64",
        ram_bytes=lambda: 8, free_disk_bytes=lambda _: 9,
        which=lambda _: "C:/Ollama/ollama.exe", ollama_check=lambda _: False,
    )
    assert probe.inspect(tmp_path, "http://127.0.0.1:11434").ollama_service_state == "stopped"


def test_probe_reports_missing_runtime(tmp_path):
    probe = SystemProbe(
        os_name=lambda: "Linux", architecture=lambda: "x86_64",
        ram_bytes=lambda: 8, free_disk_bytes=lambda _: 9,
        which=lambda _: None, ollama_check=lambda _: False,
    )
    result = probe.inspect(tmp_path, "http://127.0.0.1:11434")
    assert result.os == "linux"
    assert result.ollama_service_state == "missing"
    assert result.installation_method == "official"


def test_probe_uses_existing_ancestor_for_fresh_state_directory(tmp_path):
    seen = []
    probe = SystemProbe(
        os_name=lambda: "Linux", architecture=lambda: "x86_64",
        ram_bytes=lambda: 8, free_disk_bytes=lambda path: seen.append(path) or 9,
        which=lambda _: None, ollama_check=lambda _: False,
    )
    probe.inspect(tmp_path / "not-created" / "app-state", "http://127.0.0.1:11434")
    assert seen == [tmp_path]
