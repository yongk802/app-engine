# App Engine desktop launcher

A native macOS control window for App Engine and App Store. Each has Start,
Stop, Open and Logs buttons. Open starts the selected service when needed and
opens its web UI in your default browser. Merely opening the launcher starts
neither server. Closing the launcher leaves running services available until
stopped or logout. There is no login autostart.

## Build and install

Requires macOS 13+, Xcode command-line tools, and local `app-engine` and
`app-store` checkouts with their `uv` Python environments already set up.

```sh
bash desktop-app/build.sh --install
```

This creates `~/Desktop/App Engine.app`. By default it uses the build checkout,
its sibling `app-store` and `personal-apps` folders, existing App Engine state at
`~/.config/app-engine/app-state`, and `app-store/store-data`. When building from a
temporary worktree, pass `--engine-repo /absolute/path/to/permanent/app-engine`.
The launcher does not bundle Python or install dependencies.

Settings lets you change source, apps and data folders and the two ports
(default 8770/8780). Stop launcher-managed services before changing settings.
Both servers bind to loopback only. Existing healthy services can be opened,
but the launcher never stops a service it did not launch.

The two owned launchd jobs are `io.appengine.desktop.engine` and
`io.appengine.desktop.store`. Their definitions are in
`~/Library/Application Support/App Engine/Services`, not `LaunchAgents`.
Logs are in `~/Library/Logs/App Engine`. These jobs are independent of Atrium.
The bundle is ad-hoc signed for local use, not notarized for distribution.

## Verify

```sh
bash desktop-app/test.sh
bash desktop-app/test.sh --integration /path/to/app-engine /path/to/app-store
```

Integration tests start real servers on ports 18770/18780 using temporary state,
data, and isolated job labels, then stop them. Occupied test ports fail safely.
