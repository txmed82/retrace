# Study note: Postgres backend (P1.5)

## Update — 2026-05-12 follow-up PR

P1.5 is now **done in full**, not just foundation. Rather than
slice 7 tables across 7 PRs as originally planned, the follow-up
PR shipped a **SQL translation layer** that rewrites SQLite-flavored
SQL to Postgres at execute time. That means the 290+
`conn.execute("... ? ...")` call sites in the existing 7,346-line
Storage class **don't change** — they all flow through
`WrappedConnection.execute()` which calls `translate_sql()` first.

Translations covered:

| SQLite shape                     | Postgres rewrite                                   |
|----------------------------------|----------------------------------------------------|
| `?`                              | `%s`                                               |
| `datetime('now', ?)`             | `to_char((now()+(?::text)::interval) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')` |
| `datetime('now')`                | Same `to_char(...)` form, no param                 |
| `INSERT OR IGNORE`               | `INSERT ... ON CONFLICT DO NOTHING`                |
| `INTEGER PRIMARY KEY AUTOINCREMENT` (DDL) | `BIGSERIAL PRIMARY KEY`                   |
| `DEFAULT (datetime('now'))` (DDL) | ISO-text `to_char(...)` default                   |

The ISO-text approach for timestamps is the key trick: PG won't
implicitly cast `timestamptz` → `text`, so we never produce a
`timestamptz` for our existing `created_at TEXT` columns. Instead
both the column default AND the comparison expressions produce
ISO-8601 UTC strings. ISO-8601 lexicographic order matches
chronological order, so `created_at >= datetime('now', '-300 seconds')`
keeps working as a TEXT range scan.

Migration scope reduced from 7 per-table slices to one
translation layer + one schema translator. The 7 slices on the
plan below are now **polish opportunities** — surface a dialect
issue from real-world data, add a regex to the translator, repeat
— rather than per-table refactors.

CI: new `postgres-smoke` job runs the smoke-test module
(`tests/test_postgres_smoke.py`) against a `postgres:16-alpine`
service container. The job is required by the `cd-ghcr` publish
gate.

---

# Study note: Postgres backend chassis (P1.5 foundation)

**Studied 2026-05-12.**

## What we read

- [dagster-io/dagster](https://github.com/dagster-io/dagster) (HEAD) —
  `python_modules/dagster/dagster/_core/storage/` for the
  multi-backend storage abstraction pattern. Same problem shape:
  SQLite default, Postgres for scale, all backed by the same
  user-facing API.
- [getsentry/sentry](https://github.com/getsentry/sentry) (HEAD) —
  for "this works at scale" reference. We didn't borrow code,
  but their per-event-class storage shape informs the
  one-table-per-slice migration plan.
- [alembic](https://alembic.sqlalchemy.org/) docs — for the
  migration tool we'll eventually swap in when the inline
  `init_schema` migrations in `storage.py` become too much.

## Takeaways we took

1. **Don't refactor the 7,346-line `storage.py` in one PR.** The
   roadmap explicitly warns against this and the warning is right —
   180 methods across 41 tables can't all flip at once without
   a flag-day risk.
2. **Foundation slice = chassis only, no behavior change.** Add
   the `Backend` Protocol + the concrete classes + the URL
   factory. The existing `Storage` class is untouched apart from
   one ~12-line guard that catches `postgresql://` URLs and
   raises a clean `NotImplementedError` instead of a confusing
   `sqlite3` error.
3. **Per-table migration in follow-up PRs.** Each follow-up PR
   picks one table (or a small cluster: `failures` + their
   `evidence`, or `qa_incidents` + lifecycle events), refactors
   its `Storage` methods to call into `backend.connect()` + use
   `backend.placeholder()` + `backend.now_minus_seconds_sql(...)`,
   and adds Postgres-side implementations alongside the SQLite
   path. Each PR is small, individually testable, and never
   touches the other 39 tables.
4. **Two-way Protocol compliance via `runtime_checkable`.** We
   `isinstance(b, Backend)` in tests to verify both
   `SqliteBackend` and `PostgresBackend` (stub) satisfy the
   contract. Catches missing methods early.
5. **Dialect-aware SQL through helpers, not f-strings.**
   `backend.now_minus_seconds_sql(placeholder)` lets the same
   recency query work against SQLite's
   `datetime('now', '-N seconds')` and Postgres's
   `now() - N * interval '1 second'`. The first per-table slice
   will likely add more helpers (`json_extract`, `upsert_on_conflict`,
   `returning`).

## What we deliberately don't take

- **SQLAlchemy.** It would solve dialect translation for free
  but turns the entire storage layer into ORM mapping —
  doubles the line count and the migration cost. Raw SQL +
  small dialect helpers is the right tradeoff at our scale.
- **Connection pooling for SQLite.** Dagster has a pool-per-DB.
  SQLite is process-local; a fresh `sqlite3.connect()` per
  query (existing behavior) is fine. The Postgres backend
  WILL want a pool (psycopg's connection pool) — that's a
  per-slice concern, not a foundation concern.
- **Schema migrations via alembic right now.** The existing
  inline `init_schema` migrations work fine for SQLite and
  will need a rewrite when Postgres becomes real. We'll
  introduce alembic in the migration-tool slice — but adding
  alembic to a SQLite-only foundation is overkill.
- **Async backends.** Both `aiosqlite` and `psycopg` async
  exist. The current codebase is fully sync; converting to
  async is its own multi-month project. Not in scope for P1.5.

## Files in this PR that map back

- `src/retrace/storage_backend.py` ← original. The `Backend`
  Protocol shape is influenced by Dagster's
  `_core/storage/dagster_storage.py` interface; the dialect
  helpers are original.
- `src/retrace/storage.py` (single 12-line guard at `__init__`)
  ← so `postgresql://` URLs fail loudly with a roadmap pointer
  instead of mysteriously.
- `pyproject.toml` (`[postgres]` extra) ← `psycopg[binary]>=3.2`,
  unused at runtime today but reserved.
- `tests/test_storage_backend.py` — 27 tests pinning the URL
  parser, both backend implementations (including stub
  contract surface), and the Storage-level integration guard.

## Per-table migration plan (for the follow-up PRs)

Suggested ordering (smallest table-cluster first → most-coupled
last), each its own PR:

1. **`alert_routes` + `alert_dispatches`** (just shipped in
   P1.1, only a few methods, well-tested).
2. **`llm_pr_reviews`** (single table, small methods).
3. **`qa_incidents` cluster** (qa_incidents +
   lifecycle_events). Largest user-visible surface.
4. **`failures` + `evidence` + `repair_tasks`** (the monitoring
   pipeline cluster).
5. **`replay_*`** (sessions, replays, replay_issues, replay_files).
6. **`github_*` + `deploys`**.
7. **Remaining workspace / project / config tables**.

Each PR also adds a per-table CI run against a real Postgres
service container so regressions are caught early.
