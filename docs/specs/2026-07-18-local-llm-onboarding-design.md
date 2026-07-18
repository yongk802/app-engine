# Local LLM Onboarding Design

**Date:** 2026-07-18  
**Status:** Approved design  
**Scope:** Cross-platform, strictly local tutor-chat setup for app-engine

## Goal

Make chat-enabled apps work for nontechnical users without requiring them to
manually install, configure, or troubleshoot a local model runtime. App-engine
will detect or install Ollama, recommend an appropriate model for the computer,
manage app-engine-requested model downloads, and expose clear readiness and
recovery states to the launcher.

The default is a higher-quality tutoring profile. Users with less capable
hardware can select a smaller compatibility profile. All prompts and model
output remain local.

## Product principles

- Chat setup must never block non-chat apps or app-engine startup.
- No installer, model download, model switch, or deletion begins without an
  explicit user action.
- App-engine may manage models it requested, but must not modify or remove
  unrelated Ollama models.
- Installation actions use reviewed, fixed OS adapters. Webpage or manifest
  data must never become shell commands.
- Errors must explain what happened and offer a concrete next action.
- Technical concepts such as quantization stay out of the primary workflow.

## Supported platforms

The first public release supports macOS, current 64-bit Windows versions
supported by Ollama, and common `systemd`-based Linux distributions. Other
Linux environments may use an existing, manually configured Ollama endpoint.
The feature is strictly local-only. Cloud API fallback is out of scope.

## User journey

When a user opens a chat-enabled app, the chat area requests readiness from the
backend. If local AI is not ready, it displays a setup card rather than an
enabled but broken input.

The setup wizard:

1. Inspects OS, architecture, installed RAM, free disk, and practical local
   acceleration signals.
2. Checks the configured loopback Ollama endpoint, then looks for an installed
   executable or service.
3. Recommends the Quality or Compatibility model profile.
4. Shows approximate download size, memory guidance, and that AI stays local.
5. If Ollama is missing, shows a reviewed installation plan and requests
   explicit confirmation before launching it.
6. Offers to start an installed but stopped Ollama runtime.
7. Pulls the selected model with visible, cancellable progress.
8. Sends a short verification prompt and shows a sample response.
9. Marks tutor chat ready and focuses the chat input.

Users may leave the wizard while a model downloads. The launcher remains
responsive and non-chat apps remain usable. Returning restores the latest
operation state obtained from Ollama.

Existing Ollama users keep their installation and models. App-engine asks
before pulling its recommended model and never upgrades Ollama automatically.
An advanced setting permits a different loopback endpoint. Non-loopback
endpoints are rejected by default.

## Model profiles

Apps declare subject-specific system prompts but do not name models. A
versioned registry bundled with each app-engine release maps capability profiles
to concrete Ollama model tags.

### Quality

- Default target: a reviewed 7–8B instruct model.
- Recommended with at least 16 GB RAM and sufficient disk.
- Optimized for explanations, debugging, hints, and multi-step tutoring.

### Compatibility

- A reviewed 1–3B instruct model.
- Recommended around 8 GB RAM, low disk, or slower hardware.
- Presented as smaller and faster, with a warning that complex tutoring may be
  less reliable.

The registry records model ID, Ollama tag, profile, expected download bytes,
minimum and recommended RAM, supported architectures, context limit, and
registry version. The first release includes exactly one model per profile.
Profile selection is global; per-app overrides are deferred.

Hardware recommendations are advisory. A user may override them after seeing
resource warnings. If Quality fails verification due to resource limits, the
primary recovery action is switching to Compatibility.

## Components and public interfaces

### SystemProbe

Purpose: inspect the host without changing it.

```text
inspect() -> SystemCapabilities
```

`SystemCapabilities` contains OS (`macos`, `windows`, or `linux`), architecture
(`arm64` or `x86_64`), RAM bytes, free-disk bytes, acceleration capability,
detected Ollama endpoint and executable, service state (`running`, `stopped`,
`missing`, or `unknown`), and an optional installation method.

`inspect` has no side effects and may run concurrently. Missing Ollama is a
normal result. It raises `ProbeError` only when required system information
cannot be inspected; optional unavailable data is represented as unknown.

### ModelRegistry

Purpose: load reviewed profiles and recommend one for the inspected host.

```text
recommend(capabilities: SystemCapabilities) -> ModelRecommendation
get_profile(profile_id: ProfileId) -> ModelProfile
installed_profiles(status: OllamaStatus) -> list[InstalledProfile]
```

`ModelRecommendation` contains the profile, resource rationale, warnings, and
whether override is safe. Registry validation raises `InvalidRegistryError` at
startup. An invalid registry disables chat setup but not app-engine.

### OllamaManager

Purpose: perform reviewed Ollama lifecycle and model-management operations.

```text
status() -> OllamaStatus
installation_plan() -> InstallationPlan
authorize(plan_id: PlanId) -> ConfirmationToken
install(plan_id: PlanId, token: ConfirmationToken) -> Operation
start() -> Operation
pull(model_id: ModelId) -> async stream[ModelProgress]
cancel(operation_id: OperationId) -> OperationResult
remove_managed_model(model_id: ModelId) -> OperationResult
verify(model_id: ModelId) -> VerificationResult
```

`InstallationPlan` contains a stable plan ID, OS adapter, official source,
exact command or installer URL, privilege requirement, expected filesystem
effects, and expiration. `ConfirmationToken` is short-lived, single-use, and
bound to the exact plan. Changed, expired, or reused plans raise
`ConfirmationError`.

