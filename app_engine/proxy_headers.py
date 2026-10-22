"""Remove host credentials before passing a browser request to an app backend."""

from __future__ import annotations

from collections.abc import Iterable

from .public_origin import SESSION_COOKIE


_ENGINE_HEADERS = frozenset({"x-app-engine-admin", "x-app-state-token"})


def forward_app_headers(headers: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    forwarded: list[tuple[str, str]] = []
    for key, value in headers:
        if key.lower() in _ENGINE_HEADERS:
            continue
        if key.lower() == "cookie":
            cookies = [item.strip() for item in value.split(";")
                       if item.strip() and item.split("=", 1)[0].strip() != SESSION_COOKIE]
            if cookies:
                forwarded.append((key, "; ".join(cookies)))
            continue
        forwarded.append((key, value))
    return tuple(forwarded)
