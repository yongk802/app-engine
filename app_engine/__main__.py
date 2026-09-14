"""Command-line entry points for the standalone app-engine distribution."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _engine_paths() -> tuple[Path, Path]:
    """Return the engine source and the directory containing its data files.

    ``engine.py`` historically resolves launcher assets beside its own file.
    Setuptools installs ``py-modules`` into ``site-packages`` but installs
    ``data-files`` below the environment prefix, so the two directories differ
    in a normal virtualenv.  Keep the source file untouched and execute it
    with a legacy-compatible ``__file__`` rooted at the installed data files.
    """

    spec = importlib.util.find_spec("engine")
    if spec is None or not spec.origin or spec.origin in {"built-in", "frozen"}:
        raise RuntimeError("the packaged engine.py module could not be located")
    source = Path(spec.origin).resolve()
    candidates = (source.parent, Path(sys.prefix), Path(__file__).resolve().parents[1])
    for candidate in candidates:
        if (candidate / "launcher.html").is_file() and (candidate / "model-registry.json").is_file():
            return source, candidate
    # Preserve the original error location for source checkouts and provide a
    # useful path when a package was installed without its data files.
    return source, source.parent


def main() -> None:
    """Run the legacy top-level engine with its existing CLI semantics.

    The import is intentionally deferred so ``import app_engine`` and imports
    of the contract surface stay free of Ollama/model-file initialization.
    """

    source, data_root = _engine_paths()
    namespace = {
        "__name__": "__main__",
        "__file__": str(data_root / "engine.py"),
        "__package__": None,
        "__cached__": None,
    }
    exec(compile(source.read_bytes(), str(source), "exec"), namespace)


def players_main(argv: list[str] | None = None) -> int:
    """Manage a public-origin engine's players from a shell: the running engine picks up changes."""

    import argparse
    import os
    import time
    from pathlib import Path

    from .public_origin import Accounts, PublicOrigin, invite_link

    parser = argparse.ArgumentParser(prog="app-engine-players", description="Invite and manage players of a public-origin app-engine.")
    parser.add_argument("--state-dir", default=os.environ.get("APP_ENGINE_STATE_DIR", "~/.config/app-engine/app-state"), help="APP_ENGINE_STATE_DIR of the engine")
    parser.add_argument("--origin", default=os.environ.get("APP_ENGINE_PUBLIC_ORIGIN", ""), help="APP_ENGINE_PUBLIC_ORIGIN, used to print invitation links")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show every player")
    invite = commands.add_parser("invite", help="create a player and print a one-time invitation link")
    invite.add_argument("--username", required=True)
    invite.add_argument("--name", default=None, help="display name (defaults to the username)")
    invite.add_argument("--apps", default="", help="comma-separated app ids the player may open")
    reinvite = commands.add_parser("reinvite", help="print a fresh invitation link for an existing player (lets them set a new password)")
    reinvite.add_argument("username")
    for name, text in (("disable", "stop a player signing in"), ("enable", "let a disabled player sign in again"), ("remove", "delete a player")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("username")
    args = parser.parse_args(argv)

    accounts = Accounts.load(Path(args.state_dir).expanduser())
    public = PublicOrigin.parse(args.origin) if args.origin else None
    link = (lambda code: invite_link(public, code)) if public else (lambda code: f"<origin>/join/{code}")

    def find(username: str) -> dict:
        player = next((p for p in accounts.players() if p["username"] == username.strip().lower()), None)
        if player is None:
            parser.exit(1, f"no player named {username}\n")
        return player

    try:
        if args.command == "list":
            for p in accounts.players():
                status = "disabled" if p["disabled"] else "invited" if p["invite_pending"] else "active" if p["has_password"] else "invitation expired"
                seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(p["last_seen"])) if p["last_seen"] else "never"
                print(f"{p['username']:<24} {status:<19} last seen {seen:<17} apps: {', '.join(p['apps']) or '-'}")
            if not accounts.players():
                print("no players yet")
        elif args.command == "invite":
            apps = [a.strip() for a in args.apps.split(",") if a.strip()]
            player, code = accounts.create_player(args.username, apps, args.name)
            print(f"invited {player['username']} for {', '.join(apps) or 'no apps yet'}\nsend this link once (valid 7 days): {link(code)}")
        elif args.command == "reinvite":
            code = accounts.regenerate_invite(find(args.username)["id"])
            print(f"send this link once (valid 7 days): {link(code)}")
        elif args.command in {"disable", "enable"}:
            accounts.update_player(find(args.username)["id"], disabled=args.command == "disable")
            print(f"{args.username} {args.command}d")
        elif args.command == "remove":
            accounts.remove_player(find(args.username)["id"])
            print(f"{args.username} removed")
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    return 0


def manifest_main() -> int:
    """Validate one or more app directories using the manifest CLI."""

    from .manifest import _cli

    return _cli(sys.argv[1:])


if __name__ == "__main__":
    main()
