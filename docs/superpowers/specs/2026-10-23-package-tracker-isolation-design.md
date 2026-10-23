# Package Tracker isolation compatibility

## Goal and release boundary

Package Tracker must keep mail discovery, official carrier APIs, and public
tracking-page lookups when app-engine enforces per-command Unix identities and
loopback-only networking. The production `package-tracker` launch block remains
until the exact launch plan is separately approved after all gates pass.

## Alternatives considered

1. Give the Package Tracker unit direct public IP allowances. This cannot bind
   authorization to DNS names and leaves redirects and page subresources broad.
2. Run Playwright inside the Package Tracker unit through one public-web proxy.
   The proxy can filter destinations, but a browser exploit would share the
   mail credential and saved package state.
3. **Chosen:** run a separate unprivileged browser worker with its own proxy
   credential, keep the app unit on loopback, and give the app a separate
   fixed-host carrier proxy and a dedicated Atrium mail mTLS identity. This
   preserves page behavior without placing mail credentials in the browser UID.

## Components and interfaces

### Egress proxy

`app_engine/package_tracker_egress.py` serves an authenticated HTTP `CONNECT`
proxy on VPS loopback. The root-owned config supplies two independent random
tokens. `carrier` may connect only to `onlinetools.ups.com`,
`apis.fedex.com`, `apis.usps.com`, and `api.yunexpress.com`, port 443. `browser`
may connect to public HTTPS destinations on port 443. For both profiles, the
proxy rejects IP literals with non-global addresses, DNS answers containing any
non-global address, userinfo, malformed authorities, all other methods, and
all other ports. It resolves once, then connects to the selected vetted IP;
the client retains the requested hostname for TLS verification. Redirects and
subresources make fresh `CONNECT` requests and receive the same check. Failure
codes are 407 for absent/invalid proxy credentials, 403 for denied destination,
and 502 for resolution or upstream failure. Requests and credentials are never
logged together.

### Browser worker

`package-tracker/browser_worker.py` runs as a distinct systemd `DynamicUser`
with no app-data mount, no mail or carrier secrets, read-only code and toolchain,
`IPAddressDeny=any`, and loopback access only. It loads pinned Playwright and
Chromium installed by root in `/opt/package-tracker`; no unit downloads packages
or browsers. It accepts only authenticated loopback `POST /lookup` with a bounded
JSON body:

```json
{"kind":"carrier|order|status_url","carrier":"fedex","number":"...","url":"https://...","store_domain":"example.com"}
```

It returns the existing normalized lookup result or a bounded error. The app
process uses `RemoteBrowser` for this endpoint in production; local development
keeps the existing in-process browser path. The worker validates top-level
HTTPS URLs, blocks service workers, and configures Chromium to send loopback
requests through the authenticated browser proxy. The proxy provides the
independent private-address and DNS-rebinding boundary.

### App unit and carrier calls

The root broker recognizes only `package-tracker` `start` for additional
capabilities. It injects carrier proxy and browser-worker credentials into the
app's command plan through the existing stdin channel, mounts only the
Package Tracker mTLS client files read-only, and points at the preinstalled
Playwright path only for the browser worker. Other app IDs and preparation
phases receive none of these capabilities. The manifest starts `python3
server.py`; no `uv --with` runs inside a unit. Carrier `urllib` calls use an
explicit authenticated proxy opener, disable redirects, and keep normal CA and
hostname verification. Browser and mail calls remain distinct.

### Atrium mail

The existing Mac-side status gateway gains only three read-only GET shapes:
`/api/email/accounts`, `/api/email/accounts/{name}/inbox`, and
`/api/email/accounts/{name}/message/{uid}`. The gateway requires a new
`package-tracker-client` mTLS certificate for these routes, strips cookies,
and forwards the app's existing user-scoped `Authorization: Bearer` key to
Atrium. It does not permit the mail certificate on `/health`, nor the engine
certificate on mail. Package Tracker validates the gateway server certificate
using the private CA and presents its client certificate; the current insecure
loopback TLS bypass is removed. Existing local Atrium TLS is system-trusted.
The reverse SSH tunnel remains loopback on both machines.

### State and deployment

Back up the Mac's current 15 package records, settings, merchant memory, seen
message IDs, and credential file before migration. Install them in the VPS
app-engine scoped shared data directory with directory mode 0770 and file mode
0660 for the fixed `appengine` group, so a new DynamicUser UID can read them;
new writes retain that mode. Local, non-isolated files use mode 0600. Other
units cannot mount the Package Tracker directory. Do not copy mail message bodies. Preserve
the current `cyberpunk-tcg` process and configured SMTP. Keep the public app
allowlist unchanged until Package Tracker is explicitly authorized for public
access.

## Acceptance gates

Focused tests cover proxy credential separation, fixed hosts, private and
mixed DNS answers, DNS rebinding, redirects, subresources, service-worker and
non-HTTPS denial, mTLS route shape/method/client identity, unit capability
isolation, state permissions, and browser-worker input bounds. On the VPS,
prove a denied raw non-loopback connection from both UIDs, denied private
targets through the browser proxy, successful public browser and carrier
lookups, read-only mail with the existing API key, and persistence across a
worker restart. Repeat engine, game, app-store, SMTP, and both mTLS direction
smoke checks. Only then ask the owner to approve the exact new Package Tracker
launch fingerprint; remove its launch block and expand the public allowlist
only after that approval.
