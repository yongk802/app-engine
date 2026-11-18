"""Disposable, deadline-bound regex evaluator. Only JSON crosses this boundary."""
from __future__ import annotations

import json
import re
import sys


def main() -> int:
    # Set limits here, never preexec_fn in the multithreaded engine.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
        if sys.platform.startswith('linux'):
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    except ImportError:  # Windows still has a parent-enforced wall deadline.
        pass
    value = json.loads(sys.stdin.buffer.read(512 * 1024 + 1))
    patterns, answer = value['patterns'], value['answer']
    if (not isinstance(patterns, list) or len(patterns) > 64
            or any(not isinstance(p, str) or len(p) > 1024 for p in patterns)
            or (answer is not None and (not isinstance(answer, str) or len(answer) > 131072))):
        return 64
    result = []
    for pattern in patterns:
        compiled = re.compile(pattern, re.IGNORECASE)
        match = compiled.search(answer) if answer is not None else None
        result.append(match.group(0)[:4096] if match else None)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, TypeError, KeyError, re.error, MemoryError, OSError):
        raise SystemExit(64) from None
