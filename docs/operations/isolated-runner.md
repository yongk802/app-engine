# Isolated managed-command runner

The runner uses a root-owned socket service to start one systemd transient unit
per managed command. App-engine connects as its `appengine` Unix user. The broker
checks kernel peer credentials, and each app unit has a distinct Unix UID,
a read-only app-source bind, writable scoped data/build binds, non-loopback IP
deny, and a `BindsTo` relationship to its broker service. The broker socket is
mode `0600` for `appengine` only.

Apps with a host multiplayer manifest also start their app-supplied rules
module through the broker when isolation is enforced. That unit gets the same
network and identity boundary, the root-installed room server code read-only,
and only its app's existing room state at
`APP_ENGINE_STATE_DIR/multiplayer/<app-id>` writable. An external multiplayer
override is rejected for an enforced app. The pilot requires an empty
`APP_ENGINE_WEBHOOK_ALLOWED_ORIGINS` setting; if callback origins are configured,
the room service fails closed until a scoped notification gateway is installed.

## Install the socket without enabling enforcement

Use the exact reviewed app-engine commit after its PR checks pass. Extract it
into a root-owned directory; the installer refuses code from an appengine-owned
working tree. Replace `<reviewed-commit>` with the full commit SHA.

```bash
sudo install -d -m 0755 /root/app-engine-runner-release
git -C /home/appengine/app-engine archive <reviewed-commit> | sudo tar -x -C /root/app-engine-runner-release
sudo python3 /root/app-engine-runner-release/scripts/install-isolated-runner.py \
  --apps-root /home/appengine/personal-apps \
  --state-root /home/appengine/state/runtime --check
sudo python3 /root/app-engine-runner-release/scripts/install-isolated-runner.py \
  --apps-root /home/appengine/personal-apps \
  --state-root /home/appengine/state/runtime --apply
systemctl status app-engine-runner.socket
```

The installer writes root-owned runner and fixed-origin TCGCSV gateway code,
the room server modules, `/etc/app-engine-runner.json`, and their systemd units.
The multiplayer root defaults to the `multiplayer/` sibling of the supplied
runtime state root; use `--multiplayer-root` for a different state layout.
The broker deliberately has no root DAC bypass capabilities. If the engine
home is private (`0700`), grant only root search access to that ancestor before
installing: `sudo setfacl -m u:root:--x /home/appengine`. The installer checks
broker traversal with DAC bypass removed and reports this prerequisite; the
engine user keeps its existing home access. Keep the ACL when upgrading.
It starts the socket and
the loopback-only gateway on port 18792 but does not change app-engine's
`APP_ENGINE_PROCESS_ISOLATION=audit` setting. The gateway accepts GET only for
TCGCSV's timestamp, category, group, product, and price paths; it rejects
redirects and responses over 20 MB. Check `curl -fsS http://127.0.0.1:18792/health`
and verify an unlisted path returns 404. Re-running `--apply` from a newer
reviewed archive replaces each installed file atomically.

## Verify before enforce

Use a synthetic managed app to prove a child cannot connect to a controlled
external TCP receiver or read the engine private home or broker socket. Prove
that separate commands have separate UIDs, a loopback health service works,
command stop/disconnect removes the exact unit, and a prepared command can
write only to its scoped paths. Exercise the real production app paths and
environment in audit before changing the mode.

The staged `cyberpunk-tcg` manifest runs `/usr/bin/python3 scripts/serve.py`
with `NIGHT_CITY_TCGCSV_GATEWAY=1`. The game reads its catalog from the
read-only app source, writes price/cache files under `APP_DATA_DIR`, serves
only its two public price files from there, and reaches the fixed gateway over
loopback. Back up its existing source `data/` and runtime directory before
copying any existing `prices.json`, `top20-prices.html`, and
`sources/tcgcsv-pricing.json` into the runtime directory. Before the pilot
switch, migrate **all** existing scoped runtime state, including any `rooms/`
created by the game's backend. Also preserve the host room service's existing
state at `APP_ENGINE_STATE_DIR/multiplayer/cyberpunk-tcg/`; do not move it to
the game's runtime directory. Audit-mode processes may have created
directories at 0755 and files at 0600; a new dynamic UID cannot use those.
Stop the pilot app and room service, back up both state directories, reject
symlinks in them, then set directories to owner/group `appengine:appengine`
and mode 0770, including nested runtime and host room directories, and regular
files to group `appengine` and mode 0660. The scoped directory must not grant
traversal to unrelated Unix identities: DynamicUser UIDs are recycled after
units stop. Check modes and contents before restarting. The
source remains read-only to the isolated units. New room state uses
group-readable atomic writes so it survives subsequent dynamic UID changes.
Before switching the pilot, check the effective engine environment has no
approved webhook origins and its persisted room state has no webhook seats.
The 2026-10-23 production snapshot had neither configured origins nor webhook
seats; a later deployment must recheck both conditions.

