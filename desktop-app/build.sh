#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENGINE_REPO="$ROOT"
if [ ! -f "$ENGINE_REPO/engine.py" ]; then ENGINE_REPO="$(dirname "$ROOT")/app-engine"; fi
INSTALL=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --install) INSTALL=true; shift ;;
    --engine-repo) ENGINE_REPO="$2"; shift 2 ;;
    *) echo "Usage: $0 [--install] [--engine-repo /absolute/app-engine]" >&2; exit 2 ;;
  esac
done
BUILD="$ROOT/desktop-app/build"
APP="$BUILD/App Engine.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
xcrun swiftc -O -target "$(uname -m)-apple-macosx13.0" -framework AppKit "$ROOT/desktop-app/ServiceConfiguration.swift" "$ROOT/desktop-app/ServiceManager.swift" "$ROOT/desktop-app/AppEngine.swift" -o "$APP/Contents/MacOS/AppEngine"
/usr/bin/swift "$ROOT/desktop-app/Icon.swift" "$BUILD/icon.png"
ICONSET="$BUILD/AppIcon.iconset"
mkdir -p "$ICONSET"
for SIZE in 16 32 128 256 512; do
  /usr/bin/sips -z "$SIZE" "$SIZE" "$BUILD/icon.png" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" >/dev/null
  DOUBLE=$((SIZE * 2))
  /usr/bin/sips -z "$DOUBLE" "$DOUBLE" "$BUILD/icon.png" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
/usr/bin/python3 - "$APP/Contents/Info.plist" "$ENGINE_REPO" <<'PY'
import plistlib, sys
with open(sys.argv[1], 'wb') as file:
    plistlib.dump(dict(CFBundleName='App Engine', CFBundleDisplayName='App Engine',
        CFBundleIdentifier='io.appengine.desktop', CFBundleExecutable='AppEngine',
        CFBundlePackageType='APPL', CFBundleVersion='1', CFBundleShortVersionString='1.0',
        CFBundleIconFile='AppIcon', NSHighResolutionCapable=True, LSMinimumSystemVersion='13.0',
        AppEngineRepository=sys.argv[2]), file)
PY
/usr/bin/codesign --force --sign - "$APP"
/usr/bin/codesign --verify --strict "$APP"
if $INSTALL; then
  DEST="$HOME/Desktop/App Engine.app"
  if [ -e "$DEST" ]; then
    ID=$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$DEST/Contents/Info.plist" 2>/dev/null || true)
    if [ "$ID" != "io.appengine.desktop" ]; then echo "Refusing to replace unrelated $DEST" >&2; exit 1; fi
  fi
  /usr/bin/ditto "$APP" "$DEST"
  echo "Installed: $DEST"
else
  echo "Built: $APP"
fi
