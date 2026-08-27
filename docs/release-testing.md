# Release testing

Run this checklist on a clean macOS, Windows, and Linux machine before tagging
a release. Record the OS version, Python version, Ollama version, selected
profile/model, and the final smoke output in the release notes.

1. Follow `docs/getting-started.md` from a fresh clone and virtual environment.
2. Start app-engine and open `http://127.0.0.1:8770`.
3. Confirm apps open at `<app-id>.localhost`, save state, reload, and verify the
   state remains scoped to that app.
4. With Ollama absent, open Local AI settings and confirm the detected platform,
   recommendation, download size, privacy text, and official install guidance.
5. Install Ollama through the guided flow. Confirm app-engine detects an existing
   installation after **Check again** without needing a restart.
6. Pull the default Quality model. Cancel once, retry, finish the pull, and verify
   a tutor response. Repeat with Compatibility if the machine cannot run Quality.
7. Remove only a model downloaded by app-engine; confirm unrelated Ollama models
   are not offered for removal.
8. Stop Ollama, exercise **Start Ollama**, refresh readiness, and send another
   tutor question.
9. Run `python scripts/release_smoke.py --require-ready` and save its JSON output.
10. Run `python -m pytest tests/ -q` with `PERSONAL_APPS_DIR` pointing at a clean
    checkout of `personal-apps`.

GitHub Actions covers Python 3.10–3.13 on all three operating systems. It does
not replace these UI and model-download checks, which require real OS installers,
hardware, and several gigabytes of model data.
