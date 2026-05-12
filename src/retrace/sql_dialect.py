"""P1.5 — SQL dialect translation layer.

The existing storage code in `storage.py` is sqlite3-flavored:
`?` placeholders, `datetime('now', ?)`, `INSERT OR IGNORE`,
`INSERT OR REPLACE`. Rewriting all ~290 call sites is the
flag-day refactor the roadmap warns against.

Instead, we translate at execute time. A `WrappedConnection` proxy
intercepts every `.execute(sql, params)` call, runs the SQL through
`translate_sql(sql, backend)`, and forwards to the underlying
psycopg or sqlite3 connection.

What the translator covers today:
  * `?` → `%s` (parameter placeholders)
  * `datetime('now', ?)` → `(now() + ((%s)::text)::interval)` (sliding-window queries)
  * `datetime('now')` → `now()` (server-side timestamps)
  * `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`
  * `executescript()` → run statements one at a time
  * SCHEMA-level: `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL PRIMARY KEY`

What we deliberately don't try to handle today:
  * `INSERT OR REPLACE` — rare; surfaces as a clear error and gets
    fixed per-call-site in follow-up slices.
  * `PRAGMA table_info(...)` — used by `init_schema`'s lightweight
    migrations. The schema runner skips those migrations on Postgres.
  * Function differences (`json_extract`, `printf`) — none used in
    the current schema; revisit when a slice surfaces one.

Goal: this layer + a portable SCHEMA + a real `PostgresBackend.connect()`
gets `Storage.init_schema()` + basic CRUD working against Postgres
without rewriting any of the 180 Storage methods.
"""

from __future__ import annotations

import re
from typing import Any


# Translation regexes are run in order. They're intentionally small
# and surgical — bigger changes deserve a real parser (sqlglot),
# but our query shape is narrow enough that targeted replacements
# are safer.

_QMARK_RE = re.compile(r"\?")

# `datetime('now', ?)` with a single param like `-300 seconds`.
_DT_NOW_PARAM_RE = re.compile(
    r"datetime\(\s*'now'\s*,\s*\?\s*\)",
    re.IGNORECASE,
)

_DT_NOW_BARE_RE = re.compile(
    r"datetime\(\s*'now'\s*\)",
    re.IGNORECASE,
)

# `INSERT OR IGNORE` — common in our schema for dedup-on-write.
_INSERT_OR_IGNORE_RE = re.compile(
    r"\bINSERT\s+OR\s+IGNORE\b",
    re.IGNORECASE,
)


def translate_sql(sql: str, *, dialect: str) -> str:
    """Rewrite SQLite-flavored SQL for the target dialect.

    `dialect` is `"sqlite"` (no-op) or `"postgres"`.

    The function is idempotent — running it twice produces the same
    output. Tested for the SQL shapes our codebase actually uses.
    """
    if dialect == "sqlite":
        return sql
    if dialect != "postgres":
        raise ValueError(f"unknown dialect: {dialect!r}")

    out = sql

    # `datetime('now', ?)` → an ISO-text expression that compares
    # against the TEXT `created_at` columns. SQLite's `datetime()`
    # produces a string; we mirror that with `to_char(...)`. The
    # caller passes `f"-{n} seconds"` as the param; PG parses that
    # as an interval literal.
    out = _DT_NOW_PARAM_RE.sub(
        "to_char((now() + ((?)::text)::interval) AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD\"T\"HH24:MI:SS.US')",
        out,
    )

    # `datetime('now')` → ISO-text of current UTC. Same reason: the
    # TEXT columns expect ISO strings.
    out = _DT_NOW_BARE_RE.sub(
        "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US')",
        out,
    )

    # `INSERT OR IGNORE` → `INSERT` with a trailing `ON CONFLICT DO NOTHING`.
    # PG requires the conflict clause AT THE END of the statement, so we
    # rewrite the prefix here and append the conflict clause after the
    # placeholder pass. The marker `<<RETRACE_ON_CONFLICT_DO_NOTHING>>`
    # is appended below.
    if _INSERT_OR_IGNORE_RE.search(out):
        out = _INSERT_OR_IGNORE_RE.sub("INSERT", out)
        # Add a sentinel that the second pass picks up.
        out = f"{out.rstrip().rstrip(';')}\nON CONFLICT DO NOTHING"

    # Last pass: `?` → `%s`. Done AFTER the datetime / on-conflict
    # rewrites so those rewrites can use `?` themselves and let this
    # pass canonicalize the result.
    out = _QMARK_RE.sub("%s", out)

    return out


# ---------------------------------------------------------------------------
# WrappedConnection — sqlite3-shaped surface over psycopg
# ---------------------------------------------------------------------------


