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
