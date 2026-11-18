# Installation inventory — 2026-11-18

Read-only review by the app-engine audit remediation task. Owner: Yong Kim.
This document separates live evidence from the candidate source fixes. No
production service was restarted or modified during inventory.

| Installation | Live evidence | Security state / remediation owner |
|---|---|---|
| .201, Yongs-MacBook-Pro | loopback 8770 reports 0.3.4; listener working directory `~/git/app-engine`, checkout `0b9b198` | Older than merged audit release. Effective environment and service activation require further reconciliation. Owner: Yong Kim; plan supported update after acceptance. |
| .221, MACXKGKPY492Q | loopback 8770 reports 0.3.5; serving worktree clean at `d42fd186ae77b5760d8880216f6dbc89dbe08726`; launchd plist selects `APP_ENGINE_PROCESS_ISOLATION=audit` | Intentional trusted macOS RPMCC test host, outside Linux containment. State directory 0750, parent 0755; unrelated users cannot traverse state. No publisher policy configured in the reviewed plist. Owner: Yong Kim. |
| VPS, srv1242099 | active app-engine service reports 0.2.1; checkout `2f64461a3a2cc029503481d2c4a0c38767c6120e`; no tracked modifications (untracked worktrees preserved) | Production deployment predates the audit merge. Requires separately reviewed rollout/migration and acceptance. Owner: Yong Kim. |

VPS observed service controls: `User=appengine`, `NoNewPrivileges=yes`,
`PrivateTmp=yes`, `ProtectSystem=full`, `ProtectHome=no`, empty capability
bounding set. These service controls alone do not prove child containment.

Further deployment evidence and dated advisory results are appended after the
review completes. Unknowns remain explicitly unverified; neither repository
merge nor a Mac browser run proves the production installation is hardened.

## VPS transport, runner, keys and backup evidence

* Running process environment (read without exporting secrets) selects
  `APP_ENGINE_PROCESS_ISOLATION=enforce`, public origin
  `https://play.srv1242099.hstgr.cloud`, and listener `127.0.0.1:8770`.
  This is an old runner implementation; the new resource policy is absent.
* `/etc/app-engine-runner.json` is root-owned 0600, selecting the appengine
  user/group, app source under `/home/appengine/personal-apps`, runtime state
  `/home/appengine/state/runtime`, and multiplayer state
  `/home/appengine/state/multiplayer`. It has no `resource_limits` or
  `service_ports` fields. Root-installed broker/unit source files are 0644;
  no app units were running during this observation. Actual new kernel
  containment and bounded storage on this host remain **unverified**.
* `nginx -T` succeeds. The browser proxy's active config in
  `/etc/nginx/conf.d/play.srv1242099.hstgr.cloud.conf` declares a 2 MiB
  request body cap, preserves `$host`, sets forwarded protocol to HTTPS and
  forwarded address to `$remote_addr`, and forwards to loopback 8770.
  TLS protocol directives permit TLS 1.2/1.3. The separate MCP proxy requires
  client certificates and pins its forwarded host; its body limit was not
  separately established. Live TLS 1.3 certificate verification succeeds for
  the launcher. The certificate expires 2027-01-21 and covers launcher,
  cyberpunk-tcg and package-tracker names; it is **not a wildcard certificate**.
* Launcher, cyberpunk-tcg and a synthetic wildcard DNS name resolve to the VPS
  IPv4/IPv6 addresses. This proves resolution, not ownership of Hostinger's
  DNS zone or provider account. **Wildcard ownership remains unverified**.
* TLS private key metadata: Let's Encrypt key root-owned 0600. Reviewed
  engine/store/MCP mTLS `.key` files are 0600, owned by their configured
  installation accounts (UIDs 999/995/501 respectively); account-role alignment,
  rotation and ancestor access require operator review. `/etc/app-engine.env`
  is root-owned 0600. No private key bytes or password values were retrieved.
* State is appengine-owned 0750. Backup encryption, retention, operator access,
  off-host copies and restoration have **not** been established. These checks
  are not replaced by permissions on the active state tree.

## Dated dependency checks

[Machine-readable, deduplicated findings](security-advisories-2026-11-18.json)
retain package versions, advisory IDs and fixed versions. Current candidate
runtime, Package Tracker gateway dependencies, uv 0.13.4 build-tool input, and
npm lock report no known advisories. CI now repeats the checks for every
supported Python version; this does not guarantee future absence of advisories.

Installed .201 Python environment reports no known advisories, but its separate
Homebrew uv 0.13.1 is affected by
[GHSA-hq85-5cpj-mr62](https://github.com/astral-sh/uv/security/advisories/GHSA-hq85-5cpj-mr62).
CI's prior 0.13.1 pin was replaced with 0.13.4 and added to advisory scanning.
The task uses a scoped patched uv rather than changing the owner's global tool.

The .221 installed environment has pip 25.0.1 with six unique advisory IDs.
The VPS has nine unique IDs across certifi 2026.7.22, pip 25.1.1 and uv 0.12.13;
the scanner returned 16 records, including duplicates, which are not counted
as distinct issues. Some advisories are platform-specific (for example the uv
wheel extraction issue is Windows-only), and packaging-tool findings do not
establish an exploitable serving endpoint. The compromised-CA findings in
[certifi](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)
and [uv](https://github.com/astral-sh/uv/security/advisories/GHSA-hq85-5cpj-mr62)
require updated trust bundles/tools before new installations or network package
operations. Candidate locks contain patched versions; old installations remain
separate rollout findings.

Remediation owner: Yong Kim; tracked in **atrium-i7mll4**, a deployment task
linked to inventory follow-up **atrium-oyf74f**. Rollout must preserve state,
verify backups/restoration, provision finite storage before selecting the new
runner, select signature compatibility/enforcement explicitly, update installed
packaging tools, and retain live acceptance/advisory evidence. The inventory
follow-up is an evidence review, not authorization to restart production.
