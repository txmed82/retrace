# Changelog

All notable changes to Retrace are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and we adhere to [SemVer](https://semver.org/) post-v1.0 — see
[`docs/versioning.md`](docs/versioning.md) for the full
stability contract.

Versions in this changelog correspond to the CLI / server
artifact (`src/retrace/__init__.py` `__version__`). The Python
SDK (`packages/python-sdk/`) and the browser SDK
(`packages/browser/`) version independently and have their own
release notes once they reach `1.0`.

## [Unreleased]

### Added — P3 post-launch polish (2026-05-12)
- `retrace --version` CLI flag (P3.7).
- `docs/versioning.md` — stability contract for CLI, SDKs, ingest
  endpoints, `config.yaml` schema.
- `VERSION` export from `@retrace/browser` (P3.7).
- Six other P3 items tracked in `docs/roadmap.md`; see entries for
  flake quarantine (P3.1), tester parallelism (P3.2), end-to-end
  tests (P3.3), perf characterization (P3.4), LLM cost visibility
  (P3.5), server-side replay scaffold (P3.6).

### Added — P2 long-tail polish (2026-05-12)
- `retrace data retention apply [--dry-run]` purges old rows
  across the app-error domain (per project+env) + global
  `replay_batches` and `otel_events`, plus a filesystem sweep of
  `ui-tests/runs/` and `api-tests/runs/` (specs / baselines /
  queues NEVER touched). New `RetentionConfig` in
  `config.yaml`. P2.3 — PR #138.
- `retrace data backup --to PATH` — `.tar.gz` of a consistent
  sqlite snapshot (online BACKUP API) + `data_dir` contents.
  P2.3 — PR #138.
- OTel ingest endpoints (`/api/otel/v1/{logs,traces}`) now
  bucketed under the existing `_consume_rate_limit` machinery
  with `Retry-After` headers on 429. Browser SDK `sampleRate`
  contract pinned with regression tests. P2.2 — PR #139.

### Added — P1 scale & breadth (weeks of 2026-05-11/12)
- **P1.5 — Postgres backend** (PRs #135 + #136). Real
  `PostgresBackend.connect()` via psycopg3 plus SQL dialect
  translation at execute time (`?` → `%s`, `datetime('now', ?)`
  → ISO-text `to_char(now() + interval)`, `INSERT OR IGNORE` →
  `INSERT ... ON CONFLICT DO NOTHING`); portable `SCHEMA` via
  `sql_schema.translate_schema`. `Storage(postgresql://...)`
  works end-to-end; the 290+ existing `conn.execute(...)` call
  sites in storage.py are unchanged.
- **P1.4 — API tester polish** (PRs #134 + #137). `retrace tester
  api-diff --new --old` emits breaking-vs-safe contract changes
  across two OpenAPI documents; `retrace tester env list/show/yaml`
  manages env profiles (read-side; emits paste-ready stanzas);
  `retrace tester record --har` ingests browser DevTools HAR into
  one APITestSpec per matching request (sensitive headers
  stripped, JSON bodies structured).
- **P1.3 — Diff-aware affected-test selection** (PR #133).
  `pr_review.affected_api_specs(...)` intersects API spec URLs
  with the PR's affected flows; `retrace review
  --run-affected-tests --include-api`.
- **P1.2 — Perceptual visual diff** (PR #132). Optional `[image]`
  extra (Pillow + numpy); `visual_perceptual.perceptual_diff(...)`
  runs SSIM with an annotated red-overlay PNG. `retrace tester
  baseline compare --mode --threshold`.
- **P1.1 — Real-time alert fan-out** (PR #131).
  `alert_dispatch.dispatch_alert()` posts to Slack / Discord /
  PagerDuty / generic webhook with per-route severity floor +
  dedup window. `retrace monitor route add/list/delete/test` CLI.

### Added — P0 biggest credibility wins (2026-05-11)
- **P0.1 — LLM-powered PR review** (PRs #126 + #127).
  `retrace.llm_pr_review.llm_review(...)` produces summary +
  walkthrough + inline suggestions + risk notes;
  PII-redacted diff before LLM hits; 32k-token bail;
  file-aware chunking; in-process cache; `--llm` flag;
  prior-review memory via `llm_pr_reviews` table.
- **P0.2 — Python SDK** (PR #128). `retrace_sdk.init()`
  + FastAPI / Flask / Django / requests / logging integrations.
  Stdlib-only runtime; background-thread transport with bounded
  queue + `atexit` flush.
- **P0.3 — GitHub Actions templates** (PR #129). Three composite
  actions: `pr-review`, `source-map-upload` (curl + jq,
  dep-free), `qa-auto`.
- **P0.4 — Browser SDK breadcrumbs** (PR #130). 50-entry
  ring buffer; auto-capture for click / console / http /
  navigation / error; exception events carry the trail;
  server-side `monitoring_ingest` promotes the relevant entries
  into `IncidentEvidence` and persists the raw trail on
  `failure.metadata.breadcrumbs`.

### Notes
- The pre-history of the project (PRs #112–#124, plus everything
  earlier) is captured in `docs/roadmap.md`'s "Current state
  snapshot" and is not duplicated here.
- This is the first CHANGELOG entry. We are tracking releases
  going forward; the 0.1.0a1 line below represents "where we are
  at the time CHANGELOG was first written."

---

## [0.1.0a1] — 2026-05-12

Initial alpha tag. Everything listed under **[Unreleased]** above
is part of this snapshot — the alpha represents the post-P2-merge
state. Future releases will move entries from `[Unreleased]` into
dated version sections at cut time.
