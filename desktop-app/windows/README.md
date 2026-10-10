# Windows launcher

A native Windows Forms control window for App Engine and App Store. Uses Windows
10/11 and .NET Framework 4.8 (included with current Windows), with no Atrium,
WSL, PowerShell GUI runtime, or third-party desktop framework dependency.

Set up both repositories' Python environments with `uv sync --python 3.12`.
From either repository, install with Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop-app\windows\install.ps1
```

This compiles `AppEngine.exe`, installs it into `D:\Apps\App Engine` when D:
exists (otherwise `%LOCALAPPDATA%\App Engine`), and creates **App Engine.lnk**
on your Desktop. No elevated shell, firewall change, scheduled task or login
startup is required. `-InstallDirectory`, `-EngineRepository`,
`-StoreRepository`, `-AppsDirectory`, `-EnginePort` and `-StorePort` can override
the defaults. The sources normally live in sibling `app-engine`, `app-store`
and `personal-apps` folders. Existing saved settings are preserved on reinstall
unless you explicitly supply an override. Close the launcher and stop its
managed services before reinstalling.

Each service has Start, Stop, Open and Logs controls. Open starts its service as
needed and opens the default browser. Opening the launcher alone starts neither
server. Closing it leaves services running until stopped or logout. Existing
healthy external servers can be opened but cannot be stopped by this launcher.
Both servers listen on `127.0.0.1`; default ports are 8770 and 8780.

Settings lives in the install directory's `settings.json`; engine app state in
`app-state`; broker ownership records and logs in `runtime`. Each service has a
separate background broker and Windows Job Object. Stop verifies process ID,
creation time, executable and broker token, then closes that service's process
tree. It never kills processes by name or by port.

## Build and verify

```powershell
.\desktop-app\windows\build.ps1 -Test
.\desktop-app\windows\build.ps1 -Test -EngineRepository D:\git\app-engine -StoreRepository D:\git\app-store
```

The second command runs real servers with temporary state/catalog, isolated
ownership records and ports 18770/18780. Occupied test ports fail safely. The
shared desktop sources are maintained in App Engine and synchronized into
App Store; see `../SOURCE.md`.
