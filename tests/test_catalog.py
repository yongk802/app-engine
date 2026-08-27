"""Contract tests for immutable, refreshable app catalog snapshots."""

from __future__ import annotations

import asyncio
import builtins
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import threading

import pytest

from app_engine.catalog import ImmutableAppCatalog
from app_engine.contracts import AppId, AppSource, CatalogKey
from app_engine.manifest import parse_manifest


def _write_app(
    root: Path,
    directory: str,
    *,
    app_id: str | None = None,
    label: str | None = None,
    min_engine_version: str | None = None,
    raw_manifest: str | None = None,
) -> Path:
    app_root = root / directory
    app_root.mkdir(parents=True)
    if raw_manifest is None:
        payload: dict[str, object] = {
            "id": app_id or directory,
            "label": label or directory.title(),
            "icon": "A",
            "version": "1.0.0",
        }
        if min_engine_version is not None:
            payload["min_engine_version"] = min_engine_version
        raw_manifest = json.dumps(payload)
    (app_root / "app.json").write_text(raw_manifest, encoding="utf-8")
    (app_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return app_root


def _source(root: Path, *, kind: str = "external", priority: int = 0) -> AppSource:
    return AppSource(kind=kind, root=root, priority=priority)


def _rejection_text(rejection: object) -> str:
    return " ".join(
        str(getattr(rejection, field, "")) for field in ("reason", "detail")
    ).lower()


@pytest.mark.asyncio
async def test_configure_discovers_only_immediate_manifest_children(tmp_path: Path):
    source_root = tmp_path / "apps"
    direct = _write_app(source_root, "direct")
    _write_app(source_root / "group", "nested")
    (source_root / "loose-file").write_text("not an app", encoding="utf-8")
    (source_root / "app.json").write_text("{}", encoding="utf-8")

    catalog = ImmutableAppCatalog()
    snapshot = await catalog.configure(
        CatalogKey("main"), (_source(source_root),)
    )

    assert [app.manifest.app_id for app in snapshot.apps] == ["direct"]
    assert snapshot.apps[0].manifest.root == direct.resolve()


@pytest.mark.asyncio
async def test_inspect_and_discovery_follow_the_public_manifest_parser(tmp_path: Path):
    source_root = tmp_path / "apps"
    app_root = _write_app(source_root, "typed")
    expected = parse_manifest(app_root)
    assert expected.manifest is not None

    catalog = ImmutableAppCatalog()
    inspected = await catalog.inspect(app_root)
    snapshot = await catalog.configure(
        CatalogKey("main"), (_source(source_root),)
    )

    assert inspected == expected
    assert snapshot.apps[0].manifest == expected.manifest
    assert snapshot.apps[0].warnings == expected.warnings


@pytest.mark.asyncio
async def test_priority_and_configured_order_resolve_duplicate_ids_deterministically(
    tmp_path: Path,
):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    high_root = tmp_path / "high"
    first = _write_app(first_root, "first-copy", app_id="same", label="First")
    second = _write_app(second_root, "second-copy", app_id="same", label="Second")
    high = _write_app(high_root, "high-copy", app_id="same", label="High")

    catalog = ImmutableAppCatalog()
    snapshot = await catalog.configure(
        CatalogKey("main"),
        (
            _source(first_root, kind="first", priority=10),
            _source(high_root, kind="native", priority=100),
            _source(second_root, kind="second", priority=10),
        ),
    )

    assert len(snapshot.apps) == 1
    assert snapshot.apps[0].manifest.root == high.resolve()
    duplicate_roots = {
        rejection.root
        for rejection in snapshot.rejections
        if "duplicate" in _rejection_text(rejection)
    }
    assert duplicate_roots == {first.resolve(), second.resolve()}

    tied = await catalog.configure(
        CatalogKey("tied"),
        (
            _source(first_root, kind="first", priority=10),
            _source(second_root, kind="second", priority=10),
        ),
    )
    assert tied.apps[0].manifest.root == first.resolve()
    assert any(
        rejection.root == second.resolve()
        and "duplicate" in _rejection_text(rejection)
        for rejection in tied.rejections
    )


@pytest.mark.asyncio
async def test_catalog_surfaces_malformed_incompatible_and_shadowed_candidates(
    tmp_path: Path,
):
    preferred_root = tmp_path / "preferred"
    fallback_root = tmp_path / "fallback"
    preferred = _write_app(preferred_root, "good", app_id="shared")
    shadowed = _write_app(fallback_root, "shadowed", app_id="shared")
    malformed = _write_app(
        fallback_root,
        "malformed",
        raw_manifest="{this is not json",
    )
    incompatible = _write_app(
        fallback_root,
        "future",
        min_engine_version="999.0.0",
    )
    missing_manifest = fallback_root / "missing-manifest"
    missing_manifest.mkdir()
    (missing_manifest / "index.html").write_text("app-like", encoding="utf-8")

    catalog = ImmutableAppCatalog()
    snapshot = await catalog.configure(
        CatalogKey("main"),
        (
            _source(preferred_root, kind="native", priority=100),
            _source(fallback_root, priority=1),
        ),
    )

    assert any(app.manifest.root == preferred.resolve() for app in snapshot.apps)
    by_root = {rejection.root: rejection for rejection in snapshot.rejections}
    assert "duplicate" in _rejection_text(by_root[shadowed.resolve()])
    assert any(
        token in _rejection_text(by_root[malformed.resolve()])
        for token in ("manifest", "json", "invalid")
    )
    assert "compatib" in _rejection_text(by_root[incompatible.resolve()])
    assert by_root[missing_manifest.resolve()].reason == "missing_manifest"


@pytest.mark.asyncio
async def test_refresh_atomically_replaces_but_never_mutates_old_snapshots(
    tmp_path: Path,
):
    source_root = tmp_path / "apps"
    app_root = _write_app(source_root, "demo", label="Before")
    catalog = ImmutableAppCatalog()
    first = await catalog.configure(
        CatalogKey("main"), (_source(source_root),)
    )

    with pytest.raises(FrozenInstanceError):
        first.generation = 99  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.apps[0] = first.apps[0]  # type: ignore[index]

    payload = json.loads((app_root / "app.json").read_text(encoding="utf-8"))
    payload["label"] = "After"
    (app_root / "app.json").write_text(json.dumps(payload), encoding="utf-8")
    second = await catalog.refresh(CatalogKey("main"))

    assert second is catalog.snapshot(CatalogKey("main"))
    assert second.generation == first.generation + 1
    assert first.apps[0].manifest.label == "Before"
    assert second.apps[0].manifest.label == "After"


@pytest.mark.asyncio
async def test_refresh_publishes_only_material_changes(tmp_path: Path):
    source_root = tmp_path / "apps"
    app_root = _write_app(source_root, "demo", label="Before")
    key = CatalogKey("main")
    catalog = ImmutableAppCatalog()
    first = await catalog.configure(key, (_source(source_root),))
    events = catalog.events(key)
    next_event = asyncio.create_task(anext(events))
    await asyncio.sleep(0)

    manifest_path = app_root / "app.json"
    unchanged_bytes = manifest_path.read_bytes()
    manifest_path.write_bytes(unchanged_bytes)
    unchanged = await catalog.refresh(key)
    await asyncio.sleep(0)

    assert unchanged is first
    assert unchanged.generation == first.generation
    assert not next_event.done()

    payload = json.loads(unchanged_bytes)
    payload["description"] = "material change"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    changed = await catalog.refresh(key)
    event = await asyncio.wait_for(next_event, timeout=1)
    await events.aclose()

    assert changed.generation == first.generation + 1
    assert event.key == key
    assert event.generation == changed.generation
    assert event.changed_app_ids == (AppId("demo"),)


@pytest.mark.asyncio
async def test_snapshot_reads_are_pure_and_never_rescan_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_root = tmp_path / "apps"
    _write_app(source_root, "demo")
    key = CatalogKey("main")
    catalog = ImmutableAppCatalog()
    expected = await catalog.configure(key, (_source(source_root),))

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("snapshot() performed filesystem I/O")

    monkeypatch.setattr(builtins, "open", fail)
    monkeypatch.setattr(os, "listdir", fail)
    monkeypatch.setattr(os, "scandir", fail)
    monkeypatch.setattr(Path, "exists", fail)
    monkeypatch.setattr(Path, "is_dir", fail)
    monkeypatch.setattr(Path, "iterdir", fail)
    monkeypatch.setattr(Path, "open", fail)
    monkeypatch.setattr(Path, "read_bytes", fail)
    monkeypatch.setattr(Path, "read_text", fail)
    monkeypatch.setattr(Path, "stat", fail)

    assert catalog.snapshot(key) is expected
    assert catalog.snapshot(key) is expected


@pytest.mark.asyncio
async def test_stale_refresh_cannot_overwrite_a_newer_configure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    _write_app(old_root, "demo", label="Old")
    _write_app(new_root, "demo", label="New")
    key = CatalogKey("main")
    catalog = ImmutableAppCatalog()
    await catalog.configure(key, (_source(old_root),))

    refresh_started = threading.Event()
    allow_refresh = threading.Event()
    original_scan = catalog._scan
    block_old_scan = True

    def interleaved_scan(sources: tuple[AppSource, ...]):
        nonlocal block_old_scan
        if block_old_scan and sources[0].root == old_root:
            block_old_scan = False
            refresh_started.set()
            assert allow_refresh.wait(timeout=2)
        return original_scan(sources)

    monkeypatch.setattr(catalog, "_scan", interleaved_scan)
    events = catalog.events(key)
    next_event = asyncio.create_task(anext(events))
    refresh_task = asyncio.create_task(catalog.refresh(key))
    assert await asyncio.to_thread(refresh_started.wait, 1)
    configure_task = asyncio.create_task(
        catalog.configure(key, (_source(new_root),))
    )
    await asyncio.sleep(0)
    allow_refresh.set()

    await refresh_task
    configured = await configure_task
    event = await asyncio.wait_for(next_event, timeout=1)
    await events.aclose()

    final = catalog.snapshot(key)
    assert final is configured
    assert final.apps[0].manifest.label == "New"
    assert event.generation == final.generation


@pytest.mark.asyncio
async def test_slow_refresh_does_not_block_a_different_catalog_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    slow_root = tmp_path / "slow"
    fast_root = tmp_path / "fast"
    _write_app(slow_root, "slow-app")
    _write_app(fast_root, "fast-app")
    slow_key = CatalogKey("slow")
    fast_key = CatalogKey("fast")
    catalog = ImmutableAppCatalog()
    await catalog.configure(slow_key, (_source(slow_root),))

    slow_started = threading.Event()
    allow_slow = threading.Event()
    original_scan = catalog._scan

    def blocking_scan(sources: tuple[AppSource, ...]):
        if sources[0].root == slow_root:
            slow_started.set()
            assert allow_slow.wait(timeout=2)
        return original_scan(sources)

    monkeypatch.setattr(catalog, "_scan", blocking_scan)
    slow_refresh = asyncio.create_task(catalog.refresh(slow_key))
    assert await asyncio.to_thread(slow_started.wait, 1)
    try:
        fast = await asyncio.wait_for(
            catalog.configure(fast_key, (_source(fast_root),)), timeout=1
        )
    finally:
        allow_slow.set()
        await slow_refresh

    assert fast.apps[0].manifest.app_id == AppId("fast-app")
