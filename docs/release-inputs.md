# Credentials, dependencies and publisher trust

## Owner credential storage

New owner secrets use `pbkdf2-sha256-v1$rounds$salt$hash`, with 600,000
PBKDF2-HMAC-SHA256 iterations and a fresh 128-bit salt. The work factor follows
[OWASP's PBKDF2 guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#pbkdf2)
using Python's standard library without a new runtime dependency. Token digests
remain SHA-256 because their inputs are random capabilities, not human passwords.

Existing unsalted owner hashes upgrade after successful authentication or when
startup receives the matching configured secret. Wrong secrets do not migrate
anything. This storage upgrade preserves existing sessions. Changing the secret
is a rotation and revokes all owner and player sessions. Account transactions
are serialized across engine threads and CLI processes, and verification plus
session issuance happens in one transaction so rotation cannot race a login.
The version, iteration count, salt and digest shape are validated before an
expensive derivation; malformed or unknown hash versions fail closed.

## Release dependency inputs

`pyproject.toml` declares compatibility ranges and development groups. `uv.lock`
is the reviewed complete resolution, including the Python 3.10 compatibility
branch. The committed `requirements.txt`, `requirements-dev.txt` and
`requirements-browser.txt` are hash-bearing exports for pip users. Install the
standalone release with:

```sh
python -m pip install --require-hashes -r requirements.txt
```

CI uses uv 0.13.4 and `uv sync --locked --no-install-project` with exact Python
versions; the browser lane includes the browser group. Build backends are pinned
in both the build-system declaration and the development group. GitHub actions
are pinned to full commit SHAs and Node to an exact version. Launcher tests use
`npm ci --ignore-scripts` with the existing committed `package-lock.json`. A separate lane
regenerates the exports and fails on drift. The Package Tracker capability
installer uses its own committed hash-bearing requirements file, preserving its
separate Playwright version.

To intentionally update inputs, review the upstream release and advisory changes,
update declarations, then run:

```sh
uv lock --upgrade
uv export --locked --no-dev --no-emit-project -o requirements.txt >/dev/null
uv export --locked --no-emit-project -o requirements-dev.txt >/dev/null
uv export --locked --group browser --no-emit-project -o requirements-browser.txt >/dev/null
uv pip compile --universal --python-version 3.10 --generate-hashes deploy/package-tracker/requirements.in -o deploy/package-tracker/requirements.txt >/dev/null
```

Review the resulting versions, registry origins and artifact hashes together.
Run the Python, browser and kernel-boundary tests before accepting the update.
The offline wheel test proves a wrong artifact hash is rejected and the reviewed
hash installs a harmless fixture. Advisory scans are point-in-time checks, not
proof that a dependency is safe. GitHub-hosted OS images, vendor tool/browser
artifacts and their distribution infrastructure remain trust roots. Installing
the reusable wheel alone resolves its declared compatibility ranges; consumers
who need the frozen standalone environment must also consume the lock/export.

## Store publisher trust

The owner explicitly chooses each store URL and reviews a concrete release
preview before installation. A catalog's SHA-256 identifies the downloaded bytes
and detects a changed or corrupted package. It does **not** authenticate an
independent publisher: a compromised store can replace both a package and its
checksum. App-engine currently accepts unsigned packages and does not claim a
publisher-signature or transparency-log guarantee. Use stores and release channels
whose maintainers you trust, and verify independently supplied publisher digests
through that publisher's authenticated channel when available.

Installation validates and extracts a package without importing it or running
install/build/start commands. Store trust does not grant execution approval:
managed code and multiplayer rules still need the owner's content-bound approval
and the default enforced runner. Static browser code is served on its isolated
app origin. A package can still be malicious within the capabilities the owner
grants it; name, author metadata, HTTPS and a valid checksum do not expand those
capabilities or establish publisher identity.

`tests/test_publisher_trust.py` verifies an unsigned, hash-valid package needs
owner installation authority, executes nothing during installation, and remains
unapproved on its first managed launch. Existing archive tests cover digest
mismatch, changed previews, links, path traversal and extraction limits.
