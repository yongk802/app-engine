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

## Public-origin mode (a server other people connect to)

Run on a machine reachable from a second network (a VPS), with a TLS proxy in
front and `APP_ENGINE_PUBLIC_ORIGIN`, `APP_ENGINE_TRUST_PROXY=1` and Node.js set
up as in the README.

1. Start the engine; confirm the log prints the public origin, no warnings, and
   (first start only) the minted admin secret. `players.json` and `audit.log`
   exist under the state directory with mode 600.
2. From another network, open the origin: it redirects to `/admin`. A wrong
   secret is refused; the sixth wrong attempt within a minute answers 429.
   Sign in; the launcher lists every app and `/api/local-ai/status` answers 403.
3. Open an app from the launcher. With the subdomain layout it loads on
   `<id>.<host>` with a valid certificate; with `APP_ENGINE_APP_ORIGINS=same` it
   loads under `/apps/<id>/`. Save state, reload, confirm it persists.
4. Invite a player (Players panel and `app-engine-players invite`). In a
   private window open the link, set a password, confirm the player launcher
   shows only the allowed apps, no ⚙, and `Player · name`. Try an app not
   granted: 404. Sign out, sign in at `/sign-in` with the password.
5. On a second computer, run app-engine locally with the game whose `app.json`
   lists this server. In the game, pick the server, sign in with the player's
   username and password, set up the friend roster, and **Find a random
   opponent** while `friend-bot --queue --server … --username … --password …`
   waits: both are seated; play a full match; rematch.
6. Sign in to the server from a third browser as the same player: the friend
   roster is the same one. Disable the player from the Players panel: every
   session of theirs stops within one request; enable and re-invite.
7. Loop a client at more than 20 requests per second: it receives 429 with
   `Retry-After`, other players are unaffected. Read `audit.log`: sign-ins,
   invitations, approvals and player changes are there; no secrets are.
8. Stop the proxy: the engine still only listens on 127.0.0.1. Restart the
   engine: owner and player sessions survive, tables and rosters survive.
