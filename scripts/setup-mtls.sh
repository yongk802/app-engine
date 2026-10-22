#!/usr/bin/env bash
# Create one private CA and distinct client/server identities for the service link.
set -euo pipefail
umask 077

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: bash scripts/setup-mtls.sh NEW_CERT_DIR MCP_DNS_NAME STORE_DNS_NAME [ATRIUM_STATUS_DNS_NAME]" >&2
  exit 2
fi
directory=$1
mcp_host=$2
store_host=$3
atrium_host=${4:-}
for host in "$mcp_host" "$store_host" ${atrium_host:+"$atrium_host"}; do
  if [[ ! $host =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]]; then
    echo "DNS names must contain only letters, digits, dots, and hyphens" >&2
    exit 2
  fi
done
if [[ -e $directory ]]; then
  echo "Certificate directory already exists; choose a new directory for rotation" >&2
  exit 2
fi
key_directory="${directory}.ca-offline"
if [[ -e $key_directory ]]; then
  echo "Offline CA key directory already exists; choose a new certificate directory" >&2
  exit 2
fi
mkdir -p "$directory"
chmod 700 "$directory"
directory=$(cd "$directory" && pwd -P)
mkdir -p "$key_directory"
chmod 700 "$key_directory"
key_directory=$(cd "$key_directory" && pwd -P)
if [[ $directory == *[[:space:]\"\\]* ]]; then
  echo "Certificate directory path cannot contain whitespace, quotes, or backslashes" >&2
  exit 2
fi

openssl req -x509 -newkey rsa:3072 -noenc -sha256 -days 3650 \
  -keyout "$key_directory/ca.key" -out "$directory/ca.crt" \
  -subj '/CN=App Engine private service CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash' >/dev/null 2>&1

issue() {
  local name=$1 kind=$2 dns=${3:-}
  openssl req -new -newkey rsa:3072 -noenc -sha256 \
    -keyout "$directory/$name.key" -out "$directory/$name.csr" \
    -subj "/CN=$name" >/dev/null 2>&1
  local extension="$directory/$name.ext"
  if [[ $kind == server ]]; then
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\nsubjectAltName=DNS:%s\n' "$dns" > "$extension"
  else
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' > "$extension"
  fi
  openssl x509 -req -in "$directory/$name.csr" -CA "$directory/ca.crt" \
    -CAkey "$key_directory/ca.key" -CAcreateserial -out "$directory/$name.crt" \
    -days 397 -sha256 -extfile "$extension" >/dev/null 2>&1
  rm "$directory/$name.csr" "$extension"
}

issue atrium-client client
issue app-engine-client client
issue app-engine-mcp server "$mcp_host"
issue app-store server "$store_host"
if [[ -n $atrium_host ]]; then
  issue atrium-status server "$atrium_host"
fi

# Keep each runtime identity in its own copyable directory. Never install the
# entire staging directory on one service host.
for role in engine atrium store mcp-gateway; do
  mkdir -m 700 "$directory/$role"
  cp "$directory/ca.crt" "$directory/$role/ca.crt"
done
mv "$directory/app-engine-client.crt" "$directory/app-engine-client.key" "$directory/engine/"
mv "$directory/atrium-client.crt" "$directory/atrium-client.key" "$directory/atrium/"
mv "$directory/app-store.crt" "$directory/app-store.key" "$directory/store/"
mv "$directory/app-engine-mcp.crt" "$directory/app-engine-mcp.key" "$directory/mcp-gateway/"
if [[ -n $atrium_host ]]; then
  mkdir -m 700 "$directory/atrium-status-gateway"
  cp "$directory/ca.crt" "$directory/atrium-status-gateway/ca.crt"
  mv "$directory/atrium-status.crt" "$directory/atrium-status.key" "$directory/atrium-status-gateway/"
fi

secret=$(openssl rand -hex 32)
printf 'APP_ENGINE_MCP_PROXY_SECRET=%s\n' "$secret" > "$directory/engine/app-engine-mcp.env"
if [[ -n $atrium_host ]]; then
  printf 'APP_ENGINE_ATRIUM_STATUS_URL=https://%s/health\n' "$atrium_host" >> "$directory/engine/app-engine-mcp.env"
fi
cat > "$directory/mcp-gateway/nginx-app-engine-mcp.conf" <<EOF
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name $mcp_host;
    ssl_certificate $directory/mcp-gateway/app-engine-mcp.crt;
    ssl_certificate_key $directory/mcp-gateway/app-engine-mcp.key;
    ssl_client_certificate $directory/mcp-gateway/ca.crt;
    ssl_verify_client on;
    ssl_verify_depth 1;

    location = /api/mcp {
        if (\$ssl_client_s_dn != "CN=atrium-client") { return 403; }
        proxy_pass http://127.0.0.1:8770;
        proxy_set_header Host $mcp_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For 127.0.0.1;
        proxy_set_header X-App-Engine-MTLS-Proxy $secret;
        proxy_set_header Authorization \$http_authorization;
        proxy_set_header Cookie "";
    }
    location / { return 404; }
}
EOF
cat > "$directory/engine/outbound-mtls.json" <<EOF
{
  "https://$store_host": {
    "ca_file": "$directory/engine/ca.crt",
    "cert_file": "$directory/engine/app-engine-client.crt",
    "key_file": "$directory/engine/app-engine-client.key"
  }$(if [[ -n $atrium_host ]]; then printf ',\n  "https://%s": {\n    "ca_file": "%s/engine/ca.crt",\n    "cert_file": "%s/engine/app-engine-client.crt",\n    "key_file": "%s/engine/app-engine-client.key"\n  }' "$atrium_host" "$directory" "$directory" "$directory"; fi)
}
EOF
if [[ -n $atrium_host ]]; then
cat > "$directory/atrium-status-gateway/nginx-atrium-status.conf" <<EOF
server {
    listen 443 ssl;
    server_name $atrium_host;
    ssl_certificate $directory/atrium-status-gateway/atrium-status.crt;
    ssl_certificate_key $directory/atrium-status-gateway/atrium-status.key;
    ssl_client_certificate $directory/atrium-status-gateway/ca.crt;
    ssl_verify_client on;
    ssl_verify_depth 1;

    location = /health {
        if (\$ssl_client_s_dn != "CN=app-engine-client") { return 403; }
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $atrium_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Cookie "";
    }
    location / { return 404; }
}
EOF
fi
echo "Created service certificate bundle in $directory"
echo "Keep $key_directory/ca.key offline; do not mount or copy it into a service host."
echo "Install only the role directory needed by each service. Configure app-engine with engine/app-engine-mcp.env and APP_ENGINE_OUTBOUND_MTLS_CONFIG=$directory/engine/outbound-mtls.json."
