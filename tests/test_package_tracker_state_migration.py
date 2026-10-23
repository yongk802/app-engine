"""Package Tracker backups must enter only the shared web scope."""

import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/migrate-package-tracker-state.py"
spec = importlib.util.spec_from_file_location("package_tracker_state_migration", SCRIPT)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def archive(path, *, unsafe=False):
    values = {
        "packages.json": {"one": {"status": "in_transit"}},
        "settings.json": {"seen_ids": ["a", "b"]},
        "merchants.json": {},
        "credentials.json": {},
    }
    with tarfile.open(path, "w:gz") as target:
        for name, value in values.items():
            content = json.dumps(value).encode()
            member = tarfile.TarInfo(name if not unsafe else "../" + name)
            member.size = len(content)
            target.addfile(member, io.BytesIO(content))


def test_archive_counts_records_without_exposing_credentials(tmp_path):
    source = tmp_path / "backup.tar.gz"
    archive(source)
    files, packages, seen = migration.read_archive(source)
    assert set(files) == migration.FILES
    assert (packages, seen) == (1, 2)
    assert migration.scoped_dir(Path("/runtime")) == Path(
        "/runtime/data/package-tracker/5aa0c7be2e23c18d")


def test_archive_rejects_traversal_members(tmp_path):
    source = tmp_path / "unsafe.tar.gz"
    archive(source, unsafe=True)
    with pytest.raises(ValueError, match="members"):
        migration.read_archive(source)


def test_migration_replaces_mac_local_atrium_url_without_changing_key(tmp_path):
    source = tmp_path / "backup.tar.gz"
    archive(source)
    files, _, _ = migration.read_archive(source)
    credentials = {"ATRIUM_API_KEY": "private-key", "ATRIUM_BASE_URL": "https://127.0.0.1:9900"}
    files["credentials.json"] = json.dumps(credentials).encode()
    updated = migration.set_atrium_tunnel_url(files, migration.ATRIUM_TUNNEL_URL)
    assert json.loads(updated["credentials.json"]) == {
        "ATRIUM_API_KEY": "private-key",
        "ATRIUM_BASE_URL": "https://atrium-status.localhost:9444",
    }
    assert files["credentials.json"] == json.dumps(credentials).encode()
    with pytest.raises(ValueError, match="verified loopback"):
        migration.set_atrium_tunnel_url(files, "https://127.0.0.1:9900")
