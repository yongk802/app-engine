# Managed command isolation status

Set `APP_ENGINE_PROCESS_ISOLATION` before starting app-engine:

| Mode | Managed install, build, and start commands |
| --- | --- |
| `legacy` (default) | Run on the service host. |
| `audit` | Run on the service host and log one structured `managed_process_isolation` record before each execution. |
| `enforce` | Run managed commands through the installed isolated broker; fail closed if it is unavailable. |

`audit` records contain the app ID, target ID, phase, mode, and execution class.
They omit argv, working directory, environment values, and app configuration
secrets. Capture the app-engine process logs while piloting this mode. Cached
install/build steps produce no execution record because they do not run;
crash restarts produce another `start` record.

This policy covers managed app install, build, start, and multiplayer rules
processes. Local Ollama startup and Studio toolchain probing have separate
process launch points and are not isolated by this setting.
The engine's legacy `entry_point` proxy also points to an operator-supplied
backend outside this lifecycle.

Do not treat `audit` as sandboxing. Install and validate the broker before
enabling `enforce`. See [isolated-runner operations](operations/isolated-runner.md)
for setup, negative egress checks, per-app compatibility gates, and rollout.
