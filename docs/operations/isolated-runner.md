# Isolated managed-command runner

The runner uses a root-owned socket service to start one systemd transient unit
per managed command. App-engine connects as its `appengine` Unix user. The broker
checks kernel peer credentials, and each app unit has a distinct Unix UID,
a read-only app-source bind, writable scoped data/build binds, non-loopback IP
deny, and a `BindsTo` relationship to its broker service. The broker socket is
mode `0600` for `appengine` only.

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
`/etc/app-engine-runner.json`, and their systemd units. It starts the socket and
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
`sources/tcgcsv-pricing.json` into the runtime directory. Set that scoped
directory and its `sources/` subdirectory to group `appengine` and mode 0775;
the source remains read-only to the isolated unit. Its room server is a child
of the web process and must remain functional on loopback.

The VPS also has `package-tracker`, whose current `uv --with playwright`
launch, carrier APIs, Atrium mail access, and browser page lookups require a
separate compatibility design. Keep the global mode at `audit`. After the
owner approves the exact changed `cyberpunk-tcg` launch plan, set
`APP_ENGINE_ISOLATED_APP_IDS=cyberpunk-tcg` to enforce only that pilot; the
other app stays in audit until its own gates pass. A missing broker is a
launch failure for the pilot, with no host-process fallback. Verify the pilot
UI, price refresh, room tables, data persistence, crash/stop cleanup,
configured SMTP, store catalog, both mTLS directions, and rejected
non-loopback egress.

Only after every managed app has a verified network/toolchain path should the
global `APP_ENGINE_PROCESS_ISOLATION=enforce` setting replace the pilot. For
rollback, remove the pilot app ID and restart app-engine in audit mode; leave
the broker socket and gateway installed for diagnosis.
