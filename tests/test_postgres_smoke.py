"""P1.5 — end-to-end smoke against a real Postgres.

These tests are GATED on the `RETRACE_POSTGRES_TEST_URL` env var.
When unset (the default for a stock `pytest`), the entire module is
skipped via `pytest.importorskip` + `skip_if_no_url`. When set, we
spin up a fresh schema in the target database and run a CRUD smoke
across the smallest table cluster — `alert_routes` + `alert_dispatches`
+ `llm_pr_reviews`.

CI wires the env var in `.github/workflows/ci-cd.yml` against the
`services: postgres` container.

Local: `RETRACE_POSTGRES_TEST_URL=postgresql://postgres:postgres@localhost:5432/test pytest tests/test_postgres_smoke.py`
"""

from __future__ import annotations

import os
import uuid

import pytest


_URL = os.environ.get("RETRACE_POSTGRES_TEST_URL", "")

# Module-level skip when the env var isn't set OR psycopg isn't
# installed — keeps a plain `pytest` from failing on dev machines
# without Postgres.
if not _URL:
    pytest.skip(
        "RETRACE_POSTGRES_TEST_URL not set; Postgres smoke tests skipped.",
        allow_module_level=True,
    )

psycopg = pytest.importorskip("psycopg")


from retrace.storage import Storage  # noqa: E402  (after the skip gate)


# ---------------------------------------------------------------------------
# Per-test schema isolation
# ---------------------------------------------------------------------------


def _isolated_url() -> str:
    """Return a Postgres URL pointing at a fresh schema named
    `retrace_test_<uuid>` so concurrent test runs don't collide.

    We can't easily create a fresh DATABASE without superuser, so we
    use a PG SCHEMA inside the target database and ALTER the connection's
    `search_path` to that schema. Each test's Storage gets a clean slate.

    Limitation: the existing `init_schema` writes UNQUALIFIED table
    names, so they land in whatever schema is first on `search_path`.
    We set `search_path = retrace_test_<uuid>` so they land in the
    isolated schema.
    """
    return _URL


@pytest.fixture
def isolated_storage(monkeypatch):
    """Create a fresh PG schema for the test, build Storage against it,
    drop the schema on teardown.

    We do schema creation + DROP outside Storage so the test's own
    init_schema covers the inside-the-schema CREATE TABLE work.
    """
    schema_name = f"retrace_test_{uuid.uuid4().hex[:12]}"
    raw = psycopg.connect(_URL, autocommit=True)
    with raw.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema_name}"')
        cur.execute(f'SET search_path TO "{schema_name}"')
    raw.close()

    # Storage opens its own connection; pass a search_path option via
    # the URL's `options` query param so every connection lands in
    # the isolated schema.
    sep = "&" if "?" in _URL else "?"
    url_with_schema = (
        f"{_URL}{sep}options=-csearch_path%3D{schema_name}"
    )

    store = Storage(url_with_schema)
    try:
        store.init_schema()
        yield store
    finally:
        raw = psycopg.connect(_URL, autocommit=True)
        with raw.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
        raw.close()


# ---------------------------------------------------------------------------
# Smokes
# ---------------------------------------------------------------------------


def test_init_schema_runs_against_postgres(isolated_storage):
    """If init_schema returned without raising, the translated SCHEMA
    was valid against Postgres. Confirm a few tables exist via raw
    catalog query."""
    store = isolated_storage
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        ).fetchall()
    tables = {r["table_name"] for r in rows}
    # Sanity: the freshly-shipped P1.1 + P0.1 follow-up tables are in.
    assert "alert_routes" in tables
    assert "alert_dispatches" in tables
    assert "llm_pr_reviews" in tables
    # Plus the older core tables.
    assert "qa_incidents" in tables
    assert "sessions" in tables


def test_alert_routes_round_trip_against_postgres(isolated_storage):
    """The P1.1 alert-routes Storage methods (`upsert_alert_route`,
    `list_alert_routes`, `get_alert_route`, `delete_alert_route`)
    work end-to-end against Postgres."""
    store = isolated_storage
    ws = store.ensure_workspace(project_name="PG smoke")

    row = store.upsert_alert_route(
        project_id=ws.project_id,
        environment_id=ws.environment_id,
        name="oncall",
        target_kind="slack",
        target_url="https://hooks.slack.com/x",
        min_severity="high",
    )
    assert row.target_kind == "slack"
    assert row.min_severity == "high"

    fetched = store.get_alert_route(
        project_id=ws.project_id,
        environment_id=ws.environment_id,
        name="oncall",
    )
    assert fetched is not None
    assert fetched.public_id == row.public_id

    listed = store.list_alert_routes(
        project_id=ws.project_id,
        environment_id=ws.environment_id,
    )
    assert [r.name for r in listed] == ["oncall"]

    removed = store.delete_alert_route(
        project_id=ws.project_id,
        environment_id=ws.environment_id,
        name="oncall",
    )
    assert removed is True


def test_alert_dispatch_dedup_window_against_postgres(isolated_storage):
    """The dedup query uses `datetime('now', ?)`; our translation
    layer rewrites it to PG `now() + interval` form. End-to-end
    proof here."""
    store = isolated_storage
    ws = store.ensure_workspace(project_name="PG smoke")
    route = store.upsert_alert_route(
        project_id=ws.project_id,
        environment_id=ws.environment_id,
        name="x",
        target_kind="webhook",
        target_url="https://x/",
        dedup_window_seconds=300,
    )
    store.record_alert_dispatch(
        route_id=route.id,
        project_id=ws.project_id,
        environment_id=ws.environment_id,
        fingerprint="fp-1",
        status="sent",
        target_kind="webhook",
        target_url="https://x/",
        payload={},
    )
    # Fresh window → finds the dispatch.
    found = store.recent_alert_dispatch_for(
        route_id=route.id, fingerprint="fp-1", within_seconds=600,
    )
    assert found is not None
    # Zero window → no match.
    assert store.recent_alert_dispatch_for(
        route_id=route.id, fingerprint="fp-1", within_seconds=0,
    ) is None
    # Wrong fingerprint → no match.
    assert store.recent_alert_dispatch_for(
        route_id=route.id, fingerprint="other", within_seconds=600,
    ) is None


def test_llm_pr_review_round_trip_against_postgres(isolated_storage):
    """`add_llm_pr_review` / `list_llm_pr_reviews_for_paths` work
    against PG, including the JSON-text storage of `paths_json`."""
    store = isolated_storage
    rid = store.add_llm_pr_review(
        repo="org/app",
        pr_number=42,
        model="test-model",
        summary="ok",
        risk_notes=["risk-a"],
        suggestions=[],
        paths=["server/auth.ts", "shared/util.ts"],
    )
    assert rid > 0
    rows = store.list_llm_pr_reviews_for_paths(
        ["server/auth.ts", "elsewhere.ts"],
        repo="org/app",
    )
    assert len(rows) == 1
    assert rows[0]["pr_number"] == 42


def test_backend_name_is_postgres(isolated_storage):
    assert isolated_storage.backend_name == "postgres"