Only one install, pull, or removal operation may mutate Ollama at a time.
Status and progress reads remain concurrent. App-engine records which model IDs
it requested so deletion is limited to managed models. Cancellation is
best-effort and reports whether Ollama retained resumable partial data.

Platform adapters use official installation paths: official application or
package on macOS (optionally Homebrew when present), the official Windows
installer, and a visibly reviewed official Linux command. App-engine never
downloads an unseen response and pipes it directly to a shell.

Errors are `OllamaNotInstalledError`, `OllamaStartError`,
`InsufficientDiskError`, `DownloadError`, `OperationConflictError`,
`ConfirmationError`, `VerificationError`, and `UnsupportedPlatformError`.

### ChatRuntime

Purpose: give the launcher and apps one stable local-chat contract.

```text
readiness() -> ChatReadiness
stream(app_id: AppId, messages: list[ChatMessage]) -> async stream[ChatEvent]
```

`ChatReadiness.state` is `ready`, `setup_required`, `installing`, `starting`,
`downloading`, `loading_model`, `unavailable`, or `error`. It also contains the
active profile, selected model, operation progress, summary, and permitted
recovery actions.

`ChatMessage` has a validated `user` or `assistant` role and bounded UTF-8
content. `ChatEvent` is a tagged union:

- `token { text }`
- `complete { usage? }`
- `setup_required { readiness }`
- `model_loading { elapsed_seconds }`
- `error { code, message, actions, retryable }`

The runtime validates the app against discovered chat-enabled apps, prepends
the app system prompt plus the shared tutoring policy, and streams through
Ollama. It preserves incomplete SSE lines between network chunks. Connection,
timeout, protocol, malformed-stream, missing-model, and resource errors become
typed events. Every stream ends with `complete` or `error`.

## Shared tutoring policy

App-specific prompts remain authoritative for subject matter. App-engine adds a
short policy asking the model to explain concretely, give hints before complete
solutions when appropriate, be honest about uncertainty, avoid claiming tools
or internet access it lacks, and keep initial answers concise.

## Launcher and settings experience

The Local AI settings surface shows readiness and endpoint health, recommended
and selected profiles, installed managed models and sizes, applicable install
or recovery actions, non-mutating diagnostics, and a copyable redacted summary.

Chat panels render readiness-specific views. Cold start shows “Loading the
local model.” Low disk errors show required and available space. Missing models
offer setup, download failures offer retry, and installer failures retain local
logs and offer diagnostics.

The setup and chat UI must work at a 390 CSS-pixel viewport. On narrow screens,
the app sidebar becomes a drawer and tutor chat becomes a collapsible sheet or
tab. The document must not scroll horizontally.

## Security and privacy

- Ollama remains bound to loopback by default.
- Installer plans and commands come only from fixed application code.
- Every mutating operation requires an explicit user gesture.
- Privilege elevation uses the OS's visible mechanism and never captures
  credentials.
- Logs redact environment variables, credentials, query parameters, and user
  paths where practical.
- Prompts and responses are not logged by default.
- Diagnostics include runtime versions and error codes, not chat content.
- App manifests cannot select commands, installer URLs, endpoints, or arbitrary
  model tags.

## Persistence

App-engine stores schema version, selected profile, configured loopback
endpoint, app-engine-managed model IDs, completed setup version, and
non-sensitive operation metadata. Writes are atomic. Corrupt configuration is
quarantined and replaced with safe defaults; it never prevents startup. Model
files remain owned by Ollama and are not duplicated.

## Testing

- Unit tests cover hardware recommendations, registry validation, installation
  plans, confirmation expiry, command safety, error translation, persistence,
  and SSE lines split across chunks.
- Fake-Ollama tests cover missing/stopped runtimes, pulls, cancellation,
  interruption and retry, missing models, timeouts, malformed streams,
  verification failure, and unrelated models.
- Browser tests cover first-run setup, Compatibility recommendations, profile
  override, interrupted downloads, cold loading, keyboard and screen-reader
  access, and 390-pixel responsiveness.
- Platform VM tests cover clean installation and first response on macOS,
  Windows, and supported Linux.
- Release smoke tests open every chat-enabled personal app and ask one small,
  subject-relevant question.

## Acceptance criteria

- A supported clean machine reaches a successful local tutor response through
  the guided UI without manually entering a shell command.
- No download or installation begins before confirmation.
- Non-chat apps work throughout setup and after any setup failure.
- A 390-pixel viewport has no horizontal document overflow.
- Interrupted model setup can be retried without restarting app-engine.
- Compatibility recovers from a Quality resource failure.
- Unrelated Ollama models remain unchanged through install, switch, and removal.
- Every chat stream ends with a typed completion or actionable error.

## Delivery phases

1. **Readiness foundation:** registry, system probe, Ollama health checks,
   readiness API, and friendly setup-required chat state.
2. **Model management:** recommendations, progress, verification, cancellation,
   settings, and profile switching for existing Ollama installations.
3. **Guided installation:** reviewed macOS, Windows, and Linux adapters,
   confirmation plans, startup handling, and clean-machine VM tests.
4. **Hardening:** responsive UI, accessibility, diagnostics, interrupted
   operation recovery, security review, and release smoke tests.

This order delivers value to existing Ollama users before introducing the more
sensitive installer boundary.

## Explicitly deferred

- Cloud or remote inference providers.
- Automatic Ollama upgrades.
- Silent or background model downloads.
- Per-app model selection.
- Additional runtimes such as llama.cpp or LM Studio.
- LAN Ollama endpoints.
- Automatic deletion based on disk pressure.
