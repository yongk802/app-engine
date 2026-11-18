"""Fixed command executor inside one transient app unit."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from app_engine.network_guard import restrict_network

_MAX_PLAN_BYTES = 256 * 1024
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class CommandPlan:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    bind_ports: tuple[int, ...] = ()
    connect_ports: tuple[int, ...] = ()


def decode_plan(raw: bytes, *, roots: tuple[Path, ...]) -> CommandPlan:
    if len(raw) > _MAX_PLAN_BYTES:
        raise ValueError("command plan too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid command plan") from exc
    if (not isinstance(value, dict)
            or not {"version", "argv", "cwd", "env"} <= set(value)
            or set(value) - {"version", "argv", "cwd", "env", "network"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("invalid command plan shape")

    argv = value["argv"]
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or any(not isinstance(arg, str) or not arg or len(arg) > 4096
                   or "\x00" in arg for arg in argv)
            or not Path(argv[0]).is_absolute()):
        raise ValueError("invalid command arguments")

    cwd = value["cwd"]
    if not isinstance(cwd, str) or len(cwd) > 4096 or "\x00" in cwd:
        raise ValueError("invalid working directory")
    path = Path(cwd)
    if not path.is_absolute():
        raise ValueError("invalid working directory")
    try:
        resolved = path.resolve(strict=True)
        allowed = tuple(root.resolve(strict=True) for root in roots if root.exists())
    except OSError as exc:
        raise ValueError("invalid working directory") from exc
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
        raise ValueError("invalid working directory")

    env = value["env"]
    if (not isinstance(env, dict) or len(env) > 128
            or any(not isinstance(key, str) or not _ENV_NAME.fullmatch(key)
                   or not isinstance(item, str) or len(item) > 16384
                   or "\x00" in item for key, item in env.items())):
        raise ValueError("invalid command environment")
    network = value.get("network", {"bind": [], "connect": []})
    if (not isinstance(network, dict) or set(network) != {"bind", "connect"}
            or any(not isinstance(network[key], list) or len(network[key]) > 32
                   or any(type(port) is not int or not 0 <= port <= 65535 for port in network[key])
                   for key in network) or 0 in network['connect']):
        raise ValueError("invalid command network policy")
    return CommandPlan(tuple(argv), resolved, env, tuple(network['bind']), tuple(network['connect']))


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(_MAX_PLAN_BYTES + 1)
        plan = decode_plan(raw, roots=(Path("/workspace"), Path("/app-data"),
                                       Path("/app-build")))
        restrict_network(plan.bind_ports, plan.connect_ports)
        os.chdir(plan.cwd)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
    except (OSError, ValueError):
        print("Invalid isolated command plan.", file=sys.stderr)
        return 64
    try:
        os.execve(plan.argv[0], plan.argv, plan.env)
    except OSError:
        print("Isolated command could not start.", file=sys.stderr)
        return 127
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
