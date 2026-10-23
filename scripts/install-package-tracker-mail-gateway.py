#!/usr/bin/env python3
"""Add read-only Package Tracker mail routes to the existing Mac mTLS gateway."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MARKER = "# package-tracker-mail-v2"
OLD_MARKER = "# package-tracker-mail-v1"
SENTINEL = "location / { return 404; }"
MAIL_PATHS = (
    "= /api/email/accounts",
    "~ ^/api/email/accounts/[^/]+/inbox$",
    "~ ^/api/email/accounts/[^/]+/message/[^/]+$",
)
QUERY_GUARDS = (
    '            if ($args != "") { return 403; }\n',
    '            if ($args !~ "^folder=INBOX&limit=[0-9]{1,3}$") { return 403; }\n',
    '            if ($args !~ "^folder=INBOX$") { return 403; }\n',
)


def render(source: str, ca_file: str) -> str:
    if (not ca_file.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_./-]+", ca_file)
            or source.count("listen 127.0.0.1:9443 ssl;") != 1
            or source.count("ssl_verify_client on;") != 1
            or source.count('"CN=app-engine-client"') != 1
            or source.count("location = /health") != 1
            or source.count(SENTINEL) != 1):
        raise ValueError("unexpected Atrium status gateway configuration")
    trusted = f"proxy_ssl_trusted_certificate {ca_file};"
    if MARKER in source:
        if (source.count(MARKER) != 1 or source.count(trusted) != 4
                or any(source.count(guard) != 1 for guard in QUERY_GUARDS)):
            raise ValueError("unexpected Atrium status gateway configuration")
        return source
    if OLD_MARKER in source:
        if (source.count(OLD_MARKER) != 1 or source.count(trusted) != 4
                or source.count('"CN=package-tracker-client"') != 3
                or source.count("access_log off;") != 3):
            raise ValueError("unexpected Atrium status gateway configuration")
        source = source.replace(OLD_MARKER, MARKER, 1)
        for path, guard in zip(MAIL_PATHS, QUERY_GUARDS):
            start = source.find(f"location {path} {{")
            end = source.find("\n        }", start)
            if start < 0 or end < 0:
                raise ValueError("unexpected Atrium status gateway configuration")
            block = source[start:end]
            if block.count("            access_log off;\n") != 1:
                raise ValueError("unexpected Atrium status gateway configuration")
            source = (source[:start] + block.replace(
                "            access_log off;\n", guard + "            access_log off;\n", 1
            ) + source[end:])
        return source
    if source.count("proxy_ssl_verify off;") != 1 or "/api/email/" in source:
        raise ValueError("unexpected Atrium status gateway configuration")
    source = source.replace(
        "proxy_ssl_verify off;",
        "proxy_ssl_verify on;\n            " + trusted +
        "\n            proxy_ssl_name 127.0.0.1;",
        1,
    )
    locations = []
    for path, guard in zip(MAIL_PATHS, QUERY_GUARDS):
        locations.append(
            f"        location {path} {{\n"
            '            if ($ssl_client_s_dn != "CN=package-tracker-client") { return 403; }\n'
            '            if ($request_method != GET) { return 405; }\n'
            "            limit_except GET { deny all; }\n"
            f"{guard}"
            "            access_log off;\n"
            "            proxy_pass https://127.0.0.1:9900;\n"
            "            proxy_ssl_verify on;\n"
            f"            {trusted}\n"
            "            proxy_ssl_name 127.0.0.1;\n"
            "            proxy_set_header Host 127.0.0.1:9900;\n"
            "            proxy_set_header Authorization $http_authorization;\n"
            '            proxy_set_header Cookie "";\n'
            '            proxy_set_header X-Forwarded-For "";\n'
            "        }\n"
        )
    return source.replace(
        SENTINEL,
        MARKER + "\n" + "".join(locations) + "        " + SENTINEL,
        1,
    )


def _nginx(binary: str, prefix: Path, config: Path, action: str) -> None:
    flags = {"test": ["-t"], "reload": ["-s", "reload"]}.get(action)
    if flags is None:
        raise ValueError("unsupported nginx action")
    subprocess.run([binary, "-p", str(prefix), "-c", str(config), *flags],
                   check=True, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--install", action="store_true")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--nginx-prefix", type=Path, required=True)
    parser.add_argument("--nginx-bin", default="nginx")
    args = parser.parse_args()
    if (not args.config.is_file() or args.config.is_symlink()
            or not args.ca_file.is_file() or args.ca_file.is_symlink()
            or not args.nginx_prefix.is_dir()):
        parser.error("existing gateway configuration, CA, and prefix are required")
    source = args.config.read_text()
    try:
        target = render(source, str(args.ca_file.resolve(strict=True)))
    except ValueError as exc:
        parser.error(str(exc))
    if target == source:
        print("Package Tracker mail gateway is already installed.")
        return 0
    descriptor, temporary = tempfile.mkstemp(prefix=".package-tracker-nginx-", dir=args.config.parent)
    candidate = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(target)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(candidate, args.config.stat().st_mode & 0o777)
        _nginx(args.nginx_bin, args.nginx_prefix, candidate, "test")
        if args.check:
            print("Package Tracker mail gateway candidate passes nginx -t; no change applied.")
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = args.config.with_name(args.config.name + f".{stamp}.bak")
        if backup.exists():
            raise ValueError("gateway backup path already exists")
        shutil.copy2(args.config, backup)
        try:
            os.replace(candidate, args.config)
            _nginx(args.nginx_bin, args.nginx_prefix, args.config, "test")
            _nginx(args.nginx_bin, args.nginx_prefix, args.config, "reload")
        except (OSError, subprocess.CalledProcessError):
            shutil.copy2(backup, args.config)
            _nginx(args.nginx_bin, args.nginx_prefix, args.config, "reload")
            raise
        print(f"Installed Package Tracker mail gateway. Backup: {backup}")
        return 0
    finally:
        candidate.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
