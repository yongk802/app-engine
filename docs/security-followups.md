# Remaining audit boundaries

These follow-ups are separate from the original AE-01–AE-12 fixes. The tests
exercise hostile inputs; deployments must independently select and verify the
new policies. See [the dated installation inventory](operations/security-inventory-2026-11-18.md).

## Resource budgets (atrium-jyw9jh)

Enforced Linux commands use one systemd slice per app, shared across users,
install/build/start and multiplayer phases. Defaults: one CPU, 1 GiB memory,
no swap, 128 tasks. Preparation expires after 900 seconds; services expire after
24 hours and need reopening/restarting. All descendants receive group termination
and a five-second grace period. Core dumps are disabled. stdout/stderr combined
are limited to 16 MiB per command. Fixed tmpfs mounts cap `/tmp` and `/var/tmp`
at 64 MiB and 4096 inodes each, charged to the app's memory budget.

Persistent storage requires **dedicated root-owned mounted filesystems** at
`<runtime>/data/<app-id>` and `<runtime>/build/<app-id>`, plus
`<multiplayer-root>/<app-id>` when multiplayer is requested. Each filesystem's
entire capacity must be at most 1 GiB and 65536 inodes. Each must use a distinct
device, separate from the engine's state filesystem. These mounts cover all user
scopes. An ordinary host directory, symlink, reused device, unowned mount or
oversized filesystem fails closed before app execution. This is an intentional
runner upgrade requirement; updating engine source alone does not provision
storage or upgrade root-installed broker code.

The root policy `/etc/app-engine-runner.json` can contain `resource_limits`
with integer fields from `ResourceLimits`: `cpu_percent`, `memory_bytes`,
`tasks`, `disk_bytes`, `disk_inodes`, `preparation_seconds`, `service_seconds`,
`output_bytes`. Apps cannot submit limits. Admission remains owner-approved;
these budgets do not bound a malicious engine owner admitting unlimited apps.

Rollout: stop the affected app and room units; make a private backup and test its
restore; provision one finite ext4/XFS filesystem per mount (finite block and inode
capacity); copy state while retaining its engine owner/group and scoped 0770/0660
permissions; root-own the mount roots, group them to the engine and use 0770;
configure persistent mounts with nodev/nosuid; install the reviewed root-owned
broker release; verify the actual volumes and service policies; run synthetic
startup/save and sibling-app probes before enabling enforcement. Never mount over
unmigrated data. Keep original backups until acceptance. Audit mode continues to
grant host privileges and does not claim any of these limits.

`security_tests/isolated_vm.py` provisions tiny loopback ext4 volumes **only in a
marked disposable root VM**, then tests real CPU throttling, OOM, process creation,
block/inode exhaustion, temporary storage, output and wall deadlines. A sibling
app keeps writing a heartbeat and ordinary startup/saves are checked after each
attack. It also retains the original real network/filesystem boundary tests.

## Grounding regexes (atrium-cxqvbp)

Compilation and matching both happen in a fresh isolated Python interpreter,
with no inherited environment or site packages. Each call has a two-second wall
deadline, kill/reap on expiry, at most two workers, 64 patterns (1024 characters
each), 128 KiB answer characters and bounded result strings. Linux additionally
sets a 256 MiB address-space and one CPU-second limit; Unix disables worker core
dumps. Windows/macOS rely on the parent deadline where address-space limits are
unavailable. Packs are at most 2 MiB and 1024 entries. Compilation/search never
runs on the ASGI event loop; an overloaded or expired check produces a visible
`grounding_unavailable` error instead of pretending validation succeeded. This
worker isolates computation, not arbitrary executable app code.

## Publisher and builder trust (atrium-0m9g25)

`APP_ENGINE_PUBLISHER_POLICY` selects an operator-controlled JSON file. The
engine fails startup for a malformed or writable policy. Keys are raw 32-byte
Ed25519 public keys, base64 encoded. Example schema (replace placeholders):

```json
{
  "mode": "require_signed",
  "publishers": {
    "owner": {
      "keys": {"2026": "BASE64_PUBLISHER_PUBLIC_KEY"},
      "revoked_keys": [], "apps": ["demo"],
      "repository": "https://example.com/owner/demo", "builders": ["ci"]
    }
  },
  "builders": {
    "ci": {
      "keys": {"2026": "BASE64_BUILDER_PUBLIC_KEY"},
      "revoked_keys": [], "apps": ["demo"],
      "repository": "https://example.com/owner/demo"
    }
  }
}
```

The catalog's optional `attestation` object contains `publisher`, `key_id`,
`signature`, and `provenance`. Provenance contains `statement`, `key_id`,
`signature`. Its statement has exactly `type: app-engine-provenance-v1`,
`app_id`, `version`, `sha256`, `size_bytes`, `builder`, `repository`, `commit`
(full lowercase SHA-1/SHA-256 commit). The builder signs canonical statement
bytes. The publisher signs the result of `release_statement`, binding **every
release metadata field**, publisher/key identity and the whole builder envelope.
`canonical` uses ASCII JSON, sorted keys and compact separators. Signatures are
base64 Ed25519. The adversarial fixtures in `tests/test_signed_releases.py`
provide an executable producer example with separate generated keys.

Provision both trust roots independently of the store, verify fingerprints out
of band, and keep private publisher and builder keys separate from store servers.
Rotate by adding a new key ID to the operator policy, publishing with it, then
revoking the old key ID. Restart the engine after changing the policy; previews
are process-local and installation re-verifies the selected policy. Revocation
blocks future installations after policy reload; it does not uninstall or stop
already approved applications.

Compatibility is explicit: no policy selects `allow_unsigned` with no trusted
identities, preserving owner-reviewed existing stores. `require_signed` rejects
unsigned releases. A release that claims signatures must verify even in
compatibility mode. Installing signed bytes still never approves or runs code.
These signatures authenticate publisher/builder claims and their artifact/source
binding. They do not establish reproducible builds, benign code, transparency
log inclusion, CI workload identity, or revocation of already installed bytes.
Do not describe this small versioned envelope as SLSA/Sigstore verification.

## Deployment evidence and advisories (atrium-oyf74f)

`python -m app_engine.security_inventory --repo <checkout> --service app-engine
--state-dir <state> --endpoint http://127.0.0.1:8770` collects read-only Linux
service evidence. On macOS use `--plist <service.plist>`; the report labels plist
configuration separately from runtime activation. Only named configuration keys
are exported; passwords, tokens and arbitrary environment values are excluded.
Missing TLS/proxy, wildcard ownership, backups, advisories and live kernel checks
remain `unverified`, never inferred from source or old documentation.

CI queries current Python advisories for runtime and Package Tracker gateway
locks on every supported Python version, and runs npm audit. Tool inputs remain
locked. Scan success means no **known reported advisories at scan time**, not
proof of safety. Artifact reports retain the package/version inventory. Run the
same checks against each installation's actual dependency set before rollout;
passing the new source lock does not clear an old installation.
