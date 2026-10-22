"""Client TLS identities scoped to operator-configured HTTPS origins."""

from __future__ import annotations

import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError()
        port = parts.port if parts.port is not None else 443
        if port == 0:
            raise ValueError()
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        return f"https://{host}:{port}"
    except (ValueError, AttributeError) as exc:
        raise ValueError("mTLS endpoint must use HTTPS without embedded credentials") from exc


class OutboundTlsPolicy:
    def __init__(self, profiles: dict[str, ssl.SSLContext] | None = None):
        self._profiles = profiles or {}

    @classmethod
    def from_file(cls, path: Path) -> OutboundTlsPolicy:
        try:
            data = json.loads(Path(path).read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Outbound mTLS configuration could not be read") from exc
        if not isinstance(data, dict) or not data:
            raise ValueError("Outbound mTLS configuration must map HTTPS origins to profiles")
        profiles: dict[str, ssl.SSLContext] = {}
        for configured_origin, profile in data.items():
            if not isinstance(configured_origin, str):
                raise ValueError("Outbound mTLS origin must use HTTPS")
            origin = _origin(configured_origin)
            parts = urlsplit(configured_origin)
            if parts.path or parts.query or parts.fragment or configured_origin.rstrip("/") != configured_origin:
                raise ValueError("Outbound mTLS origin must be an HTTPS origin without a path")
            if origin in profiles:
                raise ValueError("Duplicate outbound mTLS origin")
            if not isinstance(profile, dict) or set(profile) != {"ca_file", "cert_file", "key_file"}:
                raise ValueError("Each profile requires ca_file, cert_file, and key_file")
            paths = {}
            for key in ("ca_file", "cert_file", "key_file"):
                value = profile[key]
                if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
                    raise ValueError(f"Outbound mTLS {key} must name an existing absolute file")
                paths[key] = value
            try:
                context = ssl.create_default_context(cafile=paths["ca_file"])
                context.load_cert_chain(paths["cert_file"], paths["key_file"])
            except (OSError, ssl.SSLError) as exc:
                raise ValueError(f"Outbound mTLS certificate profile for {origin} is invalid") from exc
            profiles[origin] = context
        return cls(profiles)

    def for_origin(self, url: str) -> ssl.SSLContext | None:
        try:
            return self._profiles.get(_origin(url))
        except ValueError:
            return None
