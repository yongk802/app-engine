#!/usr/bin/env python3
"""Non-destructive clean-machine smoke check for a running app-engine."""
from __future__ import annotations

import argparse
import json
import platform
from urllib.parse import urlparse

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8770")
    parser.add_argument("--require-ready", action="store_true",
                        help="also require Ollama and the selected model to be ready")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    report = {"platform": platform.platform(), "base_url": base, "checks": {}}
    with httpx.Client(base_url=base, timeout=10, follow_redirects=False) as client:
        root = client.get("/"); root.raise_for_status()
        report["checks"]["launcher"] = "ok"
        engine = client.get("/api/engine"); engine.raise_for_status()
        report["engine"] = engine.json()
        apps = client.get("/api/apps"); apps.raise_for_status()
        listed = apps.json()
        for app in listed:
            parsed = urlparse(app["url"])
            assert parsed.hostname == f"{app['id']}.localhost"
            assert parsed.fragment.startswith("atrium_state_token=")
        report["checks"]["isolated_app_origins"] = len(listed)
        status = client.get("/api/local-ai/status"); status.raise_for_status()
        ai = status.json()
        assert ai["privacy"] == "Everything stays on this computer."
        report["local_ai"] = {
            "state": ai["state"],
            "profile": ai["selected_profile"],
            "model": ai["selected_model_id"],
            "ollama_running": ai["ollama"]["running"],
        }
        if args.require_ready and ai["state"] != "ready":
            raise SystemExit("Local AI is not ready; complete setup in the launcher and retry")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

