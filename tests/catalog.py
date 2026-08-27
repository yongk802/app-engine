from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def personal_apps_dir() -> Path | None:
    configured = os.environ.get("PERSONAL_APPS_DIR")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.append(ROOT.parent / "personal-apps")
    return next((path.resolve() for path in candidates if path.is_dir()), None)


def expected_chat_apps() -> set[str]:
    apps_dir = personal_apps_dir()
    if apps_dir:
        result = set()
        for path in apps_dir.glob("*/app.json"):
            data = json.loads(path.read_text())
            if data.get("chat_enabled"):
                result.add(data.get("id", path.parent.name))
        return result
    fixture = json.loads((ROOT / "tests" / "fixtures" / "chat-apps.json").read_text())
    return set(fixture["apps"])
