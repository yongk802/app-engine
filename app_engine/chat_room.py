"""One text channel per public-origin engine: the launcher's chat dock.

Every signed-in person on the server (owner or player, in a browser here or from
their own app-engine through the bearer API) shares it. Messages are bounded and
metered per sender, the last few hundred are kept in ``chat.json`` under the state
directory, and presence is whoever polled recently. Nothing here reaches the room
service: a table's chat stays with the table.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

MESSAGE_LIMIT = 500          # characters
KEEP = 300                   # messages kept
ONLINE_WINDOW = 45.0         # seconds since last poll to count as here
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ChatRoom:
    def __init__(self, state_dir: Path, per_minute: int = 30):
        self.path = Path(state_dir) / "chat.json"
        self.per_minute = per_minute
        self.messages: list[dict] = []
        self.seen: dict[str, tuple[float, str, str]] = {}    # subject -> (last poll, name, role)
        self._sent: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and isinstance(saved.get("messages"), list):
                self.messages = [m for m in saved["messages"] if isinstance(m, dict) and isinstance(m.get("id"), int)][-KEEP:]
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError, OSError):
            self.messages = []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".chat-{secrets.token_hex(6)}.tmp")
            with open(temporary, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
                json.dump({"version": 1, "messages": self.messages}, handle)
            os.replace(temporary, self.path)
        except OSError:
            pass

    def touch(self, subject_id: str, name: str, role: str) -> None:
        self.seen[subject_id] = (time.time(), name, role)

    def online(self) -> list[dict]:
        now = time.time()
        self.seen = {k: v for k, v in self.seen.items() if now - v[0] < ONLINE_WINDOW}
        return sorted(({"id": k, "name": v[1], "role": v[2]} for k, v in self.seen.items()), key=lambda p: p["name"].lower())

    def since(self, after: int) -> list[dict]:
        return [m for m in self.messages if m["id"] > after][-100:]

    def post(self, subject_id: str, name: str, role: str, text: object) -> dict:
        """Append a message; raises ValueError for bad input, PermissionError when metered."""
        if not isinstance(text, str):
            raise ValueError("a message is text")
        clean = _CONTROL.sub("", text).strip()
        if not clean:
            raise ValueError("say something")
        if len(clean) > MESSAGE_LIMIT:
            raise ValueError(f"a message has at most {MESSAGE_LIMIT} characters")
        now = time.time()
        recent = [t for t in self._sent.get(subject_id, []) if now - t < 60]
        if len(recent) >= self.per_minute or (recent and now - recent[-1] < 1.0):
            raise PermissionError("slow down")
        recent.append(now)
        self._sent[subject_id] = recent
        message = {"id": (self.messages[-1]["id"] + 1) if self.messages else 1, "at": int(now), "from": {"id": subject_id, "name": name, "role": role}, "text": clean}
        self.messages = (self.messages + [message])[-KEEP:]
        self._save()
        self.touch(subject_id, name, role)
        return message
