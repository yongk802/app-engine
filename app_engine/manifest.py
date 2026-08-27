"""app.json manifest contract — schema, validation, and version compatibility.

This is the single source of truth for the **shared** app manifest that both
app-engine and Atrium honor. Keeping it here (importable, dependency-free) lets
the engine surface useful errors for malformed apps instead of silently
dropping them, and lets a `python -m app_engine.manifest <dir>` CLI validate an
app before it ships.

Backward compatible: every field except id/label/icon (and an entry point) is
optional, and unknown fields are tolerated (with a soft warning) so older and
Atrium-specific manifests keep working.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Bump when the host<->app contract changes in a way apps can depend on.
ENGINE_VERSION = "1.0.0"

APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SEMVER_RE = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")

# Capabilities an app may request via ``permissions``. These are browser
# Permissions-Policy features, each mapping 1:1 to a token in the iframe's
# ``allow`` attribute; the browser then handles runtime consent (the mic/camera
# prompt) at point of use. Kept to a conservative allowlist so a manifest can't
# inject arbitrary policy tokens. Powerful features only work when the app's
# sandbox includes allow-same-origin (an opaque origin can't be granted them).
PERMISSION_FEATURES = frozenset({
    "microphone", "camera", "display-capture", "geolocation",
    "midi", "clipboard-read", "clipboard-write", "fullscreen", "autoplay",
})

# Fields the shared manifest contract recognizes. Atrium-only fields are listed
# so the shared validator doesn't warn about them (keeps apps portable).
KNOWN_FIELDS = frozenset({
    # identity & listing metadata
    "id", "label", "icon", "version", "description", "categories", "author", "screenshots",
    # runtime
    "sandbox", "entry_point", "min_engine_version", "permissions",
    # local-AI tutor
    "chat_enabled", "chat_system_prompt", "chat_knowledge",
    # Atrium-only (recognized here so the shared contract stays warning-free)
    "secret", "allow", "proxy_read_timeout", "launch_mode", "multi",
})


def parse_version(v: str) -> tuple[int, int, int]:
    """Parse a semver-ish string into a comparable (major, minor, patch) tuple.

    Pre-release/build metadata is ignored for ordering. Returns (0,0,0) on junk.
    """
    if not isinstance(v, str):
        return (0, 0, 0)
    core = re.split(r"[-+]", v.strip(), 1)[0]
    parts = core.split(".")
    nums: list[int] = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def version_ge(a: str, b: str) -> bool:
    """True if version a >= version b (by major.minor.patch)."""
    return parse_version(a) >= parse_version(b)


def is_compatible(min_engine_version: str, engine_version: str = ENGINE_VERSION) -> bool:
    """True if an app declaring `min_engine_version` can run on this engine."""
    if not min_engine_version:
        return True
    return version_ge(engine_version, min_engine_version)


def _is_relative_asset(p: str) -> bool:
    """A screenshot/asset path must stay inside the app dir."""
    if not isinstance(p, str) or not p:
        return False
    if p.startswith("/") or p.startswith("\\") or ".." in Path(p).parts:
        return False
    return True


def validate_manifest(m: dict, *, has_index: bool, dir_name: str = "") -> tuple[list[str], list[str]]:
    """Validate a parsed app.json.

    Returns (errors, warnings). A non-empty `errors` list means the app is not
    loadable and should be surfaced as rejected. `warnings` are non-fatal
    (missing version, unknown keys, …) and can be attached to a loaded app.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(m, dict):
        return (["app.json must be a JSON object"], warnings)

    app_id = m.get("id", dir_name)
    if not isinstance(app_id, str) or not APP_ID_RE.match(app_id):
        errors.append(f"id {app_id!r} must match [a-z0-9][a-z0-9-]* (or omit it to use the folder name)")

    if not m.get("label") or not isinstance(m.get("label"), str):
        errors.append("label is required and must be a non-empty string")
    if not m.get("icon") or not isinstance(m.get("icon"), str):
        errors.append("icon is required and must be a non-empty string")

    entry_point = m.get("entry_point", "")
    if not isinstance(entry_point, str):
        errors.append("entry_point must be a string (a backend URL)")
        entry_point = ""
    if entry_point:
        try:
            parsed = urlparse(entry_point)
        except ValueError:
            parsed = None
        if (parsed is None or parsed.scheme != "http" or
                parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or
                parsed.username is not None or parsed.password is not None):
            errors.append("entry_point must be an http:// loopback URL (localhost, 127.0.0.1, or ::1)")
    if not has_index and not entry_point:
        errors.append("app has neither an index.html nor an entry_point")

    version = m.get("version", "")
    if version and not _SEMVER_RE.match(str(version)):
        errors.append(f"version {version!r} is not a valid semver (e.g. \"1.0.0\")")
    elif not version:
        warnings.append("no version field — defaulting to 0.0.0; add one for update ordering")

    mev = m.get("min_engine_version", "")
    if mev and not _SEMVER_RE.match(str(mev)):
        errors.append(f"min_engine_version {mev!r} is not a valid semver")

    cats = m.get("categories")
    if cats is not None and not (isinstance(cats, list) and all(isinstance(c, str) for c in cats)):
        errors.append("categories must be a list of strings")

    shots = m.get("screenshots")
    if shots is not None:
        if not isinstance(shots, list) or not all(isinstance(s, str) for s in shots):
            errors.append("screenshots must be a list of relative path strings")
        elif not all(_is_relative_asset(s) for s in shots):
            errors.append("screenshots must be relative paths inside the app (no '/' prefix or '..')")

    for key in ("description", "author"):
        if key in m and not isinstance(m[key], str):
            errors.append(f"{key} must be a string")

    perms = m.get("permissions")
    if perms is not None:
        if not (isinstance(perms, list) and all(isinstance(p, str) for p in perms)):
            errors.append("permissions must be a list of capability strings")
        else:
            for p in perms:
                if p not in PERMISSION_FEATURES:
                    warnings.append(f"unknown permission {p!r} (ignored; not a recognized capability)")

    sandbox = m.get("sandbox", "allow-scripts")
    if not (sandbox is False or isinstance(sandbox, str)):
        errors.append("sandbox must be a string or false")

    for key in m:
        if key not in KNOWN_FIELDS:
            warnings.append(f"unknown manifest field {key!r} (ignored — check for a typo)")

    return (errors, warnings)


