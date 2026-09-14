# Deploying a public app-engine on a Hostinger VPS

Verified against `srv1242099.hstgr.cloud` (2026-09-13): the VPS name resolves,
Hostinger publishes a **wildcard** for it at any depth
(`anything.play.srv1242099.hstgr.cloud` → the same address), `hstgr.cloud` is on
the Public Suffix List (so the `srv…` name behaves like a domain of your own for
cookies and certificate rate limits), and ports 80/443 are already served by
**nginx for the AMP game panel** with a Let's Encrypt certificate for the base
name only. Port 8770 is not reachable from outside, which is how it should stay.

Nothing about DNS needs to change. The layout:

| Name | Serves |
|---|---|
| `srv1242099.hstgr.cloud` | AMP, untouched |
| `play.srv1242099.hstgr.cloud` | the app-engine launcher (`APP_ENGINE_PUBLIC_ORIGIN`) and the player API games connect to |
| `<app>.play.srv1242099.hstgr.cloud` | each app on its own origin (the default `subdomain` layout) |

## 1. Install

```bash
# as root on the VPS
apt-get install -y python3-venv python3-pip nodejs npm certbot python3-certbot-nginx git
useradd --system --create-home --shell /usr/sbin/nologin appengine
sudo -u appengine -H bash -c '
  cd ~ && git clone https://github.com/yongk802/app-engine.git && git clone https://github.com/yongk802/personal-apps.git
  cd app-engine && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  cd ../personal-apps/cyberpunk-tcg && python3 scripts/import_cards.py && python3 scripts/import_printing_art.py'
node --version   # 18 or newer
```

## 2. Run the engine as a service

`/etc/systemd/system/app-engine.service`:

```ini
[Unit]
Description=app-engine (public origin)
After=network.target

[Service]
User=appengine
WorkingDirectory=/home/appengine/app-engine
Environment=APP_ENGINE_APPS_DIR=/home/appengine/personal-apps
Environment=APP_ENGINE_STATE_DIR=/home/appengine/state
Environment=APP_ENGINE_PUBLIC_ORIGIN=https://play.srv1242099.hstgr.cloud
Environment=APP_ENGINE_TRUST_PROXY=1
Environment=APP_ENGINE_HOST=127.0.0.1
Environment=APP_ENGINE_PORT=8770
# Optional: Environment=APP_ENGINE_ADMIN_SECRET=...   (otherwise one is minted and logged once)
ExecStart=/home/appengine/app-engine/.venv/bin/python engine.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now app-engine
journalctl -u app-engine -n 20     # shows the public origin and, first time, the admin secret
```

## 3. Front it with the nginx that is already there

`/etc/nginx/sites-available/app-engine` (then symlink into `sites-enabled` and
`nginx -t && systemctl reload nginx`):

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name play.srv1242099.hstgr.cloud *.play.srv1242099.hstgr.cloud;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name play.srv1242099.hstgr.cloud *.play.srv1242099.hstgr.cloud;
    # certbot fills in ssl_certificate / ssl_certificate_key below

    client_max_body_size 2m;
    location / {
        proxy_pass http://127.0.0.1:8770;
        proxy_http_version 1.1;
        proxy_set_header Host $host;              # the engine routes apps by host name
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 60s;
    }
}
```

Certificates: one certificate listing the launcher name and every app name
you host (HTTP-01, no DNS plugin; renewals are automatic through certbot's
timer). Add `-d` entries and re-run with `--expand` whenever you add an app:

```bash
certbot --nginx --expand \
  -d play.srv1242099.hstgr.cloud \
  -d cyberpunk-tcg.play.srv1242099.hstgr.cloud
```

A wildcard certificate (`*.play.srv1242099.hstgr.cloud`) would need the DNS-01
challenge, which requires control of the zone; Hostinger owns `hstgr.cloud`, so
per-name certificates are the right tool here. Should you later point a domain
of your own at the VPS, the same configuration works with your names, and a
DNS-01 wildcard becomes possible.

## 4. First sign-in and players

1. `https://play.srv1242099.hstgr.cloud/admin` — the admin secret from the
   service log (or the one you configured). Open Night City Table once and
   approve its launch plan; that approval stands for players.
2. Invite players from the launcher's **Players** panel or
   `sudo -u appengine APP_ENGINE_STATE_DIR=/home/appengine/state APP_ENGINE_PUBLIC_ORIGIN=https://play.srv1242099.hstgr.cloud /home/appengine/app-engine/.venv/bin/app-engine-players invite --username rook --apps cyberpunk-tcg`.
3. Players open the game on their own computers, pick **Hostinger VPS** in the
   Table server panel (the game's `app.json` lists
   `https://play.srv1242099.hstgr.cloud`), sign in, and meet each other.

## Checks

```bash
curl -sI https://play.srv1242099.hstgr.cloud/ | head -1            # 303 → /admin
curl -s https://play.srv1242099.hstgr.cloud/api/engine              # {"name":"app-engine",...}
curl -s -X POST https://play.srv1242099.hstgr.cloud/api/players/sign-in \
  -H 'Content-Type: application/json' -d '{"username":"x","password":"y"}'   # 403 with an error body
```

The release checklist's public-origin section (`docs/release-testing.md`)
covers the full walk-through with two computers.
