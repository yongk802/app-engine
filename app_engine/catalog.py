"""Immutable, refreshable application catalog snapshots.

Filesystem work is confined to :meth:`configure`, :meth:`refresh`, and
:meth:`inspect`.  Published snapshots contain only frozen contract objects, so
``snapshot()`` is a pure in-memory lookup suitable for request hot paths.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from .contracts import (
    AppCatalog,
    AppId,
    AppSource,
    CatalogApp,
    CatalogEvent,
    CatalogKey,
    CatalogRejection,
    CatalogSnapshot,
    ManifestInspection,
)
from .manifest import is_compatible, parse_manifest


class ImmutableAppCatalog(AppCatalog):
    """Discover apps and atomically publish immutable catalog generations.

    Higher source priorities win duplicate app IDs.  Equal priorities retain
    configured source order, followed by lexicographic directory order within
    a source.  Rejected and shadowed candidates remain visible for diagnosis.
    """

    def __init__(self) -> None:
        self._snapshots: dict[CatalogKey, CatalogSnapshot] = {}
        self._sources: dict[CatalogKey, tuple[AppSource, ...]] = {}
        self._subscribers: dict[
            CatalogKey, set[asyncio.Queue[CatalogEvent]]
        ] = {}
        self._operation_locks: dict[CatalogKey, asyncio.Lock] = {}
        self._publish_lock = asyncio.Lock()

    async def configure(
        self, catalog_key: CatalogKey, sources: tuple[AppSource, ...]
    ) -> CatalogSnapshot:
        """Configure sources and publish their current materialized catalog."""
        async with self._operation_lock(catalog_key):
            materialized = await asyncio.to_thread(self._scan, sources)
            async with self._publish_lock:
                self._sources[catalog_key] = sources
                return self._publish(catalog_key, materialized)

    def snapshot(self, catalog_key: CatalogKey) -> CatalogSnapshot:
        """Return the already-published snapshot without filesystem access."""
        try:
            return self._snapshots[catalog_key]
        except KeyError:
            raise KeyError(f"catalog {catalog_key!r} is not configured") from None

    async def inspect(self, app_root: Path) -> ManifestInspection:
        """Inspect one app through the shared public manifest parser."""
        return await asyncio.to_thread(parse_manifest, app_root)

    async def refresh(self, catalog_key: CatalogKey) -> CatalogSnapshot:
        """Rescan configured sources and publish only a material change."""
        async with self._operation_lock(catalog_key):
            try:
                sources = self._sources[catalog_key]
            except KeyError:
                raise KeyError(f"catalog {catalog_key!r} is not configured") from None
            materialized = await asyncio.to_thread(self._scan, sources)
            async with self._publish_lock:
                return self._publish(catalog_key, materialized)

    async def events(self, catalog_key: CatalogKey) -> AsyncIterator[CatalogEvent]:
        """Stream material refresh events for one configured catalog."""
        queue: asyncio.Queue[CatalogEvent] = asyncio.Queue()
        subscribers = self._subscribers.setdefault(catalog_key, set())
        subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(catalog_key, None)

    def _operation_lock(self, key: CatalogKey) -> asyncio.Lock:
        """Return the sequencing lock for one catalog, without cross-key waits."""
        lock = self._operation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._operation_locks[key] = lock
        return lock

    def _publish(
        self,
        key: CatalogKey,
        materialized: tuple[tuple[CatalogApp, ...], tuple[CatalogRejection, ...]],
    ) -> CatalogSnapshot:
        apps, rejections = materialized
        previous = self._snapshots.get(key)
        if (
            previous is not None
            and previous.apps == apps
            and previous.rejections == rejections
        ):
            return previous

        generation = 1 if previous is None else previous.generation + 1
        snapshot = CatalogSnapshot(
            key=key,
            generation=generation,
            apps=apps,
            rejections=rejections,
            created_at=datetime.now(timezone.utc),
        )
        self._snapshots[key] = snapshot

        if previous is not None:
            event = CatalogEvent(
                key=key,
                generation=generation,
                changed_app_ids=self._changed_app_ids(previous.apps, apps),
            )
            for queue in tuple(self._subscribers.get(key, ())):
                queue.put_nowait(event)
        return snapshot

    @staticmethod
    def _changed_app_ids(
        before: tuple[CatalogApp, ...], after: tuple[CatalogApp, ...]
    ) -> tuple[AppId, ...]:
        old = {app.manifest.app_id: app for app in before}
        new = {app.manifest.app_id: app for app in after}
        return tuple(
            AppId(app_id)
            for app_id in sorted(set(old) | set(new))
            if old.get(app_id) != new.get(app_id)
        )

    @staticmethod
    def _scan(
        sources: tuple[AppSource, ...],
    ) -> tuple[tuple[CatalogApp, ...], tuple[CatalogRejection, ...]]:
        candidates: list[tuple[int, int, CatalogApp]] = []
        rejections: list[CatalogRejection] = []

        for source_order, source in enumerate(sources):
            try:
                children = sorted(
                    (
                        child
                        for child in source.root.iterdir()
                        if child.is_dir() and (child / "app.json").is_file()
                    ),
                    key=lambda child: child.name,
                )
            except OSError as exc:
                rejections.append(
                    CatalogRejection(
                        root=source.root.resolve(strict=False),
                        app_id="",
                        reason="source_unavailable",
                        detail=str(exc),
                        source=source,
                    )
                )
                continue

            for child_order, app_root in enumerate(children):
                inspection = parse_manifest(app_root)
                if inspection.manifest is None:
                    detail = "; ".join(issue.message for issue in inspection.errors)
                    rejections.append(
                        CatalogRejection(
                            root=inspection.root,
                            app_id=app_root.name,
                            reason="invalid_manifest",
                            detail=detail or "manifest validation failed",
                            source=source,
                        )
                    )
                    continue

                manifest = inspection.manifest
                if not is_compatible(manifest.metadata.min_engine_version):
                    rejections.append(
                        CatalogRejection(
                            root=manifest.root,
                            app_id=str(manifest.app_id),
                            reason="incompatible_engine",
                            detail=(
                                "app requires app-engine "
                                f"{manifest.metadata.min_engine_version} or newer"
                            ),
                            source=source,
                        )
                    )
                    continue

                candidates.append(
                    (
                        source_order,
                        child_order,
                        CatalogApp(
                            manifest=manifest,
                            source=source,
                            compatible=True,
                            warnings=inspection.warnings,
                        ),
                    )
                )

        # Ranking is explicit rather than dependent on traversal order.  A
        # stable source-order tie breaker makes source configuration meaningful.
        grouped: dict[AppId, list[tuple[int, int, CatalogApp]]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate[2].manifest.app_id, []).append(candidate)

        winners: dict[AppId, tuple[int, int, CatalogApp]] = {}
        for app_id, matches in grouped.items():
            ranked = sorted(
                matches,
                key=lambda item: (-item[2].source.priority, item[0], item[1]),
            )
            winner = ranked[0]
            winners[app_id] = winner
            winner_app = winner[2]
            for loser in ranked[1:]:
                loser_app = loser[2]
                rejections.append(
                    CatalogRejection(
                        root=loser_app.manifest.root,
                        app_id=str(loser_app.manifest.app_id),
                        reason="duplicate_app_id",
                        detail=(
                            f"duplicate app id is shadowed by "
                            f"{winner_app.manifest.root}"
                        ),
                        source=loser_app.source,
                    )
                )

        apps = tuple(
            item[2]
            for item in sorted(
                winners.values(),
                key=lambda item: (
                    -item[2].source.priority,
                    item[0],
                    item[1],
                    str(item[2].manifest.app_id),
                ),
            )
        )
        return apps, tuple(rejections)


__all__ = ["ImmutableAppCatalog"]
