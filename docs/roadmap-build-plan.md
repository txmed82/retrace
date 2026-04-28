# Retrace Roadmap Build Plan

This plan maps the next executable Linear issues to coherent build slices.

## Slice 1: Operational Test and Self-Host Foundation

- `RET-23` Add multi-model assertion consensus
- `RET-24` Add step caching and auto-healing
- `RET-34` Add cloud observability
- `RET-10` Package self-host deployment

Initial implementation:

- Native tester supports parseable model-vote consensus assertions and writes
  consensus artifacts.
- Native tester caches resolved step URLs, verifies cached steps with a real
  request, and records cache hit/miss/auto-heal events.
- API exposes provider-neutral local metrics at `GET /api/metrics`
  (service-token protected).
- Docker Compose separates API, replay worker, and cron roles.

Full implementation checklist:

1. `RET-23` model consensus assertions
   - Accept deterministic `model_votes` for offline runs and fixtures.
   - Run configured primary/secondary assertion models from native specs.
   - Attach response evidence with redacted headers and optional body excerpts.
   - Retry failed votes with fresh evidence when enabled.
   - Use an arbiter model on disagreement when configured.
   - Persist assertion results plus grouped consensus artifacts under each run.
2. `RET-24` step caching and auto-healing
   - Key cache entries by spec, app URL, action, URL, and path.
   - Store effective URLs after successful navigation steps.
   - Verify cached steps with observable status/text checks.
   - Auto-heal stale cache entries by retrying the fresh spec URL.
   - Bypass cache during retries and explicit override runs.
   - Persist cache hit, miss, bypass, and auto-heal events as run artifacts.
3. `RET-34` local/cloud observability
   - Track in-process API request counts, latency, failures, routes, and trace IDs.
   - Summarize replay sessions, batches, events, queue state, failed jobs, detector counts, issue status/severity, analysis status, and UI test run health.
   - Expose `GET /api/metrics` behind service-token scopes for self-host diagnostics.
   - Emit structured API request logs with trace IDs for external collectors.
4. `RET-10` self-host deployment
   - Ship separate Docker Compose roles for API, UI, replay worker, browser runner, and cron.
   - Mount config, env, reports, and data volumes consistently.
   - Keep local SQLite, replay blobs, UI specs, run artifacts, and cache data persistent.
   - Document setup, token creation, health checks, upgrades, backups, and storage sizing.
5. `RET-18` replay dashboard
   - Add local UI views for replay-backed issues and recent sessions.
   - Include status filters, shareable hashes, affected counts, signal summaries, repro steps, external ticket state, and linked sessions.
   - Render first-party rrweb events through the existing player when available.
   - Provide a UI action to process queued replay finalization jobs.

## Slice 2: Replay Product Workflow

- `RET-18` Add replay dashboard views
- `RET-27` Create stable bug and replay identifiers
- `RET-28` Add reproduction-from-replay generator
- `RET-31` Implement Linear and GitHub sinks

Build order:

1. Finish dashboard issue/session detail views around existing replay APIs.
2. Extend stable public IDs into all UI links, sink payloads, and support
   references.
3. Generate editable test specs from replay issue evidence.
4. Promote replay-backed issues into Linear/GitHub with dedupe and state sync.

Implementation notes:

- Replay sessions use stable `rpl_` public IDs and replay issues use stable `bug_`
  public IDs derived from project, environment, and source identity.
- Replay issue IDs can be resolved by internal ID or public ID for API/CLI workflows.
- Reproduction generation creates native tester specs from representative replay
  navigation, click, and input evidence. Ambiguous rrweb node actions are kept as
  editable steps with known gaps instead of being silently discarded.
- Linear/GitHub promotion produces a normalized sink payload, stores external IDs
  on the replay issue, and dedupes subsequent promotions.

## Slice 3: Native AI UI Testing Engine

- `RET-19` Replace shell harness with native runner
- `RET-20` Define durable test spec schema
- `RET-21` Implement snapshot-based agent tool loop

Build order:

1. Expand the current native runner from HTTP smoke execution to Playwright
   browser lifecycle ownership.
2. Version the durable spec schema around exact steps, exploratory goals,
   auth, fixtures, schedules, and browser settings.
3. Add the accessibility-snapshot tool loop with bounded actions and artifacts.

Implementation notes:

- Tester specs are schema version 2, with legacy version 1 specs migrated on load.
- Durable specs include exact steps, exploratory goals, assertions, auth,
  fixtures, data extraction, schedules, env overrides, and browser settings.
- The native runner keeps the lightweight HTTP runtime for self-host smoke checks
  and can switch to an optional Playwright runtime for browser actions when the
  `browser` extra is installed.

## Slice 4: Reliability and Security

- `RET-33` Build background processing pipeline
- `RET-36` Harden security and privacy
- `RET-41` Expand automated test coverage

Build order:

1. Generalize the replay finalize queue into a reusable worker/job substrate.
2. Add tenant isolation, redaction, key rotation, abuse limits, audit logs, and
   data deletion flows.
3. Broaden integration and end-to-end coverage across SDK, API, runner, sinks,
   and self-host paths.
