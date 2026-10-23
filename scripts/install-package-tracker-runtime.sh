#!/usr/bin/env bash
# Install Package Tracker's prebuilt browser and tightly scoped VPS services.
set -Eeuo pipefail

usage() {
  echo 'Usage: install-package-tracker-runtime.sh --check|--install --cert-bundle DIR [--apps-source DIR]' >&2
  exit 64
}

mode=''
cert_bundle=''
apps_source='/home/appengine/personal-apps/package-tracker'
while (($#)); do
  case "$1" in
    --check|--install) [[ -z "$mode" ]] || usage; mode="$1"; shift ;;
    --cert-bundle) (($# >= 2)) || usage; cert_bundle="$2"; shift 2 ;;
    --apps-source) (($# >= 2)) || usage; apps_source="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$mode" && -n "$cert_bundle" ]] || usage

engine_source="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cert_bundle="$(cd "$cert_bundle" && pwd -P)"
apps_source="$(cd "$apps_source" && pwd -P)"
[[ "$(uname -s)" == Linux ]] || { echo 'Linux is required.' >&2; exit 78; }
[[ -f /etc/os-release ]] || { echo 'Cannot identify OS.' >&2; exit 78; }
grep -q '^ID=debian$' /etc/os-release || { echo 'Debian is required.' >&2; exit 78; }
for command in uv openssl systemctl systemd-analyze apt-get; do
  command -v "$command" >/dev/null || { echo "Missing: $command" >&2; exit 78; }
done
for name in ca.crt package-tracker-client.crt package-tracker-client.key; do
  [[ -f "$cert_bundle/$name" && ! -L "$cert_bundle/$name" ]] || {
    echo "Missing certificate bundle file: $name" >&2; exit 78;
  }
done
openssl verify -CAfile "$cert_bundle/ca.crt" "$cert_bundle/package-tracker-client.crt" >/dev/null
subject="$(openssl x509 -in "$cert_bundle/package-tracker-client.crt" -noout -subject -nameopt RFC2253)"
[[ "$subject" == 'subject=CN=package-tracker-client' ]] || {
  echo 'Unexpected Package Tracker certificate identity.' >&2; exit 78;
}
cert_public="$(openssl x509 -in "$cert_bundle/package-tracker-client.crt" -pubkey -noout | openssl sha256)"
key_public="$(openssl pkey -in "$cert_bundle/package-tracker-client.key" -pubout | openssl sha256)"
[[ "$cert_public" == "$key_public" ]] || { echo 'Certificate key does not match.' >&2; exit 78; }
for source in \
  "$engine_source/app_engine/package_tracker_egress.py" \
  "$engine_source/app_engine/isolation_broker.py" \
  "$engine_source/app_engine/isolated_units.py" \
  "$engine_source/deploy/package-tracker/app-engine-package-proxy.service" \
  "$engine_source/deploy/package-tracker/app-engine-package-browser.service" \
  "$apps_source/browser_worker.py" "$apps_source/page_lookup.py" "$apps_source/carriers.py"; do
  [[ -f "$source" && ! -L "$source" ]] || { echo "Missing source: $source" >&2; exit 78; }
done
id -g appengine >/dev/null || { echo 'Missing appengine group.' >&2; exit 78; }

echo 'Package Tracker runtime: Playwright 1.64.1 and its pinned Chromium in /opt/package-tracker.'
echo 'Services: loopback proxy 8788 and separate browser worker 8799.'
echo 'Policy: Package Tracker start only; existing production launch block remains.'
if [[ "$mode" == --check ]]; then
  exit 0
fi
[[ "$(id -u)" == 0 ]] || { echo 'Installation requires root.' >&2; exit 77; }

umask 077
config_dir='/etc/app-engine-package-tracker'
mtls_dir='/root/app-engine-package-tracker/mtls'
browser_dir='/usr/local/libexec/package-tracker-browser'
proxy_file='/usr/local/libexec/app-engine/package_tracker_egress.py'
broker_dir='/usr/local/libexec/app-engine/app_engine'
policy_file='/etc/app-engine-runner.json'
proxy_unit='/etc/systemd/system/app-engine-package-proxy.service'
browser_unit='/etc/systemd/system/app-engine-package-browser.service'
backup_dir="/root/app-engine-isolation-backups/$(date -u +%Y%m%d-%H%M%S)-package-tracker"
install -d -m 0700 -o root -g root "$backup_dir"
cp -p "$policy_file" "$backup_dir/app-engine-runner.json"
[[ ! -e "$proxy_unit" ]] || cp -p "$proxy_unit" "$backup_dir/app-engine-package-proxy.service"
[[ ! -e "$browser_unit" ]] || cp -p "$browser_unit" "$backup_dir/app-engine-package-browser.service"
for name in isolation_broker.py isolated_units.py package_tracker_egress.py; do
  [[ ! -e "$broker_dir/$name" ]] || cp -p "$broker_dir/$name" "$backup_dir/$name"
done

rollback() {
  local code=$?
  trap - ERR
  echo "Package Tracker setup failed; restoring service policy from $backup_dir." >&2
  systemctl stop app-engine-package-browser.service app-engine-package-proxy.service >/dev/null 2>&1 || true
  cp -p "$backup_dir/app-engine-runner.json" "$policy_file" || true
  if [[ -f "$backup_dir/app-engine-package-proxy.service" ]]; then
    cp -p "$backup_dir/app-engine-package-proxy.service" "$proxy_unit"
  else
    [[ ! -e "$proxy_unit" ]] || unlink "$proxy_unit"
  fi
  if [[ -f "$backup_dir/app-engine-package-browser.service" ]]; then
    cp -p "$backup_dir/app-engine-package-browser.service" "$browser_unit"
  else
    [[ ! -e "$browser_unit" ]] || unlink "$browser_unit"
  fi
  for name in isolation_broker.py isolated_units.py package_tracker_egress.py; do
    if [[ -f "$backup_dir/$name" ]]; then
      cp -p "$backup_dir/$name" "$broker_dir/$name"
    else
      [[ ! -e "$broker_dir/$name" ]] || unlink "$broker_dir/$name"
    fi
  done
  systemctl daemon-reload >/dev/null 2>&1 || true
  exit "$code"
}
trap rollback ERR

install -d -m 0700 -o root -g root "$config_dir" /root/app-engine-package-tracker
for role in browser carrier worker; do
  if [[ ! -e "$config_dir/$role-token" ]]; then
    openssl rand -hex 32 > "$config_dir/$role-token"
  fi
  chmod 0600 "$config_dir/$role-token"
  chown root:root "$config_dir/$role-token"
done
install -d -m 0710 -o root -g appengine "$mtls_dir"
for name in ca.crt package-tracker-client.crt package-tracker-client.key; do
  install -m 0640 -o root -g appengine "$cert_bundle/$name" "$mtls_dir/$name"
done
install -d -m 0755 -o root -g root "$browser_dir" "$broker_dir" /opt/package-tracker
install -m 0644 -o root -g root "$engine_source/app_engine/package_tracker_egress.py" "$proxy_file"
for name in isolation_broker.py isolated_units.py package_tracker_egress.py; do
  install -m 0644 -o root -g root "$engine_source/app_engine/$name" "$broker_dir/$name"
done
PYTHONPATH=/usr/local/libexec/app-engine /usr/bin/python3 -B -c 'import app_engine.isolation_broker'
for name in browser_worker.py page_lookup.py carriers.py; do
  install -m 0644 -o root -g root "$apps_source/$name" "$browser_dir/$name"
done
if [[ ! -x /opt/package-tracker/venv/bin/python ]]; then
  uv venv --python /usr/bin/python3 /opt/package-tracker/venv
fi
uv pip install --python /opt/package-tracker/venv/bin/python 'playwright==1.64.1'
PLAYWRIGHT_BROWSERS_PATH=/opt/package-tracker/browsers \
  /opt/package-tracker/venv/bin/python -m playwright install --with-deps chromium
chown -R root:root /opt/package-tracker
chmod -R go-w /opt/package-tracker
chmod -R a+rX /opt/package-tracker

install -m 0644 -o root -g root \
  "$engine_source/deploy/package-tracker/app-engine-package-proxy.service" "$proxy_unit"
install -m 0644 -o root -g root \
  "$engine_source/deploy/package-tracker/app-engine-package-browser.service" "$browser_unit"
systemd-analyze verify "$proxy_unit" "$browser_unit"
systemctl daemon-reload
systemctl enable --now app-engine-package-proxy.service app-engine-package-browser.service
systemctl is-active --quiet app-engine-package-proxy.service
systemctl is-active --quiet app-engine-package-browser.service

python3 - "$policy_file" "$mtls_dir" "$config_dir" <<'PY'
import json, os, sys, tempfile
from pathlib import Path
policy, mtls, config = map(Path, sys.argv[1:])
value = json.loads(policy.read_text())
expected = {
    'mtls_dir': str(mtls),
    'carrier_token_file': str(config / 'carrier-token'),
    'worker_token_file': str(config / 'worker-token'),
    'carrier_proxy_port': 8788,
    'browser_worker_port': 8799,
}
if 'package_tracker' in value and value['package_tracker'] != expected:
    raise SystemExit('A different Package Tracker policy already exists.')
value['package_tracker'] = expected
descriptor, path = tempfile.mkstemp(dir=policy.parent, prefix='.app-engine-runner-')
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(path, policy)
finally:
    if os.path.exists(path): os.unlink(path)
PY

trap - ERR
echo "Package Tracker dependencies and scoped services installed. Backup: $backup_dir"
echo 'The production Package Tracker launch block has not been changed.'
