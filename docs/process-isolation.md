# Managed command isolation status

Set `APP_ENGINE_PROCESS_ISOLATION` before starting app-engine:

| Mode | Managed install, build, and start commands |
| --- | --- |
| `legacy` | Run on the service host. |
| `audit` | Run on the service host and log one structured `managed_process_isolation` record before each execution. |
| `enforce` (default) | Run managed commands through the installed isolated broker; fail closed if it is unavailable. |

`APP_ENGINE_BLOCKED_APP_IDS` is an optional comma-separated list of app IDs
whose managed preview and launch operations are rejected before commands run.
Use it to hold an incompatible app during a global `enforce` rollout. Removing
an ID is an operator change and does not approve that app's exact launch plan.

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

## Enforced boundary

The runner requires Linux on x86-64 or ARM64, systemd with working namespace,
seccomp and IP filtering support, and Landlock ABI 4 or later. Missing kernel or
runner support refuses execution. macOS and Windows continue to serve static
apps; running trusted backends there requires explicitly selecting `legacy` or
`audit`, which grants host account privileges.

Each enforced command runs as a dynamic UID in a root-controlled filesystem
root with read-only system binaries/libraries, the fixed executor and its own
read-only `/workspace`. Only that scope's `/app-data` and `/app-build` are writable.
Original app/state paths, host home/configuration and privileged sockets are not
mounted. Socket creation is limited to TCP over IPv4/IPv6; Unix sockets, datagrams,
raw sockets, non-TCP stream protocols (including SCTP/MPTCP), compatibility syscall
architectures and io_uring are restricted. A root-owned hosts file resolves only
localhost and the fixed local status capability; no ambient DNS is delegated.
The kernel denies TCP connections except the ports granted by the root-owned
broker policy; cgroup IP filtering restricts those ports to loopback. App listeners
receive only their assigned bind port (room servers may bind an ephemeral port).
These restrictions also apply to descendants and cannot be relaxed by app code.

Optional root-owned `service_ports` in `/etc/app-engine-runner.json` maps app IDs
to arrays of dedicated gateway ports, for example `{"cards": [18890]}`. This is a
connection capability to the whole port: use a dedicated, authenticated service
with the app's intended API, never the engine, Docker, Ollama's management API or
a sibling backend. The broker overwrites any network grants supplied by a client.
The existing Package Tracker policy grants its reviewed carrier/browser/status
gateways only in its start phase. No service is granted implicitly to other apps.
Install/build steps also have no default egress; ship reviewed dependencies or
configure a dedicated package gateway rather than granting general network access.

Public requests preserve the real player identity in runtime scope keys. Shared
runtimes remain shared; per-user runtimes have separate processes, configuration,
data and build directories. The owner grants approval to the current app code;
this lets invited users run that version in their own scopes without granting
execution approval themselves. Existing app-wide build outputs must be rebuilt
in the new scope-specific build directory.

Legacy `entry_point` services require a separate owner approval covering the
app package and exact pinned loopback URL. The launcher requests it before loading
the frame. Engine/management destinations are refused; changed content or bindings
require approval again. The reusable gateway defaults to refusing legacy bindings;
embedding hosts must supply `authorize_backend` with their explicit trust policy.
This authorizes proxy access to an external service, not containment of a process
that was started outside app-engine.

## Reproducible kernel tests

`security_tests/isolated_vm.py` runs only as root in a disposable VM explicitly
marked with `/run/app-engine-audit-disposable`. It uses fake local TCP, UDP and
Unix services, disposable canary files and an unprivileged engine account; it
never needs production credentials or host directory mounts. Run:

```sh
sudo touch /run/app-engine-audit-disposable
sudo env PYTHONPATH="$PWD" python3 security_tests/isolated_vm.py --engine-user "$(id -un)"
```

The harness tests denial of engine/sibling TCP, a reachable non-loopback canary,
Unix/abstract sockets, UDP, original host paths, sibling files and another user's
data. It also proves own-data/build writes, an explicit service capability and
engine-to-app health access work. The dedicated GitHub Actions isolation lane
runs this harness on a disposable Ubuntu VM. Python tests separately cover actual
public-user process identities and legacy proxy approval. This is OS containment,
not a claim of immunity from kernel vulnerabilities.

References: [Linux Landlock ABI and TCP rules](https://www.kernel.org/doc/html/v6.8/userspace-api/landlock.html),
[systemd execution namespaces and filters](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).
