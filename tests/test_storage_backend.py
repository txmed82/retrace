"""P1.5 foundation: storage backend chassis tests.

This slice is intentionally a thin module — `Backend` Protocol +
`SqliteBackend` + `PostgresBackend` stub + URL factory. The existing
`Storage` class is unchanged. These tests pin the chassis contract
so future per-table migration slices can lean on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from retrace.storage_backend import (
    Backend,
    ParsedDsn,
    PostgresBackend,
    SqliteBackend,
    backend_from_url,
    parse_storage_url,
)


# ---------------------------------------------------------------------------
# parse_storage_url
# ---------------------------------------------------------------------------


def test_parse_storage_url_bare_path():
    """A bare filesystem path (no scheme) is treated as SQLite."""
    dsn = parse_storage_url("data/retrace.db")
    assert dsn.is_sqlite()
    assert dsn.path == "data/retrace.db"


def test_parse_storage_url_pathlib_path(tmp_path: Path):
    """`Storage(Path(...))` is the back-compat shape."""
    dsn = parse_storage_url(tmp_path / "retrace.db")
    assert dsn.is_sqlite()
    assert dsn.path.endswith("retrace.db")


def test_parse_storage_url_sqlite_url():
    dsn = parse_storage_url("sqlite:///abs/path/x.db")
    assert dsn.is_sqlite()
    assert dsn.path == "abs/path/x.db"


def test_parse_storage_url_sqlite_in_memory():
    """`sqlite:///:memory:` and the bare `sqlite://` form both
    resolve to `":memory:"`."""
    assert parse_storage_url("sqlite:///:memory:").path == ":memory:"
    assert parse_storage_url("sqlite://").path == ":memory:"


def test_parse_storage_url_postgres():
    dsn = parse_storage_url("postgresql://alice:secret@db.example.com:5432/retrace")
    assert dsn.is_postgres()
    assert dsn.host == "db.example.com"
    assert dsn.port == 5432
    assert dsn.user == "alice"
    assert dsn.password == "secret"
    assert dsn.database == "retrace"


def test_parse_storage_url_postgres_alias():
    """`postgres://` is the old-style alias; we still accept it."""
    dsn = parse_storage_url("postgres://host/dbname")
    assert dsn.is_postgres()
    assert dsn.scheme in {"postgres", "postgresql"}


@pytest.mark.parametrize("bad", ["", "mysql://x/y", "redis://x", "https://example.com/db"])
def test_parse_storage_url_rejects_unknown_schemes(bad):
    """Anything-but-sqlite-or-postgres URL schemes raise, but a bare
    path like `foo:bar` is allowed (back-compat — treated as SQLite
    file path since there's no `://`)."""
    with pytest.raises(ValueError):
        parse_storage_url(bad)


def test_parse_storage_url_bare_string_with_colon_is_sqlite_path():
    """A bare string with a colon but no slash-after is a SQLite path
    — SQLite accepts paths containing colons (rare but legal). The
    malformed-URL guard only fires for `scheme:/...` shapes."""
    dsn = parse_storage_url("foo:bar.db")
    assert dsn.is_sqlite()
    assert dsn.path == "foo:bar.db"


@pytest.mark.parametrize(
    "bad",
    [
        "postgresql:/prod/db",   # single slash — clearly meant `://`
        "sqlite:/path/db",        # same
        "mysql:/somewhere",       # foreign scheme + single slash
    ],
)
def test_parse_storage_url_rejects_single_slash_schemes(bad):
    """Regression for CodeRabbit Major on PR #135: an input like
    `postgresql:/prod/db` (single slash — clearly meant `postgresql://`)
    must NOT silently become a SQLite path that creates a local
    `postgresql:` file. Fail loudly instead."""
    with pytest.raises(ValueError, match="malformed"):
        parse_storage_url(bad)


def test_parse_storage_url_allows_windows_drive_letters():
    """`C:/foo/bar.db` is a Windows path, not a malformed URL."""
    assert parse_storage_url("C:/data/retrace.db").is_sqlite()
    assert parse_storage_url(r"D:\data\retrace.db").is_sqlite()


# ---------------------------------------------------------------------------
# SqliteBackend
# ---------------------------------------------------------------------------


def test_sqlite_backend_protocol_compliance():
    """`SqliteBackend` is a `Backend` per `runtime_checkable`."""
    b = SqliteBackend(ParsedDsn(scheme="sqlite", path=":memory:"))
    assert isinstance(b, Backend)
    assert b.name == "sqlite"
    assert b.placeholder() == "?"


def test_sqlite_backend_connects_to_memory():
    b = SqliteBackend(ParsedDsn(scheme="sqlite", path=":memory:"))
    conn = b.connect()
    try:
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.execute("INSERT INTO x VALUES (1)")
        row = conn.execute("SELECT id FROM x").fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_sqlite_backend_creates_parent_directory(tmp_path: Path):
    """Matches the existing Storage behaviour: parent dir is created
    so the user doesn't have to mkdir first."""
    path = tmp_path / "nested" / "data" / "retrace.db"
    b = SqliteBackend(ParsedDsn(scheme="sqlite", path=str(path)))
    assert path.parent.is_dir()
    conn = b.connect()
    conn.execute("CREATE TABLE x (id INTEGER)")
    conn.close()
    assert path.exists()


def test_sqlite_backend_now_minus_seconds_sql_uses_datetime_modifier():
    b = SqliteBackend(ParsedDsn(scheme="sqlite", path=":memory:"))
    sql = b.now_minus_seconds_sql("?")
    assert sql == "datetime('now', ?)"


def test_sqlite_backend_rejects_postgres_dsn():
    with pytest.raises(ValueError, match="sqlite DSN"):
        SqliteBackend(ParsedDsn(scheme="postgresql", host="x", database="x"))


# ---------------------------------------------------------------------------
# PostgresBackend (stub)
# ---------------------------------------------------------------------------


def test_postgres_backend_protocol_compliance():
    """Even the stub satisfies the Protocol — chassis is in place."""
    b = PostgresBackend(ParsedDsn(scheme="postgresql", host="x", database="y"))
    assert isinstance(b, Backend)
    assert b.name == "postgres"
    assert b.placeholder() == "%s"


def test_postgres_backend_now_minus_seconds_uses_interval():
    """SQL string is the dialect-correct form even though connect()
    is unimplemented."""
    b = PostgresBackend(ParsedDsn(scheme="postgresql", host="x", database="y"))
    sql = b.now_minus_seconds_sql("%s")
    assert "interval" in sql.lower()
    assert "%s" in sql


def test_postgres_backend_rejects_sqlite_dsn():
    with pytest.raises(ValueError, match="postgres DSN"):
        PostgresBackend(ParsedDsn(scheme="sqlite", path=":memory:"))


def test_postgres_backend_requires_host_and_database():
    with pytest.raises(ValueError, match="host"):
        PostgresBackend(ParsedDsn(scheme="postgresql", host="", database="x"))
    with pytest.raises(ValueError, match="database"):
        PostgresBackend(ParsedDsn(scheme="postgresql", host="x", database=""))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_backend_from_url_picks_sqlite():
    b = backend_from_url("sqlite:///:memory:")
    assert isinstance(b, SqliteBackend)


def test_backend_from_url_picks_postgres():
    """The URL is accepted today even though the backend can't connect yet."""
    b = backend_from_url("postgresql://x/y")
    assert isinstance(b, PostgresBackend)


def test_backend_from_url_bare_path_is_sqlite(tmp_path: Path):
    b = backend_from_url(tmp_path / "x.db")
    assert isinstance(b, SqliteBackend)


def test_backend_from_url_unsupported_raises():
    with pytest.raises(ValueError):
        backend_from_url("mongodb://localhost/x")


# ---------------------------------------------------------------------------
# Storage(...) integration — chassis only, no behavior change
# ---------------------------------------------------------------------------


def test_storage_accepts_sqlite_url(tmp_path: Path):
    """A `sqlite:///path` URL must work the same as the bare Path()
    form — the foundation slice is purely additive."""
    from retrace.storage import Storage

    db = tmp_path / "x.db"
    store = Storage(f"sqlite:///{db}")
    store.init_schema()
    assert db.exists()


def test_storage_accepts_pathlib_path(tmp_path: Path):
    """The back-compat shape still works."""
    from retrace.storage import Storage

    store = Storage(tmp_path / "y.db")
    store.init_schema()
    assert (tmp_path / "y.db").exists()


def test_storage_accepts_postgres_url_at_construct_time(tmp_path: Path):
    """P1.5 finished: Storage now accepts Postgres URLs. The actual
    connect happens lazily on the first `_conn()` call; if the
    server isn't running, that's a `psycopg.OperationalError` —
    a real network error, not a construct-time refusal."""
    from retrace.storage import Storage

    store = Storage("postgresql://user:pass@localhost:5432/retrace")
    assert store.backend_name == "postgres"
