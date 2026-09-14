"""Packaging and public-import contract tests for app-engine."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _version() -> str:
    """The package version as declared in pyproject.toml (source of truth)."""
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _build_wheel(destination: Path) -> Path:
    """Build a wheel without allowing a build to reach a package index."""

    destination.mkdir()
    result = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--offline",
            "--out-dir",
            str(destination),
        ],
        cwd=ROOT,
        env={**os.environ, "UV_OFFLINE": "1"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = sorted(destination.glob("*.whl"))
    assert len(wheels) == 1, sorted(p.name for p in destination.iterdir())
    return wheels[0]


def test_package_exposes_version_runtime_and_contract_exports():
    import app_engine
    from app_engine import (
        AppCatalog,
        AppEngineRuntime,
        AppId,
        AppManifest,
        CommandStep,
        ConfigurationField,
        ManifestInspection,
        ProcessScope,
        RuntimeState,
        TargetSpec,
    )

    assert isinstance(app_engine.__version__, str)
    assert app_engine.__version__.strip()
    assert AppEngineRuntime is app_engine.AppEngineRuntime
    assert AppId and AppManifest and AppCatalog and ManifestInspection
    assert CommandStep and ConfigurationField and TargetSpec
    assert ProcessScope.SHARED.value == "shared"
    assert RuntimeState.READY.value == "ready"


def test_wheel_contains_importable_modules_and_standalone_assets(tmp_path):
    wheel = _build_wheel(tmp_path / "dist")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert any(name.endswith("app_engine/__init__.py") for name in names)
    assert any(name.endswith("app_engine/contracts.py") for name in names)
    assert any(name.endswith("app_engine/manifest.py") for name in names)
    assert any(name.endswith("studio/index.html") for name in names)
    assert any("templates/" in name for name in names)

    install_dir = tmp_path / "installed"
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheel),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app_engine; "
                "assert app_engine.__version__; "
                "assert hasattr(app_engine, 'AppEngineRuntime'); "
                "from app_engine import AppManifest, ManifestInspection"
            ),
        ],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(install_dir)},
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr


def test_clean_venv_install_resolves_assets_and_console_manifest_entrypoint(tmp_path):
    """A normal venv install keeps legacy engine assets beside its data root."""

    wheel = _build_wheel(tmp_path / "dist")
    venv_dir = tmp_path / "clean-venv"
    venv_result = subprocess.run(
        ["uv", "venv", "--seed", str(venv_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert venv_result.returncode == 0, venv_result.stdout + venv_result.stderr
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert python.is_file()

    install = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    outside = tmp_path / "unrelated-cwd"
    outside.mkdir()
    app_root = tmp_path / "sample-app"
    app_root.mkdir()
    (app_root / "app.json").write_text('{"id":"sample-app","label":"Sample","icon":"🧪"}')
    (app_root / "index.html").write_text("<h1>Sample</h1>")
    v2_root = tmp_path / "sample-v2"
    v2_root.mkdir()
    (v2_root / "app.json").write_text(
        '{"manifest_version":2,"id":"sample-v2","label":"Sample v2",'
        '"icon":"🧪","targets":{"api":{"kind":"web",'
        '"runtime":{"driver":"process","start":{"argv":["app"]}}}}}'
    )

    probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import sys; from pathlib import Path; "
                "import app_engine; "
                "from app_engine.__main__ import _engine_paths; "
                "source, data = _engine_paths(); "
                "assert source.name == 'engine.py'; "
                "assert data == Path(sys.prefix); "
                "assert all((data / name).is_file() for name in "
                "('launcher.html', 'app.schema.json', 'model-registry.json')); "
                f"assert (data / 'knowledge' / 'tutors.json').is_file(); "
                f"assert app_engine.__version__ == '{_version()}'"
            ),
        ],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr

    cli = venv_dir / ("Scripts/app-engine-manifest.exe" if os.name == "nt" else "bin/app-engine-manifest")
    cli_result = subprocess.run(
        [str(cli), str(app_root), str(v2_root)],
        cwd=outside,
        capture_output=True,
        text=True,
    )
    assert cli_result.returncode == 0, cli_result.stdout + cli_result.stderr
    assert "sample-app" in cli_result.stdout
    assert "sample-v2" in cli_result.stdout
    assert "valid" in cli_result.stdout
