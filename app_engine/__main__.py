"""Command-line entry points for the standalone app-engine distribution."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _engine_paths() -> tuple[Path, Path]:
    """Return the engine source and the directory containing its data files.

    ``engine.py`` historically resolves launcher assets beside its own file.
    Setuptools installs ``py-modules`` into ``site-packages`` but installs
    ``data-files`` below the environment prefix, so the two directories differ
    in a normal virtualenv.  Keep the source file untouched and execute it
    with a legacy-compatible ``__file__`` rooted at the installed data files.
    """

    spec = importlib.util.find_spec("engine")
    if spec is None or not spec.origin or spec.origin in {"built-in", "frozen"}:
        raise RuntimeError("the packaged engine.py module could not be located")
    source = Path(spec.origin).resolve()
    candidates = (source.parent, Path(sys.prefix), Path(__file__).resolve().parents[1])
    for candidate in candidates:
        if (candidate / "launcher.html").is_file() and (candidate / "model-registry.json").is_file():
            return source, candidate
    # Preserve the original error location for source checkouts and provide a
    # useful path when a package was installed without its data files.
    return source, source.parent


def main() -> None:
    """Run the legacy top-level engine with its existing CLI semantics.

    The import is intentionally deferred so ``import app_engine`` and imports
    of the contract surface stay free of Ollama/model-file initialization.
    """

    source, data_root = _engine_paths()
    namespace = {
        "__name__": "__main__",
        "__file__": str(data_root / "engine.py"),
        "__package__": None,
        "__cached__": None,
    }
    exec(compile(source.read_bytes(), str(source), "exec"), namespace)


def manifest_main() -> int:
    """Validate one or more app directories using the manifest CLI."""

    from .manifest import _cli

    return _cli(sys.argv[1:])


if __name__ == "__main__":
    main()
