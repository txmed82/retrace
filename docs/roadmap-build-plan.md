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
- API exposes provider-neutral local metrics at `GET /api/metrics`.
- Docker Compose separates API, replay worker, and cron roles.

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
