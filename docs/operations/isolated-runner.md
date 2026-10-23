# Isolated managed-command runner

The runner uses a root-owned socket service to start one systemd transient unit
per managed command. App-engine connects as its `appengine` Unix user. The broker
checks kernel peer credentials, and each app unit has a distinct Unix UID,
scoped filesystem binds, non-loopback IP deny, and a `BindsTo` relationship to
its broker service. The broker socket is mode `0600` for `appengine` only.

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

The installer writes only root-owned runner code, `/etc/app-engine-runner.json`,
and two systemd units. It starts the socket but does not change app-engine's
`APP_ENGINE_PROCESS_ISOLATION=audit` setting. Re-running `--apply` from a newer
reviewed archive replaces each installed file atomically.

## Verify before enforce

Use a synthetic managed app to prove a child cannot connect to a controlled
external TCP receiver or read the engine private home or broker socket. Prove
that separate commands have separate UIDs, a loopback health service works,
command stop/disconnect removes the exact unit, and a prepared command can
write only to its scoped paths. Exercise the real production app paths and
environment in audit before changing the mode.

`cyberpunk-tcg` requires a fixed-origin TCGCSV refresh operation and a writable
mapping for its in-tree `data/` before enforce. Its room server is a child of
the web process and must remain functional on loopback. Review and approve its
exact app-engine launch plan as owner; the security rollout never grants that
approval automatically. Also verify configured SMTP, store catalog, both mTLS
directions, and the public app after the pilot.

Only after those gates pass, set `APP_ENGINE_ISOLATED_RUNNER_SOCKET` to
`/run/app-engine-runner.sock` and `APP_ENGINE_PROCESS_ISOLATION=enforce` in the
app-engine service configuration, then restart the service. A missing or
unhealthy socket must fail closed. For rollback, restore audit mode and restart
app-engine; leave the broker socket installed for diagnosis.
