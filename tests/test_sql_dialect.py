"""P1.5 — SQL dialect translation tests (works offline, no Postgres
server needed).
"""

from __future__ import annotations

import pytest

from retrace.sql_dialect import WrappedConnection, translate_sql
from retrace.sql_schema import translate_schema


# ---------------------------------------------------------------------------
# translate_sql
# ---------------------------------------------------------------------------


def test_translate_sqlite_dialect_is_no_op():
    sql = "SELECT * FROM x WHERE id = ? AND created_at >= datetime('now', ?)"
    assert translate_sql(sql, dialect="sqlite") == sql


def test_translate_qmark_to_format():
    sql = "SELECT * FROM x WHERE id = ? AND name = ?"
    out = translate_sql(sql, dialect="postgres")
    assert "?" not in out
    assert out.count("%s") == 2


def test_translate_datetime_now_with_param():
    """`datetime('now', ?)` translates to an ISO-text expression that
    compares correctly against the TEXT `created_at` columns
    (lexicographic ISO-8601 ordering)."""
    sql = "SELECT * FROM x WHERE created_at >= datetime('now', ?)"
    out = translate_sql(sql, dialect="postgres")
    assert "datetime" not in out
    assert "now()" in out
    assert "interval" in out
    assert "to_char" in out          # produces ISO-text, not timestamptz
    assert out.count("%s") == 1


def test_translate_datetime_now_bare():
    sql = "INSERT INTO x (created_at) VALUES (datetime('now'))"
    out = translate_sql(sql, dialect="postgres")
    assert "datetime('now')" not in out
    assert "now()" in out
    assert "to_char" in out


def test_translate_insert_or_ignore():
    """`INSERT OR IGNORE` becomes `INSERT ... ON CONFLICT DO NOTHING`."""
    sql = "INSERT OR IGNORE INTO x (id, name) VALUES (?, ?)"
    out = translate_sql(sql, dialect="postgres")
    assert "INSERT OR IGNORE" not in out
    assert "ON CONFLICT DO NOTHING" in out
    assert out.count("%s") == 2


def test_translate_insert_or_ignore_preserves_trailing_semicolon_removal():
    """The on-conflict clause appends correctly even when the original
    statement ended with a semicolon."""
    sql = "INSERT OR IGNORE INTO x VALUES (?);"
    out = translate_sql(sql, dialect="postgres")
    assert out.rstrip().endswith("ON CONFLICT DO NOTHING")


def test_translate_unknown_dialect_raises():
    with pytest.raises(ValueError):
        translate_sql("SELECT 1", dialect="mysql")


def test_translate_sql_idempotent_on_already_translated():
    """Running the translator twice doesn't double-rewrite. (Don't
    care about the exact behavior here, just that it doesn't crash
    or generate gibberish like `%%s`.)"""
    sql = "SELECT * FROM x WHERE id = ?"
    once = translate_sql(sql, dialect="postgres")
    twice = translate_sql(once, dialect="postgres")
    # The second pass has no `?` to rewrite, so the result is stable.
    assert twice == once
    assert "%%s" not in twice


# ---------------------------------------------------------------------------
# translate_schema
# ---------------------------------------------------------------------------


def test_schema_translate_autoincrement():
    schema = "CREATE TABLE x (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)"
    out = translate_schema(schema, dialect="postgres")
    assert "AUTOINCREMENT" not in out
    assert "BIGSERIAL PRIMARY KEY" in out


def test_schema_translate_datetime_default():
    """`DEFAULT (datetime('now'))` → an ISO-text expression so a
    TEXT column with a current-timestamp default works on PG (PG
    won't implicitly cast timestamptz → text)."""
    schema = "CREATE TABLE x (created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    out = translate_schema(schema, dialect="postgres")
    assert "datetime('now')" not in out
    assert "now()" in out
    assert "to_char" in out


def test_schema_translate_strftime_iso_default():
    """`DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))` — SQLite's
    ISO-8601 millisecond default. Regression for the PG smoke
    failure on PR #136: I'd only translated `datetime('now')` but
    missed `strftime(...)` which produces the same kind of value
    with explicit millisecond precision.
    """
    schema = (
        "CREATE TABLE x (created_at TEXT NOT NULL DEFAULT "
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    out = translate_schema(schema, dialect="postgres")
    assert "strftime" not in out
    assert "to_char" in out
    # Trailing `Z` literal must survive — clients depend on the
    # ISO suffix to recognize UTC.
    assert '"Z"' in out
    # Format string must use millisecond precision (SQLite's `%f`).
    assert "MS" in out


def test_schema_sqlite_dialect_is_no_op():
    schema = "CREATE TABLE x (id INTEGER PRIMARY KEY AUTOINCREMENT)"
    assert translate_schema(schema, dialect="sqlite") == schema


def test_schema_unknown_dialect_raises():
    with pytest.raises(ValueError):
        translate_schema("CREATE TABLE x (id INTEGER)", dialect="mysql")


# ---------------------------------------------------------------------------
# WrappedConnection / WrappedCursor
# ---------------------------------------------------------------------------


class _FakeCursor:
    """In-memory psycopg-shaped cursor for unit tests — no real PG."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.fetched: list = []
        self.description = None
        self.rowcount = 0
        self.closed = False

    def execute(self, sql: str, params=()):
        self.calls.append((sql, tuple(params or ())))
        self.rowcount = 1

    def executemany(self, sql: str, params_seq):
        for p in params_seq:
            self.calls.append((sql, tuple(p)))

    def fetchone(self):
        return self.fetched.pop(0) if self.fetched else None

    def fetchall(self):
        out, self.fetched = self.fetched, []
        return out

    def fetchmany(self, size: int = 1):
        return self.fetchall()[:size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_wrapped_connection_translates_on_execute():
    fake = _FakeConn()
    conn = WrappedConnection(fake, dialect="postgres")
    conn.execute("INSERT INTO x VALUES (?, ?)", ("a", "b"))
    sql, params = fake.cur.calls[-1]
    assert "%s" in sql
    assert params == ("a", "b")


def test_wrapped_connection_executescript_runs_per_statement():
    fake = _FakeConn()
    conn = WrappedConnection(fake, dialect="postgres")
    conn.executescript("CREATE TABLE a (id INTEGER); CREATE TABLE b (id INTEGER);")
    assert len(fake.cur.calls) == 2


def test_wrapped_connection_context_manager_commits_on_success():
    fake = _FakeConn()
    conn = WrappedConnection(fake, dialect="postgres")
    with conn:
        conn.execute("INSERT INTO x VALUES (?)", (1,))
    assert fake.committed is True
    assert fake.rolled_back is False


def test_wrapped_connection_context_manager_rolls_back_on_error():
    fake = _FakeConn()
    conn = WrappedConnection(fake, dialect="postgres")
    with pytest.raises(RuntimeError):
        with conn:
            conn.execute("INSERT INTO x VALUES (?)", (1,))
            raise RuntimeError("boom")
    assert fake.rolled_back is True
    assert fake.committed is False
