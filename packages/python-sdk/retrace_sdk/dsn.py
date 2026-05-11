"""DSN parsing for the Retrace SDK.

Retrace's ingest path is wire-compatible with Sentry's envelope endpoint,
so we accept Sentry-style DSNs unchanged. The shape we care about:

    http://<public_key>@<host>[:port][/<path_prefix>]/<project_id>

`<public_key>` is the SDK key (`rtpk_…`). `<project_id>` is the workspace
identifier the API server uses to route ingests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


class DsnError(ValueError):
    """Raised when a DSN is missing required parts."""


@dataclass(frozen=True)
class Dsn:
    raw: str
    scheme: str
    public_key: str
    host: str
    port: Optional[int]
    path_prefix: str  # path between host and project_id, no leading/trailing /
    project_id: str

    @property
    def base_url(self) -> str:
        """`scheme://host[:port][/path_prefix]` — no trailing slash, no
        project segment. The SDK appends `/api/sentry/<project_id>/...`
        below.
        """
        netloc = self.host
        if self.port is not None:
            netloc = f"{self.host}:{self.port}"
        if self.path_prefix:
            return f"{self.scheme}://{netloc}/{self.path_prefix}"
        return f"{self.scheme}://{netloc}"

    @property
    def envelope_url(self) -> str:
        return f"{self.base_url}/api/sentry/{self.project_id}/envelope/"

    @property
    def store_url(self) -> str:
        return f"{self.base_url}/api/sentry/{self.project_id}/store/"


def parse_dsn(dsn: str) -> Dsn:
    """Parse a Sentry-compatible DSN.

    Raises `DsnError` on missing public key, host, or project id. Empty
    inputs raise too — callers should treat `None` and `""` as "no DSN
    configured" before calling.
    """
    if not dsn or not isinstance(dsn, str):
        raise DsnError("DSN is empty")
    parsed = urlparse(dsn.strip())
    if not parsed.scheme:
        raise DsnError(f"DSN has no scheme: {dsn!r}")
    if not parsed.username:
        raise DsnError(f"DSN has no public key (expected `<key>@host`): {dsn!r}")
    if not parsed.hostname:
        raise DsnError(f"DSN has no host: {dsn!r}")

    path = (parsed.path or "").strip("/")
    if not path:
        raise DsnError(f"DSN has no project id: {dsn!r}")
    # Last path segment is the project id; any leading segments are a
    # mount prefix (handy for reverse proxies).
    segments = [s for s in path.split("/") if s]
    project_id = segments[-1]
    path_prefix = "/".join(segments[:-1])

    return Dsn(
        raw=dsn,
        scheme=parsed.scheme,
        public_key=parsed.username,
        host=parsed.hostname,
        port=parsed.port,
        path_prefix=path_prefix,
        project_id=project_id,
    )
