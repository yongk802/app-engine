# Production security for app-engine

**Date:** 2026-10-22
**Status:** Approved design; stage gates apply to each rollout

## Boundary

The standalone app-engine is a separate deployment from Atrium. Its public
listener stays behind a local TLS proxy. Atrium's command isolation PRD is the
model for classifying subprocesses, network services, and explicit host
administration. Neither repository may claim command isolation until its actual
runner passes a controlled egress probe.

App-engine currently executes manifest install, build, and start steps as host
subprocesses. Those processes can reach the network and share the service
account's files. The production rollout must show this state accurately, avoid
silent fallback, and introduce isolation one execution family at a time.

## Release slices

1. **Close proven public-boundary leaks.** A browser session or engine control
   capability must never be forwarded to an app backend. Only loopback bind is
   accepted for the standalone HTTP server, and a public origin outside
   `.localhost` requires HTTPS. Backend redirects remain responses to the
   caller, never engine-side requests to a new destination. Tests exercise
   owner and player cookies, managed and legacy app proxies, and startup refusal.
2. **Mutually authenticated service traffic.** A dedicated reverse proxy endpoint
   requires a client certificate from the app-engine CA before forwarding MCP
   requests. App-engine additionally requires its independent bearer key and
   recognizes the proxy's verified-client assertion only from the loopback
   peer. Atrium's MCP HTTP transport accepts a server CA, client certificate,
   and key configured as paths and presents them on every request. App-engine
   outbound HTTPS clients use the same three-file profile only for explicitly
   configured origins, so a credential is never offered to an arbitrary store,
   redirect, or attacker-controlled host. Certificate generation, proxy
   configuration, Atrium configuration, rotation, and negative handshakes are
   documented as a short setup path. This covers Atrium to app-engine and
   app-engine to an Atrium or app-server HTTP endpoint when one is configured.
3. **Isolate app commands.** Inventory all process launchers, including managed
   preparation/start, multiplayer Node service, and local AI setup. Add an
   operator-controlled `legacy` / `audit` / `enforce` policy with audit-only
   evidence first. Enforce mode admits a verified runner with no unrestricted
   network, host secrets, or daemon socket and fails closed if it is absent.
   Provide approved package downloads through a separate narrow network
   operation. Pilot against real production app workflows before enabling
   enforce on the public server. Each family requires positive workflow and
   negative egress tests before progression.

## Interfaces

- `forward_app_headers(headers) -> tuple[tuple[str, str], ...]`: removes the
  `app_engine_session` cookie and engine control headers while preserving other
  app-specific headers. No secret value enters logs.
- `bind_is_allowed(host, public, accounts) -> str | None`: returns a startup
  refusal for any non-loopback bind and for a non-local plaintext public origin.
- `McpTlsProfile.from_config(config) -> ssl.SSLContext | None`: validates the
  three configured paths, verifies server identity, and loads a client chain;
  incomplete or unreadable profiles raise a configuration error. HTTP transport
  does not downgrade to an unverified TLS client.
- `OutboundTlsPolicy.for_origin(url) -> ssl.SSLContext | None`: returns a client
  identity only for an exact configured HTTPS origin; unexpected destinations
  receive no client certificate. Invalid paths and scheme fail closed.
- `ProcessIsolationPolicy.decide(plan, runner_evidence) -> decision`: records
  the selected execution class; enforce mode refuses unavailable or stale
  evidence and never runs a local fallback.

## Acceptance

Each slice has a failing regression test before implementation, focused tests,
and an independent security review. mTLS acceptance includes a successful
request with the expected client certificate and bearer key, rejection without
the certificate, rejection with an untrusted certificate, rejection without the
bearer key, and server certificate validation on outbound requests. Process
isolation acceptance requires a real runner probe against a controlled receiver
and normal app launch on the same runner. Deployment changes are applied only
after the tested candidate is reviewable; there is no blanket production
restart or enforcement flip as part of writing code.
