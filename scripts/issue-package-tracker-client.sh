#!/usr/bin/env bash
# Issue a narrow client identity from the existing offline app-engine CA.
set -Eeuo pipefail

usage() {
  echo 'Usage: issue-package-tracker-client.sh --check|--issue --ca-cert FILE --ca-key FILE --output DIR' >&2
  exit 64
}

mode=''; ca_cert=''; ca_key=''; output=''
while (($#)); do
  case "$1" in
    --check|--issue) [[ -z "$mode" ]] || usage; mode="$1"; shift ;;
    --ca-cert) (($# >= 2)) || usage; ca_cert="$2"; shift 2 ;;
    --ca-key) (($# >= 2)) || usage; ca_key="$2"; shift 2 ;;
    --output) (($# >= 2)) || usage; output="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$mode" && -n "$ca_cert" && -n "$ca_key" && -n "$output" ]] || usage
[[ -f "$ca_cert" && -f "$ca_key" && ! -L "$ca_cert" && ! -L "$ca_key" ]] || {
  echo 'CA certificate and offline key are required.' >&2; exit 78;
}
[[ ! -e "$output" ]] || { echo 'Output directory already exists.' >&2; exit 78; }
openssl x509 -in "$ca_cert" -noout >/dev/null
openssl pkey -in "$ca_key" -noout >/dev/null
echo 'Will issue CN=package-tracker-client with clientAuth only and a one-year lifetime.'
echo 'Only the client role directory should be copied to the VPS; keep the CA key offline.'
if [[ "$mode" == --check ]]; then
  exit 0
fi

umask 077
mkdir -m 0700 "$output"
cleanup() {
  local code=$?
  trap - ERR
  python3 - "$output" <<'PY'
from pathlib import Path
import sys
directory = Path(sys.argv[1])
for name in ('package-tracker-client.key', 'package-tracker-client.crt',
             'package-tracker-client.csr', 'ca.crt', 'extensions.cnf'):
    (directory / name).unlink(missing_ok=True)
directory.rmdir()
PY
  exit "$code"
}
trap cleanup ERR
openssl req -new -newkey rsa:3072 -noenc -sha256 \
  -subj '/CN=package-tracker-client' \
  -keyout "$output/package-tracker-client.key" \
  -out "$output/package-tracker-client.csr" >/dev/null 2>&1
cat > "$output/extensions.cnf" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth
EOF
serial="$(openssl rand -hex 16)"
openssl x509 -req -sha256 -days 365 \
  -in "$output/package-tracker-client.csr" \
  -CA "$ca_cert" -CAkey "$ca_key" -set_serial "0x$serial" \
  -extfile "$output/extensions.cnf" \
  -out "$output/package-tracker-client.crt" >/dev/null 2>&1
cp "$ca_cert" "$output/ca.crt"
chmod 0600 "$output"/*
openssl verify -CAfile "$output/ca.crt" -purpose sslclient \
  "$output/package-tracker-client.crt" >/dev/null
python3 - "$output" <<'PY'
from pathlib import Path
import sys
directory = Path(sys.argv[1])
(directory / 'package-tracker-client.csr').unlink()
(directory / 'extensions.cnf').unlink()
PY
trap - ERR
echo "Issued Package Tracker client role at $output"
