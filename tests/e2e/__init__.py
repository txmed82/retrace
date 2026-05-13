"""P3.3 — end-to-end / dogfood tests.

The rest of the test suite is unit-shaped: pure functions, fake
stores, mocked HTTP. These tests exercise the full connective
tissue — real `retrace api serve` running on an ephemeral port,
real SDK keys, real ingest paths, real storage round-trips. They
fail when something between modules regresses even though every
unit test still passes.

Three scenarios match the roadmap:

  1. **Replay round-trip** — a synthetic rrweb batch lands at
     `/api/sdk/replay`, the server-side ingest persists a
     `replay_session`, and `list_replay_sessions` returns it.
  2. **Sentry-compat round-trip** — a synthetic envelope hits
     `/api/sentry/.../envelope`, a `qa_incident` opens.
  3. **PR-review on a fixture diff** — `llm_review` against a
     mocked LLM produces the expected shape.

Scenarios 1 + 2 hit the real HTTP server; scenario 3 is in-process
because the LLM client is mockable and the diff path is pure.

These tests are slower than the unit suite (server startup +
network RTT). They're not gated separately today — the full suite
runs them — but they're collected under `tests/e2e/` so a future
`pytest -m e2e` split (or the dedicated CI job from the roadmap)
can target them.
"""
