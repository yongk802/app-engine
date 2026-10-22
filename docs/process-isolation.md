# Managed command isolation status

Set `APP_ENGINE_PROCESS_ISOLATION` before starting app-engine:

| Mode | Managed install, build, and start commands |
| --- | --- |
| `legacy` (default) | Run on the service host. |
| `audit` | Run on the service host and log one structured `managed_process_isolation` record before each execution. |
| `enforce` | Refuse every managed command with `isolated_runner_unavailable`. There is no isolated runner yet. |

`audit` records contain the app ID, target ID, phase, mode, and execution class.
They omit argv, working directory, environment values, and app configuration
secrets. Capture the app-engine process logs while piloting this mode. Cached
install/build steps produce no execution record because they do not run;
crash restarts produce another `start` record.

This policy covers the `ProcessLifecycleManager` launch family only. The
multiplayer Node service, local Ollama startup, and Studio toolchain probing
have separate process launch points and are **not isolated** by this setting.
The engine's legacy `entry_point` proxy also points to an operator-supplied
backend outside this lifecycle.

Do not treat `audit` as sandboxing. Do not enable `enforce` expecting managed
apps to launch: it deliberately fails closed until a runner can prove that it
blocks host secret and daemon-socket access, passes a controlled negative
egress probe, and serves a normal app with its health check. Package download
needs a separate, narrow network operation before build and install can be
isolated without breaking supported workflows.