During the Night City Table pilot, keep the global mode at `audit` and set
`APP_ENGINE_ISOLATED_APP_IDS=cyberpunk-tcg` after the owner approves that exact
changed launch plan. A missing broker is a launch failure for the pilot, with
no host-process fallback. Verify the pilot
UI, price refresh, room tables, data persistence, crash/stop cleanup,
configured SMTP, store catalog, both mTLS directions, and rejected
non-loopback egress. Verify the host multiplayer route, including the remote
player API, starts a broker unit and never starts a host-UID Node process.

For global enforcement while Package Tracker remains blocked, set
`APP_ENGINE_BLOCKED_APP_IDS=package-tracker` and
`APP_ENGINE_PROCESS_ISOLATION=enforce`. Its managed preview must reject with
`app_launch_blocked`. On the production VPS, `APP_ENGINE_PUBLIC_APPS` omits
Package Tracker, so the open route returns 404 before lifecycle policy; if it
is hosted later while still blocked, open returns `app_launch_blocked`. Its
source and data stay in place.
Verify no other process manifest exists, and retain Night City Table's exact
standing approval. The Package Tracker compatibility setup below does not
remove its block. Remove the block only after the production gates pass and
the owner approves the new exact launch-plan fingerprint.
For rollback, restore the prior service override and restart app-engine in
audit mode; leave the broker socket and gateway installed for diagnosis.

## Stage Package Tracker compatibility while launch stays blocked

The [Package Tracker design](../superpowers/specs/2026-10-23-package-tracker-isolation-design.md)
uses a separately identified browser worker, an authenticated loopback CONNECT
proxy, a fixed-origin carrier profile, and a dedicated mTLS client for Atrium's
read-only mail routes. The browser receives no mail or carrier credentials.
The broker grants these capabilities only to Package Tracker's start unit.

Issue the role certificate on the Mac from the offline app-engine mTLS CA. The
issuer's `--check` validates the requested paths and identity without writing.
Keep the CA signing key on the Mac. Transfer only `ca.crt`,
`package-tracker-client.crt`, and `package-tracker-client.key` to a root-only
staging directory on the VPS. Avoid displaying the key or bearer token in logs.

```bash
bash scripts/issue-package-tracker-client.sh --check \
  --ca-cert /Users/yongkim/.config/app-engine-mtls-v2/ca.crt \
  --ca-key /Users/yongkim/.config/app-engine-mtls-v2.ca-offline/ca.key \
  --output /Users/yongkim/.config/atrium/package-tracker-mtls-v1
```

On the VPS, extract reviewed app-engine and personal-apps commits into
root-owned release directories. With the three certificate files staged in
`/root/package-tracker-cert-staging`, check and install the pinned Playwright
1.64.1 runtime, Chromium, service units, and broker policy:

```bash
sudo bash scripts/install-package-tracker-runtime.sh --check \
  --cert-bundle /root/package-tracker-cert-staging \
  --apps-source /root/personal-apps-release/package-tracker
sudo bash scripts/install-package-tracker-runtime.sh --install \
  --cert-bundle /root/package-tracker-cert-staging \
  --apps-source /root/personal-apps-release/package-tracker
```

The installer backs up `/etc/app-engine-runner.json` and the installed broker
modules, replaces them from the reviewed release, installs root-owned runtime
code, and starts services on loopback ports 8788 and 8799. It prints
the backup path. Keep that backup for rollback. Install the read-only mail
routes on the Mac's existing health gateway with
`install-package-tracker-mail-gateway.py --check`, then `--install`, using its
existing Nginx config, prefix, binary, and local Atrium CA. The gateway
installer tests a candidate config before reloading and saves a timestamped
backup. The `/health` route remains restricted to `app-engine-client`; the mail
routes require `package-tracker-client` plus a user API key with email scope.

Back up the Mac's `~/.local/share/package-tracker` data before migration. Copy
only the app's packages, seen IDs, and credential files to its scoped runtime
data directory; do not start the app from source to perform migration. Reject
symlinks, set all data directories to `appengine:appengine` mode 0770 and files
to group `appengine` mode 0660, and verify the package count and seen-ID count.
The app's atomic writes retain these modes across changing DynamicUser IDs.

Before seeking exact launch approval, prove the proxy rejects unauthenticated,
private, and unlisted-carrier CONNECT requests; prove the browser worker
cannot use a direct external socket and can load a public HTTPS tracking page
through the proxy; prove the dedicated mTLS mail route rejects the other client
identity and non-GET requests, accepts the scoped mail API key, and keeps
`/health` working. Check that Night City Table, SMTP, and the existing app-store
and Atrium mTLS paths continue working. A failed gate leaves Package Tracker
blocked; restore the saved policy and Nginx config when rolling back.
