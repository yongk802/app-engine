# Contributing

Thanks for your interest in improving app-engine! This project is open source
under the [MIT License](LICENSE).

New to app-engine? Start with [docs/getting-started.md](docs/getting-started.md)
to get it running first.

## Development setup

```bash
git clone https://github.com/yongk802/app-engine.git
cd app-engine
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

All tests should pass before you start. Cross-repository tutor checks use a
sibling `personal-apps` clone automatically, or set `PERSONAL_APPS_DIR` to its
location explicitly.

## The git workflow for a change

If you're new to contributing on GitHub, the flow is:

1. **Fork** the repository on GitHub (creates your own copy).
2. **Clone your fork** and create a branch for your change:
   ```bash
   git clone https://github.com/yongk802/app-engine.git
   cd app-engine
   git checkout -b my-change
   ```
3. **Make your change**, then stage and commit it:
   ```bash
   git add <files-you-changed>
   git commit -m "short description of the change"
   ```
4. **Push** the branch to your fork and open a **pull request**:
   ```bash
   git push -u origin my-change
   ```
   GitHub prints a link to open the pull request.

## Guidelines

- **Add tests** for new behavior; keep the suite green (`python -m pytest tests/ -q`).
- **No new runtime dependencies** without discussion — the engine is
  deliberately small (FastAPI, uvicorn, httpx).
- **Local-only AI** — chat must resolve to a loopback endpoint. Remote/cloud
  inference endpoints are rejected by design; don't loosen that. The separate
  external assistant panel embeds an owner-configured web page; its harness
  owns any remote inference and connects explicitly through MCP.
- **Tutor knowledge** — every chat-enabled app needs at least two reviewed
  entries in its knowledge pack (CI enforces this). Keep facts concise and
  cite a source note.
- Keep commits focused and messages descriptive.

By contributing, you agree that your contributions are licensed under the
project's MIT License.
