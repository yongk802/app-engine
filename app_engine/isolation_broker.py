"""Root-side parsing for requests to the isolated command runner.

Only the fixed isolated executor is started on the host. App-supplied command
arguments travel on its standard input after systemd applies the unit policy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .isolated_units import UnitRequest
from .isolation_wire import MAX_REQUEST_BYTES

_REQUEST_FIELDS = {"version", "app_id", "phase", "app_root", "data_dir",
                   "build_dir", "plan"}
_SAFE_APP_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}\Z")


@dataclass(frozen=True)
class BrokerRequest:
    unit: UnitRequest


def decode_request(raw: bytes) -> BrokerRequest:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("invalid broker request size")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid broker request JSON") from exc
    if (not isinstance(value, dict) or set(value) != _REQUEST_FIELDS
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["app_id"], str)
            or not isinstance(value["phase"], str)
            or not isinstance(value["plan"], dict)):
        raise ValueError("invalid broker request shape")
    paths = []
    for key in ("app_root", "data_dir", "build_dir"):
        supplied = value[key]
        if (not isinstance(supplied, str) or not supplied.startswith("/")
                or len(supplied) > 4096 or any(char in supplied for char in (":", "\n", "\r", "\x00"))):
            raise ValueError("invalid broker scope path")
        paths.append(Path(supplied))
    if value["phase"] not in {"install", "build", "start"}:
        raise ValueError("invalid broker phase")
    if not _SAFE_APP_ID.fullmatch(value["app_id"]):
        raise ValueError("invalid broker app ID")
    try:
        plan = json.dumps(value["plan"], separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid broker command plan") from exc
    if len(plan.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("broker command plan too large")
    return BrokerRequest(UnitRequest(
        app_id=value["app_id"], phase=value["phase"], app_root=paths[0],
        data_dir=paths[1], build_dir=paths[2], payload=plan,
    ))
