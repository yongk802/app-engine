# Production app launch block

The owner chose to make Package Tracker unavailable on the production VPS while
its browser and network dependencies are adapted to the isolated runner. The
engine will retain its source, manifest, and data. Operators set
`APP_ENGINE_BLOCKED_APP_IDS=package-tracker` and
`APP_ENGINE_PROCESS_ISOLATION=enforce` in the service environment.

`ProcessIsolationPolicy.from_environment() -> ProcessIsolationPolicy` parses
comma-separated app IDs with the same strict grammar used by the pilot list.
`is_blocked(app_id: str) -> bool` reports a match, and
`decide(phase: str, app_id: str | None) -> ProcessDecision` denies every managed
command phase for a blocked app. Invalid IDs raise `ValueError` at service
startup. The lifecycle rejects a blocked app during both preview and launch
with `LaunchError(code="app_launch_blocked")`, before approval, preparation,
or spawning. The command check repeats the policy as defense in depth.

Only the configured app ID is blocked. Other managed commands use the existing
root broker with a changing Unix identity and denied non-loopback egress.
Runtime data/build directories use owner/group mode 0770, matching the runner
operations guide. Existing catalog and source entries remain visible, and the
operator can remove the block after Package Tracker passes its own compatibility
and exact owner launch gates. The rollout gate is a rejected production preview
and open for Package Tracker, a successful Night City Table launch and persistence
check, no host-UID managed child, and healthy engine/mTLS/store services.
