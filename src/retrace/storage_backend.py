"""P1.5 — Storage backend abstraction (FOUNDATION SLICE).

The existing `retrace.storage.Storage` class is 7,000+ lines and
hard-codes SQLite (`sqlite3.Connection`, `?`-style parameters,
`datetime('now', '-N seconds')` modifiers). SQLite is fine to
~100k events/day; past that, contention bites and self-host teams
will want Postgres.

This module ships the **chassis** for that migration:

  - `Backend` (Protocol) — the small connection-level surface every
    storage backend must satisfy. Deliberately small; concrete
    Storage methods stay where they are and call into the backend
    for parameter style + SQL-dialect translation as we slice tables
    over in follow-up PRs.

  - `SqliteBackend` — wraps the existing sqlite3 semantics so the
    Protocol is satisfied without any behavior change.

  - `PostgresBackend` — stub. Lazy-imports `psycopg` so users
    without the `[postgres]` extra still `import retrace` cleanly.
    Methods raise `NotImplementedError` until a follow-up slice
    implements them. The point of including the stub now is to
    fail loudly with a clear message on a `postgresql://` URL
    instead of mystery `sqlite3` errors.

  - `backend_from_url(url, **kwargs) -> Backend` — factory.
    `sqlite:///<path>` or a bare path → `SqliteBackend`.
    `postgresql://...` (or `postgres://`) → `PostgresBackend`.

**This PR does not change Storage behavior.** Every test that
worked before still works because the existing class never touches
this module. The foundation slice lets future PRs (one per table /
table-cluster) flip to the backend abstraction incrementally with
no flag-day risk.

Inspired by Dagster's `python_modules/dagster/dagster/_core/storage/`
multi-backend pattern.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit


_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+psycopg"})

# Matches `scheme:/foo` (single slash) — a malformed URI we want to
# reject, not silently route to SQLite as a path with colons in it.
# `[a-z][a-z0-9+.-]+:` is the RFC 3986 scheme grammar, followed by
# a single slash (URLs need `://`).
_SCHEME_LIKE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:/", re.IGNORECASE)

# Carve-out for Windows drive letters (`C:/foo`, `D:\bar`) so a
# Windows user can still pass a bare drive path. Always uppercase /
# lowercase single letter — anything else is a scheme.
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")


@dataclass(frozen=True)
class ParsedDsn:
    """Normalized storage URL — what every backend needs to open."""

    scheme: str
    path: str = ""        # for sqlite: file path. "" / ":memory:" allowed.
    host: str = ""
    port: Optional[int] = None
    user: str = ""
    password: str = ""
    database: str = ""    # for postgres: database name.

    def is_sqlite(self) -> bool:
        return self.scheme == "sqlite" or self.scheme == ""

    def is_postgres(self) -> bool:
        return self.scheme in _POSTGRES_SCHEMES


def parse_storage_url(url: str | Path) -> ParsedDsn:
    """Accept SQLAlchemy-ish storage URLs.

    Forms:
      - `sqlite:///abs/or/rel/path.db` → SQLite at that path
      - `sqlite:///:memory:` or `sqlite://` → in-memory SQLite
      - `/literal/path/to.db` or `Path(...)` → SQLite (back-compat)
      - `postgresql://user:pass@host:port/dbname` → Postgres

    Anything else raises `ValueError`.
    """
    if isinstance(url, Path):
        return ParsedDsn(scheme="sqlite", path=str(url))
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("storage URL cannot be empty")

    # Bare path (back-compat): `Storage(Path("data/retrace.db"))` worked
    # before and should still resolve as SQLite. But a malformed URL
    # like `postgresql:/db` (single slash — missing the second one)
    # must NOT silently become a SQLite path that creates a local
    # `postgresql:` file — fail loudly. Windows drive letters
    # (`C:/foo`) get a carve-out. (CodeRabbit Major catch on PR #135.)
    if "://" not in raw:
        if _SCHEME_LIKE_RE.match(raw) and not _WINDOWS_DRIVE_RE.match(raw):
            raise ValueError(
                f"malformed storage URL {raw!r} "
                "(URLs use `scheme://...`; for a file path use a bare "
                "path or `sqlite:///<path>`)"
            )
        return ParsedDsn(scheme="sqlite", path=raw)

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme == "sqlite":
        # `sqlite:///path` puts the leading slash IN parts.path because
        # SQLite URLs have a triple slash. `sqlite://` (two slashes,
        # empty path) means in-memory.
        path = parts.path or ""
        # `sqlite:///:memory:` should be honored verbatim.
        if path.lstrip("/") == ":memory:" or path == "":
            return ParsedDsn(scheme="sqlite", path=":memory:")
        # Strip the single leading slash from `sqlite:///foo/bar.db`
        # to recover `foo/bar.db`. The path is already absolute if the
        # user wrote `sqlite:////abs/path`.
        if path.startswith("/") and not path.startswith("//"):
            path = path[1:] if len(parts.netloc) == 0 else parts.netloc + path
        return ParsedDsn(scheme="sqlite", path=path)
    if scheme in _POSTGRES_SCHEMES:
        database = (parts.path or "").lstrip("/")
        return ParsedDsn(
            scheme=scheme,
            host=parts.hostname or "",
            port=parts.port,
            user=parts.username or "",
            password=parts.password or "",
            database=database,
        )
    raise ValueError(
        f"unsupported storage URL scheme {scheme!r} "
        "(expected `sqlite://`, bare path, or `postgresql://`)"
    )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """Minimum surface every storage backend must satisfy.

    This is intentionally small. Concrete Storage methods stay where
    they are — they call `backend.connect()` for a context-managed
    DB connection and use `backend.placeholder()` to choose `?` vs
    `%s`. Higher-level helpers (sliding-window timestamps, JSON
    columns, upserts) get added to this Protocol as we slice tables
    over in future PRs.
    """

    name: str   # `"sqlite"` or `"postgres"`

    def connect(self) -> "ConnectionContext":
        """Acquire a connection. Must support `with backend.connect() as conn:`."""
        ...

    def placeholder(self) -> str:
        """Return the parameter placeholder for prepared statements.
        `"?"` for SQLite, `"%s"` for Postgres."""
        ...

    def now_minus_seconds_sql(self, seconds_param: str) -> str:
        """Return a dialect-correct expression for `now() - N seconds`.

        SQLite: `datetime('now', '-N seconds')` where N is interpolated
        via param.
        Postgres: `now() - interval %s`.

        Used by recency queries (alert dedup window, recent dispatches).
        Caller provides the placeholder name, the backend assembles
        the rest.
        """
        ...


@runtime_checkable
class ConnectionContext(Protocol):
    """`with backend.connect() as conn:` returns this."""

    def __enter__(self) -> Any: ...
    def __exit__(self, *exc_info) -> None: ...


# ---------------------------------------------------------------------------
# SQLite backend (default, no behavior change)
# ---------------------------------------------------------------------------


class SqliteBackend:
    """Adapter over the existing `sqlite3.connect(...)` semantics.

    Mirrors what the existing `Storage._conn()` does: `row_factory =
    sqlite3.Row`, autocommit on context exit. Use `dsn` to control
    the file path; pass `":memory:"` for a private database.
    """

    name = "sqlite"

    def __init__(self, dsn: ParsedDsn):
        if not dsn.is_sqlite():
            raise ValueError(f"SqliteBackend requires sqlite DSN, got {dsn.scheme!r}")
        self.dsn = dsn
        self.path = dsn.path or ":memory:"
        # Mirror Storage's existing parent-dir creation behaviour so
        # the foundation backend is a drop-in for the path it owns.
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def placeholder(self) -> str:
        return "?"

    def now_minus_seconds_sql(self, seconds_param: str) -> str:
        # `datetime('now', ?-seconds)` is awkward to interpolate; the
        # existing code passes `f"-{n} seconds"` as the parameter value.
        # We preserve that convention.
        return f"datetime('now', {seconds_param})"


# ---------------------------------------------------------------------------
# Postgres backend (stub — fails loudly until follow-up slices implement it)
# ---------------------------------------------------------------------------


class PostgresBackend:
    """Stub Postgres backend.

    `postgresql://...` URLs are accepted by `parse_storage_url` so
    callers can configure a future Postgres install today. Until the
    follow-up table-slice PRs land, every method raises
    `NotImplementedError` with a pointer to the roadmap item.

    Why ship the stub now: it lets users get a clear "Postgres is on
    the roadmap, not yet implemented — slice #1 coming in PR N" error
    instead of a confusing `sqlite3.OperationalError` when they
    point Retrace at Postgres.

    The `psycopg` import is deferred to `connect()` so a stock
    `import retrace.storage_backend` works without the `[postgres]`
    extra installed.
    """

    name = "postgres"

    def __init__(self, dsn: ParsedDsn):
        if not dsn.is_postgres():
            raise ValueError(f"PostgresBackend requires postgres DSN, got {dsn.scheme!r}")
        if not dsn.host:
            raise ValueError("PostgresBackend DSN missing host")
        if not dsn.database:
            raise ValueError("PostgresBackend DSN missing database name")
        self.dsn = dsn

    def connect(self) -> Any:  # pragma: no cover - stub
        try:
            import psycopg  # noqa: F401  (availability check only)
        except ImportError as exc:
            raise NotImplementedError(
                "PostgresBackend requires `pip install 'retrace[postgres]'`. "
                "The Postgres backend is the P1.5 roadmap item; only the URL "
                "scheme is accepted today — implementation lands in follow-up "
                "table-slice PRs."
            ) from exc
        raise NotImplementedError(
            "PostgresBackend.connect is not yet implemented. See "
            "`docs/roadmap.md` P1.5 for the per-table migration plan."
        )

    def placeholder(self) -> str:
        return "%s"

    def now_minus_seconds_sql(self, seconds_param: str) -> str:
        # psycopg's `interval` syntax. `seconds_param` is expected to
        # be the placeholder itself (`%s`) with the parameter value
        # being the integer seconds.
        return f"now() - ({seconds_param} * interval '1 second')"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def backend_from_url(url: str | Path) -> Backend:
    """Pick the right Backend implementation for a storage URL.

    Bare paths and `sqlite://...` URLs return `SqliteBackend`.
    `postgresql://...` URLs return `PostgresBackend` (which raises a
    clean `NotImplementedError` until the per-table slices land).
    Anything else raises `ValueError`.
    """
    dsn = parse_storage_url(url)
    if dsn.is_sqlite():
        return SqliteBackend(dsn)
    if dsn.is_postgres():
        return PostgresBackend(dsn)
    # parse_storage_url should have raised already, but be explicit.
    raise ValueError(f"unsupported scheme {dsn.scheme!r}")


__all__ = [
    "Backend",
    "ConnectionContext",
    "ParsedDsn",
    "PostgresBackend",
    "SqliteBackend",
    "backend_from_url",
    "parse_storage_url",
]
