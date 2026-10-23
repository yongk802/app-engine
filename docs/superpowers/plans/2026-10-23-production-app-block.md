# Production App Block Implementation Plan

**Goal:** Deny Package Tracker launches on the VPS and enforce isolation for
every remaining managed command.

**Architecture:** Extend the existing process policy with a strict blocked-ID
set. Reject blocked previews before any launch side effect and retain a command
check for defense in depth. Change enforced runtime directories to mode 0770.
The production service sets the block and global enforce after the reviewed
code lands.

**Tech stack:** Python, pytest, systemd, VPS app-engine runtime.

1. In `tests/test_process_isolation_policy.py`, exercise strict blocked-ID
   parsing, denial of Package Tracker, and isolated Night City Table under
   global enforce. In `tests/test_lifecycle.py`, prove preview/launch reject a
   blocked app and enforced runtime directories are group scoped. Run the
   focused tests and observe the expected failures.
2. In `app_engine/process_isolation.py`, parse blocked IDs and return a denied
   `ProcessDecision`. In `app_engine/lifecycle.py`, reject the blocked app at
   preview and at the command check; change enforced directory modes to 0770.
   Rerun focused and full test suites and lint.
3. Document the operator setting and rollback in
   `docs/operations/isolated-runner.md`. Review and merge the exact-head PR
   after all required CI checks pass.
4. On the VPS, verify only the two known process manifests exist and back up
   the current service override. Set the blocked app ID and global enforce,
   reload systemd, restart app-engine, and verify effective environment.
   Test rejected Package Tracker preview/open, positive Night City Table
   launch/state/price checks, distinct isolated UID and egress boundary, and
   engine/mTLS/store health. Roll back the override to audit if a gate fails.
