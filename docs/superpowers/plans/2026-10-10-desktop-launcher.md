# App Engine desktop launcher implementation plan

**Goal:** Install a native Swift Desktop control panel that launches App Engine and App Store on demand.
**Architecture:** Pure service definitions generate validated launchd plists; a focused manager runs exact owned jobs and validates HTTP identity; Cocoa provides service cards and settings. Existing Python services and browser UI remain authoritative.
**Tech stack:** Swift/Foundation/AppKit, launchd, existing Python virtual environments. Tracking: atrium-gebdw.

First write `desktop-app/ServiceTests.swift` against `ServiceConfiguration` and service identity decoding. Run `desktop-app/test.sh` and observe the missing implementation. Cover whitespace paths, executable arguments without shell expansion, isolated state dirs and labels, invalid ports, unrelated service responses and no automatic login registration.

Implement `desktop-app/ServiceConfiguration.swift`, then `ServiceManager.swift` for nonblocking status/start/stop, real health polling, port conflicts and exact job ownership. Native service integration tests use temporary labels and alternate ports, start the real app-engine and app-store with temporary data, verify HTTP identity and stop only their own jobs. Failure must clean up those jobs.

Implement `desktop-app/AppEngine.swift` as two cards with native controls, a settings dialog, accessibility labels and reopen support. Add `Icon.swift` and `build.sh`: compile with swiftc, generate an icon, write Info.plist via plistlib, ad-hoc sign the local launcher (it requests no TCC entitlements), copy to Desktop only on --install. Add `desktop-app/README.md` with paths, service lifetime and rebuild instructions.

Run native tests, build and smoke checks; install the requested Desktop app; launch it and inspect an actual screenshot and accessibility tree. Use UI controls to start/open App Store and App Engine and validate their pages. Independently review, fix findings, commit only owned new files and push main; preserve unrelated speech edits in the shared checkout.
