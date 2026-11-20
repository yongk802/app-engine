# App Review — the Security app

Every app app-engine serves or installs has to pass **App Review** first. A
review is static (app code is never run) and has two halves:

| Half | Asks | Examples |
|---|---|---|
| **Contract** | Does the folder implement what the engine needs to host it? | valid `app.json`, a launch surface, the icon/screenshots/multiplayer modules it names, assets `index.html` loads, permissions the code uses but never declares; store releases also need `version`, `description`, `author`, `categories` and must fit the 256 MiB package limit |
| **Security** | OWASP Top 10 (2021) mapped scan of every shipped file | secrets and private keys (A02/A07), `eval`/`new Function`/HTML sinks/shell/SQL injection (A03), disabled TLS checks (A02), sandbox opt-outs, CORS `*`, `0.0.0.0` binds (A05), unpinned or http CDN code, known-vulnerable jQuery/lodash/AngularJS/… (A06/A08), `curl \| sh`, pickle, native binaries (A08), reaching the engine's owner API, another app's `/api/app-state/<id>`, or the launcher frame (A01) |

Findings rated **critical** or **high** block the app. Medium and below are
reported and lower the 0–100 score, but never block.

Everything the engine can serve is reviewed at full severity: minified and
vendored bundles, `node_modules`, long lines and browser files under `tests/`
get no exemption. Only server-side test files (`test_*.py`, `tests/*.go`, …),
which never run inside the app, are reported at low severity. Code-sink rules
ignore matches inside balanced, interpolation-free string literals and HTML
prose, so a tutorial that *mentions* `eval()` is not flagged.

An incomplete review never passes: a browser file too large to scan, the
file/byte limits, and the per-review time budget are blocking.

Native executables block store packages (a release must ship source). In a
local folder they are usually the app's own runtime build output, so they are
reported at medium severity.

Not reviewed, and therefore **never served** by the engine: `.git`, `.hg`,
`.svn`, `.venv`, `venv`, `__pycache__`, tool caches, the engine's `.runtime`
output and the owner-only `private/` folder (case-insensitive).

## Where the gate runs

* **Store installs**: the package is extracted to a private staging folder and
  reviewed with the `store` profile *before* it can be renamed into place. A
  refusal returns `422 review_failed` with the full report; nothing is installed.
* **Local app folders**: catalog discovery reviews each app with the `local`
  profile. A failing app is not served; it is listed in the launcher's
  "skipped" note and in `GET /api/apps/rejected` with `review: true`.

Reports are cached under `<state>/app-review/reports/` and reused until a
file in the app (or the scanner itself) changes.

## Modes

`APP_ENGINE_APP_REVIEW` selects the policy:

* `enforce` (default): failing apps are neither served nor installed.
* `warn`: everything is reviewed and shown in Security, but failing apps still run.
* `off`: no review.

## Waivers

Some findings are intentional: a coding tutor *has* to run the learner's code.
In **Security** (owner only, launcher sidebar → *Security ↗*) the owner can
waive a single blocking finding with a reason. A waiver:

* covers exactly one statement (rule + file + the line's text), so a new `eval`
  added later is not pre-approved, and survives the line moving;
* lives in `<state>/app-review/waivers.json`, never in the app, so an app
  cannot approve itself;
* can be removed at any time. Contract failures that make the app unusable
  (broken manifest, missing store listing) cannot be waived.

Owner API (launcher origin; mutations need the `X-App-Engine-Admin` capability):

| Method | Path | |
|---|---|---|
| GET | `/api/app-engine/review` | mode + one summary per app |
| GET | `/api/app-engine/review/apps/{id}` | full report |
| POST | `/api/app-engine/review/apps/{id}/rescan` | force a fresh review |
| POST | `/api/app-engine/review/apps/{id}/waivers` | `{fingerprint, reason}` |
| DELETE | `/api/app-engine/review/apps/{id}/waivers/{fingerprint}` | remove a waiver |
| GET | `/api/app-engine/review/rules` | the rule catalogue |
| GET | `/api/app-engine/review/ui` | the Security page |

## Before you ship an app

```bash
app-engine-review path/to/my-app                 # local profile
app-engine-review path/to/my-app --profile store # what a store install will require
python -m app_engine.app_review path/to/my-app --json
```

The exit status is 1 when the app would be refused.