def _features_from_allow(raw: object) -> list[str]:
    """Extract recognized features from a legacy Permissions-Policy ``allow``
    string (e.g. "microphone; camera"). Kept so Atrium's `allow` manifests
    remain a valid source under the shared contract."""
    out: list[str] = []
    if isinstance(raw, str):
        for part in re.split(r"[;,]", raw):
            token = part.strip()
            name = token.split()[0].lower() if token else ""
            if name in PERMISSION_FEATURES and name not in out:
                out.append(name)
    return out


def permission_list(m: dict) -> list[str]:
    """The app's granted browser capabilities: recognized entries from
    ``permissions`` (canonical) unioned with any from a legacy ``allow`` string,
    deduped and order-stable. Unknown tokens are dropped."""
    out: list[str] = []
    perms = m.get("permissions")
    if isinstance(perms, list):
        for p in perms:
            if isinstance(p, str) and p in PERMISSION_FEATURES and p not in out:
                out.append(p)
    for f in _features_from_allow(m.get("allow", "")):
        if f not in out:
            out.append(f)
    return out


def permissions_to_allow(perms: list[str]) -> str:
    """Render a permission list into an iframe ``allow`` (Permissions-Policy)
    attribute value. The container grants each feature to the iframe's own
    origin, so no per-feature origin list is needed."""
    return "; ".join(perms)


def normalize(m: dict, dir_name: str) -> dict:
    """Return the normalized listing/runtime fields for a validated manifest."""
    permissions = permission_list(m)
    sandbox = m.get("sandbox", "allow-scripts")
    if sandbox is False:
        sandbox = ""
    return {
        "id": m.get("id", dir_name),
        "label": m["label"],
        "icon": m["icon"],
        "version": str(m.get("version", "") or "0.0.0"),
        "description": m.get("description", ""),
        "categories": list(m.get("categories", []) or []),
        "author": m.get("author", ""),
        "screenshots": [s for s in (m.get("screenshots", []) or []) if _is_relative_asset(s)],
        "min_engine_version": str(m.get("min_engine_version", "") or ""),
        "permissions": permissions,
        "allow": permissions_to_allow(permissions),
        "sandbox": sandbox,
        "chat_enabled": bool(m.get("chat_enabled", False)),
        "chat_system_prompt": m.get("chat_system_prompt", ""),
        "chat_knowledge": m.get("chat_knowledge", ""),
        "entry_point": m.get("entry_point", "") or "",
    }


def _cli(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m app_engine.manifest <app-dir> [<app-dir> ...]", file=sys.stderr)
        return 2
    rc = 0
    for arg in argv:
        d = Path(arg)
        manifest = d / "app.json"
        if not manifest.is_file():
            print(f"✗ {d}: no app.json")
            rc = 1
            continue
        try:
            m = json.loads(manifest.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"✗ {d}: invalid app.json — {exc}")
            rc = 1
            continue
        errors, warnings = validate_manifest(m, has_index=(d / "index.html").is_file(), dir_name=d.name)
        label = m.get("id", d.name)
        if errors:
            rc = 1
            print(f"✗ {label}: {len(errors)} error(s)")
            for e in errors:
                print(f"    error:   {e}")
        else:
            print(f"✓ {label}: valid" + (f" (engine {ENGINE_VERSION})" if not warnings else ""))
        for w in warnings:
            print(f"    warning: {w}")
    return rc


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