class WrappedCursor:
    """Wraps a psycopg cursor so it quacks like sqlite3.Cursor:

    - `.execute(sql, params)` translates SQL on the way in.
    - `.fetchone()` / `.fetchall()` return rows that support both
      tuple indexing and `row[column_name]` indexing (sqlite3.Row
      semantics).
    """

    def __init__(self, pg_cursor: Any, dialect: str):
        self._cur = pg_cursor
        self._dialect = dialect

    def execute(self, sql: str, params: Any = ()) -> "WrappedCursor":
        translated = translate_sql(sql, dialect=self._dialect)
        if params is None:
            params = ()
        self._cur.execute(translated, params)
        return self

    def executemany(self, sql: str, params_seq: Any) -> "WrappedCursor":
        translated = translate_sql(sql, dialect=self._dialect)
        self._cur.executemany(translated, params_seq)
        return self

    def fetchone(self) -> Any:
        row = self._cur.fetchone()
        return _wrap_row(row, self._cur.description) if row is not None else None

    def fetchall(self) -> list:
        return [_wrap_row(r, self._cur.description) for r in self._cur.fetchall()]

    def fetchmany(self, size: int = 1) -> list:
        return [_wrap_row(r, self._cur.description) for r in self._cur.fetchmany(size)]

    @property
    def lastrowid(self) -> int | None:
        # psycopg doesn't expose lastrowid uniformly — INTEGER PRIMARY
        # KEY AUTOINCREMENT becomes BIGSERIAL on PG, and you fetch the
        # generated id via `RETURNING id`. Callers that need lastrowid
        # for an autoincrement insert have to switch to RETURNING.
        return None

    @property
    def rowcount(self) -> int:
        return int(self._cur.rowcount or 0)

    def close(self) -> None:
        self._cur.close()


class WrappedConnection:
    """psycopg connection wrapped to look like a sqlite3.Connection.

    Supports the subset of the sqlite3 API our Storage class uses:

      - `.execute(sql, params)` (autocommit-style)
      - `.executemany(sql, params_seq)`
      - `.executescript(sql)` (semicolon-split, run one stmt at a time)
      - `with conn:` context manager (commits on exit, rolls back on exc)
      - `.close()`
      - `.row_factory` settable (no-op on PG — rows already act like
        sqlite3.Row via `_wrap_row`)
    """

    row_factory: Any = None

    def __init__(self, pg_conn: Any, dialect: str = "postgres"):
        self._conn = pg_conn
        self._dialect = dialect

    def execute(self, sql: str, params: Any = ()) -> WrappedCursor:
        cur = self._conn.cursor()
        wrapped = WrappedCursor(cur, self._dialect)
        return wrapped.execute(sql, params)

    def executemany(self, sql: str, params_seq: Any) -> WrappedCursor:
        cur = self._conn.cursor()
        wrapped = WrappedCursor(cur, self._dialect)
        return wrapped.executemany(sql, params_seq)

    def executescript(self, sql: str) -> None:
        """sqlite3's executescript runs a multi-statement string with
        no parameters and no implicit transactions. We split on `;`
        boundaries and run statements individually."""
        for stmt in _split_statements(sql):
            stmt = stmt.strip()
            if not stmt:
                continue
            translated = translate_sql(stmt, dialect=self._dialect)
            with self._conn.cursor() as cur:
                cur.execute(translated)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "WrappedConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        # Match sqlite3's `with conn:` semantics — the connection
        # stays open on context exit.


# ---------------------------------------------------------------------------
# Row wrapping — psycopg returns tuples; sqlite3.Row supports `row[name]`.
# ---------------------------------------------------------------------------


class _MappedRow:
    """Read-only row supporting both `row[0]` and `row["name"]`."""

    __slots__ = ("_values", "_columns")

    def __init__(self, values: tuple, columns: tuple[str, ...]):
        self._values = values
        self._columns = columns

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        if isinstance(key, str):
            try:
                idx = self._columns.index(key)
            except ValueError as exc:
                raise KeyError(key) from exc
            return self._values[idx]
        raise TypeError(f"row index must be int or str, got {type(key).__name__}")

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> tuple[str, ...]:
        return self._columns

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, str):
            return key in self._columns
        return key in self._values

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Row {dict(zip(self._columns, self._values))!r}>"


def _wrap_row(row: Any, description: Any) -> Any:
    """Wrap a psycopg row (tuple-shaped) so it accepts string keys."""
    if row is None:
        return None
    if description is None:
        return row
    columns = tuple(col.name if hasattr(col, "name") else col[0] for col in description)
    return _MappedRow(tuple(row), columns)


# ---------------------------------------------------------------------------
# Statement splitter for executescript
# ---------------------------------------------------------------------------


def _split_statements(sql: str) -> list[str]:
    """Split a multi-statement SQL string into individual statements.

    Respects:
      * String literals (`'...'`, `"..."`) including doubled-quote
        escapes.
      * Line comments (`-- ...` to end of line) — a `;` inside a
        line comment is NOT a statement boundary. This matters
        because our schema has comment blocks with semicolons in
        the prose, like `-- table; every matching...`.
      * Block comments (`/* ... */`) — same rule.

    SQLite's `executescript` is permissive; our split needs to be
    at least as permissive for the schema strings we feed it.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    in_line_comment = False
    in_block_comment = False
    string_char = ""
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            buf.append(ch)
            if ch == string_char:
                # Look-ahead for doubled-quote escape (`''` inside string).
                if nxt == string_char:
                    buf.append(nxt)
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        # Not inside a string or comment.
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch in ("'", '"'):
            in_string = True
            string_char = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


__all__ = [
    "WrappedConnection",
    "WrappedCursor",
    "translate_sql",
]
