#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE_REPO="$ROOT"
if [ ! -f "$ENGINE_REPO/engine.py" ]; then ENGINE_REPO="$(dirname "$ROOT")/app-engine"; fi
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
swiftc "$ROOT/desktop-app/ServiceConfiguration.swift" "$ROOT/desktop-app/ServiceManager.swift" "$ROOT/desktop-app/ServiceTests.swift" -o "$BUILD/service-tests"
"$BUILD/service-tests"
if [ "${1:-}" = "--integration" ]; then
  swiftc "$ROOT/desktop-app/ServiceConfiguration.swift" "$ROOT/desktop-app/ServiceManager.swift" "$ROOT/desktop-app/IntegrationTests.swift" -o "$BUILD/integration-tests"
  "$BUILD/integration-tests" "${2:-$ENGINE_REPO}" "${3:-$(dirname "$ENGINE_REPO")/app-store}"
fi
