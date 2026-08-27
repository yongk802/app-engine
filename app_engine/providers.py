"""Target-provider registration for current and future platform kinds.

The manifest format intentionally treats ``TargetSpec.kind`` as an extension
identifier.  This registry is the runtime seam that binds those identifiers
to implementations without teaching the parser about every platform.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .contracts import TargetProvider, TargetProviderError


_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


class ProviderRegistry:
    """An explicit, host-independent registry of target providers."""

    def __init__(self, providers: Iterable[TargetProvider] = ()) -> None:
        self._providers: dict[str, TargetProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(
        self, provider: TargetProvider, *, replace: bool = False
    ) -> TargetProvider:
        """Register ``provider`` under its declared kind.

        Accidental replacement is rejected because a different provider can
        materially change build and launch behavior.  Hosts that deliberately
        override a built-in provider must opt in with ``replace=True``.
        """
        kind = getattr(provider, "kind", "")
        if not isinstance(kind, str) or _KIND_PATTERN.fullmatch(kind) is None:
            raise TargetProviderError(
                "invalid_provider_kind",
                "A target provider must declare a lowercase identifier kind.",
                remediation="Use a kind such as 'web', 'ios', or 'android'.",
            )
        if kind in self._providers and not replace:
            raise TargetProviderError(
                "provider_already_registered",
                f"A provider for target kind {kind!r} is already registered.",
                remediation="Pass replace=True only for an intentional override.",
                context=(("kind", kind),),
            )
        self._providers[kind] = provider
        return provider

    def get(self, kind: str) -> TargetProvider:
        """Return the provider for ``kind`` or a stable diagnostic error."""
        try:
            return self._providers[kind]
        except KeyError:
            raise TargetProviderError(
                "target_provider_unavailable",
                f"No target provider is registered for kind {kind!r}.",
                remediation="Install and register a provider for this target kind.",
                context=(("kind", kind),),
            ) from None

    def unregister(self, kind: str) -> TargetProvider | None:
        """Remove and return a provider, if present."""
        return self._providers.pop(kind, None)

    def kinds(self) -> tuple[str, ...]:
        """Return registered kinds in deterministic order."""
        return tuple(sorted(self._providers))

