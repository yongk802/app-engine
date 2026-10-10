# Windows launcher implementation plan

Goal: provide the same standalone App Engine/App Store desktop controls on Windows and version all launcher sources in both repos.
Architecture: .NET Framework Windows Forms UI, asynchronous service manager, Job Object broker, build/install PowerShell, source sync from App Engine to App Store.
Tracking: beads atrium-in51e, rather than Markdown task lists.

1. Add Windows tests first: configure temporary roots with spaces, assert literal argv preservation, loopback, identity, reject duplicate ports/missing runtime, broker ownership isolation and PID mismatch. Run on .173 to observe missing implementation. Then implement Settings.cs and Services.cs with the public settings/config/manager/broker APIs exercised by those tests.
2. Add real integration acceptance using D:\git\app-engine and D:\git\app-store, temporary apps/state/catalog and distinct ports. Start, repeat start, observe external ownership from another manager, refuse foreign stop, stop owned service and children. Keep tests bounded with finally cleanup.
3. Implement Launcher.cs/Program.cs, keeping poll and operation states distinct and querying live ownership for settings. Compile via desktop-app/windows/build.ps1 with csc and install via install.ps1 into D:\Apps\App Engine with Desktop .lnk. Test shortcut target and native visible UI on .173, capture and judge screenshot, repair defects.
4. Add source sync/check script and adjust macOS build defaults for either checkout. Sync all source to app-store/desktop-app, run synchronization check and macOS configuration tests. Independently review code, resolve blockers and repeat relevant verification.
5. Commit/push both repos, update .173 to the accepted revisions and verify hashes, close beads and clean isolated worktrees.
