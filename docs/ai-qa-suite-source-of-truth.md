# Retrace AI QA Suite Source Of Truth

> **For agentic workers:** REQUIRED SUB-SKILL for implementation work: use `superpowers:writing-plans` to turn any roadmap issue below into a focused implementation plan, then use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to execute it. This document is the product and architecture source of truth, not a single sprint plan.

**Goal:** Build Retrace into a legitimate open-source AI QA suite for developers, centered on real failure evidence, generated tests, repair-ready context, and verification that fixes stay fixed.

**Architecture:** Every QA loop emits normalized failure, evidence, test, and repair artifacts into one shared intelligence layer. UI failure capture, AI UI testing, API testing, error monitoring, CI signals, repair agents, and PR review should communicate through common schemas instead of becoming separate tools with separate context silos.

**Tech Stack:** Python CLI/API/worker, SQLite-first self-host storage, `@retrace/browser` SDK, Browser Harness for agentic browser testing, optional Playwright/native runners, GitHub/Linear/Slack integrations, OpenAI-compatible LLM providers, eventual OpenTelemetry-compatible ingest.

---

## Product Positioning

Retrace should not be positioned as a generic test generator, generic Sentry clone, or generic code review bot.

The durable positioning is:

> Retrace is the open-source AI QA suite that turns real failures and risky changes into reproducible tests, repair-ready context, and verified fixes.

The product should serve developers who want practical QA automation without buying a managed QA service or trusting opaque SaaS agents.

## Core Product Promise

For any meaningful quality signal:

- a real user failure
- an AI UI test failure
- an API/backend regression
- an error-monitoring incident
- a CI failure
- a risky pull request

Retrace should answer:

1. What broke?
2. Who or what was affected?
3. What evidence proves this is real?
4. How do we reproduce it locally or in CI?
5. What code is likely involved?
6. What test should prevent it from coming back?
7. What repair context should a human or agent use?
8. Has the fix been verified?

## Strategic Principle

Build one excellent loop first, then add loops that feed the same shared repair layer.

Do not build five disconnected products. Every module must produce or consume the same core artifacts:

- `Failure`
- `Evidence`
- `Reproduction`
- `TestSpec`
- `TestRun`
- `CodeCandidate`
- `RepairTask`
- `ExternalThread`
- `Verification`

## Product Modules

### `retrace capture`

Owns first-party capture from browsers and later backends.

Includes:

- browser replay capture
- click/input/selector metadata
- console/network capture
- frontend exception capture
- trace context capture
- privacy and redaction
- SDK keys and service tokens

### `retrace identify`

Owns failure detection and grouping.

Includes:

- deterministic detectors
- AI-assisted summaries
- clustering
- duplicate detection
- severity and confidence
- incident creation
- status transitions

### `retrace test`

Owns UI/API test creation and execution.

Includes:

- replay-derived tests
- Browser Harness exploratory tests
- local API/backend tests
- CI runner output
- flake classification
- coverage links from failures to tests

### `retrace monitor`

Owns error monitoring and alert/incident behavior.

Includes:

- browser and backend error ingest
- Sentry/PostHog/webhook ingest
- OpenTelemetry-compatible correlation
- alert grouping
- deploy correlation
- daily/weekly digest
- Slack incident delivery

### `retrace repair`

Owns repair-ready context for humans and coding agents.

Includes:

- likely source files
- root-cause hypotheses
- regression test recommendations
- Codex/Claude prompts
- draft branch or PR generation
- validation commands
- repair status and verification

### `retrace review`

Owns PR-time QA review.

Includes:

- changed-flow detection
- test selection
- missing test recommendations
- production-bug regression risk
- OpenReview-style GitHub App behavior
- inline QA comments
- optional simple code suggestions

---

## Shared Failure Intelligence Layer

This is the most important long-term architectural requirement. All loops must communicate through this layer.

### Canonical Object: Failure

A `Failure` is any observed or predicted quality problem.

Sources:

- replay detector
- AI UI tester
- native UI/API test runner
- CI job
- Sentry/PostHog/Grafana/Datadog webhook
- OpenTelemetry signal
- GitHub PR review
- manual report

Fields to support:

- stable internal ID
- public ID
- project ID
- environment ID
- source type
- source external ID
- fingerprint
- title
- summary
- severity
- confidence
- status: `new`, `triaged`, `in_progress`, `resolved`, `regressed`, `ignored`
- affected users
- affected sessions
- first seen
- last seen
- related deploy SHA
- related PR number
- linked tests
- linked repair task
- linked external ticket or Slack thread

### Canonical Object: Evidence

Evidence is immutable context attached to a failure.

Evidence types:

- replay event window
- console log
- network request
- frontend exception
- backend exception
- stack trace
- trace ID
- span excerpt
- log excerpt
- metric anomaly
- screenshot
- DOM snapshot
- Browser Harness transcript
- API request/response
- CI log excerpt
- GitHub diff hunk
- user/session metadata

Required behavior:

- Evidence is append-only.
- Evidence is treated as untrusted data in prompts.
- Evidence can be redacted.
- Evidence can be referenced by tests and repair tasks.

### Canonical Object: Reproduction

A `Reproduction` is an executable or human-readable way to re-trigger a failure.

Types:

- UI replay-derived steps
- Browser Harness task
- Playwright/native exact steps
- API request sequence
- CLI command
- fixture setup
- manual reproduction notes

### Canonical Object: TestSpec

A `TestSpec` verifies behavior.

Types:

- replay-derived UI regression
- Browser Harness exploratory test
- deterministic UI smoke test
- visual UI test
- API contract test
- backend integration test
- CI command wrapper

Fields:

- spec ID
- source failure ID
- source PR number
- engine
- app URL or API base URL
- auth setup
- steps
- assertions
- fixtures
- expected artifacts
- flake policy
- schedule

### Canonical Object: RepairTask

A `RepairTask` packages everything a human or agent needs to fix a failure.

Fields:

- repair ID
- source failure IDs
- likely source files
- relevant symbols
- evidence bundle
- reproduction bundle
- recommended tests
- prompt artifacts
- agent target
- branch name
- PR URL
- validation commands
- status
- residual risk

### Canonical Object: ExternalThread

An `ExternalThread` represents a Slack thread, GitHub issue, Linear issue, or GitHub PR conversation.

Fields:

- provider
- external ID
- external URL
- linked failure IDs
- linked repair task ID
- last synced status
- outgoing messages
- inbound actions

### Canonical Object: Verification

A `Verification` proves whether a fix worked.

Fields:

- failure ID
- test spec ID
- test run ID
- commit SHA
- environment
- status: `passed`, `failed`, `flaky`, `blocked`
- artifacts
- verified at

---

## Long Timeline

### Stage 0: Shared Foundations

Purpose: make all future loops interoperable.

Target duration: 2-4 weeks.

Primary outcome: a stable internal schema and API for failures, evidence, tests, repair tasks, and verification.

### Stage 1: Excellent User-Failure UI Identification

Purpose: make the current Retrace wedge genuinely useful and trustworthy.

Target duration: 6-10 weeks.

Primary outcome: real user UI failures become high-quality issues with timelines, reproducible tests, likely code, and trusted Slack/GitHub output.

### Stage 2: Browser Harness AI UI Tester

Purpose: make Browser Harness the preferred agentic UI testing engine inside Retrace.

Target duration: 4-8 weeks.

Primary outcome: developers can ask Retrace to explore an app, generate useful UI tests, classify failures, and feed all results into the shared repair layer.

### Stage 3: Local Backend/API Testing

Purpose: add local API testing that connects naturally to UI failures and backend regressions.

Target duration: 4-8 weeks.

Primary outcome: failed frontend network calls, OpenAPI specs, and described backend behavior can produce local API regression tests and repair tasks.

### Stage 4: Open-Source Error Monitoring And Incident Grouping

Purpose: add the practical subset of Sentry/Superlog-style monitoring that feeds repair.

Target duration: 8-16 weeks.

Primary outcome: errors, logs, traces, deploys, and alerts group into incidents with repair-ready evidence.

### Stage 5: Repair Across All Failure Identifiers

Purpose: make repair artifacts consistent no matter where the failure came from.

Target duration: 8-12 weeks.

Primary outcome: every failure can produce likely files, reproduction, tests, prompts, and optionally a draft PR.

### Stage 6: QA-Focused PR Review

Purpose: add OpenReview-style PR review through a QA lens.

Target duration: 8-16 weeks.

Primary outcome: PRs get reviewed for product risk, missing tests, affected flows, linked production failures, and suggested test additions.

### Stage 7: General Code Review

Purpose: cover generic code review last, only after Retrace has differentiated QA context.

Target duration: 12+ weeks after Stage 6.

Primary outcome: code review uses Retrace's failure/test/monitoring context, rather than competing as another generic review bot.

---

# Stage 0: Shared Foundations

## RQA-0001: Define Canonical Failure Schema

**Why:** Current replay issues and report findings are close but too UI-specific. Future loops need a shared failure abstraction.

**Build:**

- Add canonical failure domain types.
- Map existing replay issues into canonical failures.
- Preserve existing public IDs.
- Support multiple sources.

**Likely files:**

- `src/retrace/failures.py`
- `src/retrace/storage.py`
- `tests/test_failures.py`
- `tests/test_storage.py`

**Acceptance criteria:**

- A replay issue can be represented as a canonical failure.
- A test failure can be represented as a canonical failure.
- A future monitor incident can be represented without schema changes.
- Existing replay issue APIs keep working.

## RQA-0002: Add Evidence Store

**Why:** Evidence should not be buried in issue JSON blobs. Repair and review need typed, queryable evidence.

**Build:**

- Add `failure_evidence` storage.
- Store evidence type, timestamp, source, redaction state, payload JSON, and artifact path.
- Add helper methods to append and list evidence.
- Backfill evidence from replay issue `evidence_json`.

**Likely files:**

- `src/retrace/storage.py`
- `src/retrace/evidence.py`
- `tests/test_evidence.py`

**Acceptance criteria:**

- Evidence can be attached to any failure.
- Evidence can be listed in chronological order.
- Evidence payloads are JSON-serializable.
- Evidence can be excluded from prompts when marked sensitive.

## RQA-0003: Add RepairTask Model

**Why:** Fix prompts are currently report-finding artifacts. Repair needs to become a first-class workflow.

**Build:**

- Add repair task storage.
- Link repair tasks to failures.
- Store status, likely files, prompt artifacts, validation commands, branch, PR URL, and risk notes.
- Migrate generated fix prompts into repair task concepts where possible.

**Likely files:**

- `src/retrace/repair.py`
- `src/retrace/storage.py`
- `src/retrace/fix_suggestions.py`
- `tests/test_repair.py`

**Acceptance criteria:**

- A replay issue can generate a repair task.
- A repair task can include multiple evidence items.
- Existing `retrace suggest-fixes` still writes prompt files.
- UI/API can later fetch repair task status.

## RQA-0004: Add Failure-Test Coverage Links

**Why:** The product needs to say “this issue is covered by test X.”

**Build:**

- Add `failure_test_links`.
- Link generated specs to failures.
- Track latest run status.
- Surface coverage state: `not_covered`, `covered_unverified`, `covered_passing`, `covered_failing`, `covered_flaky`.

**Likely files:**

- `src/retrace/storage.py`
- `src/retrace/tester.py`
- `src/retrace/replay_specs.py`
- `src/retrace/commands/api.py`
- `tests/test_replay_specs.py`
- `tests/test_tester_native.py`

**Acceptance criteria:**

- `from-replay-issue` creates a coverage link.
- Test runs update latest coverage state.
- Resolved issue verification can find linked specs through the coverage table.

## RQA-0005: Add Artifact Manifest Format

**Why:** Every loop needs portable artifacts for UI, CLI, Slack, GitHub, and repair agents.

**Build:**

- Define artifact manifest JSON.
- Include artifact ID, type, file path, MIME type, source failure, source run, label, and metadata.
- Have tester runs and repair prompts write manifests.

**Likely files:**

- `src/retrace/artifacts.py`
- `src/retrace/tester.py`
- `src/retrace/fix_suggestions.py`
- `tests/test_artifacts.py`

**Acceptance criteria:**

- Tester runs produce artifact manifests.
- Repair prompt generation produces artifact manifests.
- Artifacts can be rendered in the local UI without special-case parsing.

---

# Stage 1: Excellent User-Failure UI Identification

## RQA-0101: Failure Timeline View

**Why:** A developer should understand a replay-backed issue in under one minute.

**Build:**

- Build an issue timeline from evidence items.
- Include replay events, click/input metadata, console logs, network calls, detector hits, exceptions, traces, and screenshots when available.
- Add filtering by event type.
- Add “copy evidence bundle” action.

**Likely files:**

- `src/retrace/commands/ui.py`
- `src/retrace/replay_core.py`
- `src/retrace/evidence.py`
- `tests/test_ui_replay_specs.py`
- `tests/test_replay_core.py`

**Acceptance criteria:**

- Issue detail view shows chronological events.
- Detector hits are visually distinct from raw events.
- Network 4xx/5xx entries show method, URL, status, and timing.
- Console errors show level and message.
- Timeline can be generated from existing demo seed data.

## RQA-0102: Durable Selector Inference

**Why:** Replay-derived tests are only useful if selectors survive normal UI changes.

**Build:**

- Rank selector candidates from SDK target metadata.
- Prefer `data-testid`, `data-test`, `data-qa`.
- Then prefer role/name/label.
- Then stable ID.
- Then constrained text.
- Avoid brittle class names unless no better option exists.
- Store selector rationale in generated specs.

**Likely files:**

- `packages/browser/src/index.ts`
- `src/retrace/replay_specs.py`
- `tests/test_replay_core.py`
- `tests/test_ui_replay_specs.py`

**Acceptance criteria:**

- Generated specs include selector candidates and rationale.
- Input values remain masked unless explicitly configured.
- Tests prefer durable selectors over rrweb node IDs.
- Demo replay produces readable steps.

## RQA-0103: Improve Replay-To-Test Generation

**Why:** `from-replay` should create tests that are useful without heavy editing.

**Build:**

- Convert replay action windows into exact steps.
- Generate assertions from the failure signal.
- Include precondition and fixture notes.
- Include unsupported-step warnings.
- Generate both human-readable and executable forms.

**Likely files:**

- `src/retrace/replay_specs.py`
- `src/retrace/tester.py`
- `src/retrace/commands/tester.py`
- `tests/test_replay_core.py`
- `tests/test_cli_demo.py`

**Acceptance criteria:**

- Network failure issues produce assertions on request status or absence of error UI.
- Blank render issues produce content/visibility assertions.
- Error toast issues produce absence-of-toast assertions after reproduction.
- Generated specs link back to failure and replay.

## RQA-0104: Detector Confidence And Reason Codes

**Why:** Developers need to know why Retrace thinks something is a bug.

**Build:**

- Extend detector `Signal` details with confidence and reason codes.
- Add detector-specific reason code docs.
- Display confidence and reasons in issue timeline.

**Likely files:**

- `src/retrace/detectors/base.py`
- `src/retrace/detectors/*.py`
- `src/retrace/replay_core.py`
- `tests/test_detectors/*.py`

**Acceptance criteria:**

- Every detector emits at least one reason code.
- Confidence is one of `low`, `medium`, `high`.
- Issue summaries include reason codes.

## RQA-0105: False Positive Tuning

**Why:** User-failure identification must be trusted.

**Build:**

- Add fixtures for known false positives.
- Tune rage click, dead click, blank render, and abandonment detectors.
- Add suppression rules.
- Track ignored fingerprints.

**Likely files:**

- `tests/fixtures/events.py`
- `tests/test_detectors/*.py`
- `src/retrace/detectors/*.py`
- `src/retrace/storage.py`

**Acceptance criteria:**

- Common benign clicks do not create dead-click issues.
- Loading states do not create blank-render issues unless they exceed threshold.
- Repeated clicks on disabled-but-explained controls are lower confidence.
- Ignored fingerprints do not notify or generate repair tasks.

## RQA-0106: Flake Classification For UI Runs

**Why:** Failed tests need triage, not panic.

**Build:**

- Classify failures as app bug, test bug, environment failure, auth failure, timeout, selector drift, or unknown.
- Store classification on test runs.
- Feed classification into failure creation and repair tasks.

**Likely files:**

- `src/retrace/tester.py`
- `src/retrace/commands/tester.py`
- `src/retrace/storage.py`
- `tests/test_tester_native.py`
- `tests/test_tester_playwright.py`

**Acceptance criteria:**

- Retry-pass runs are marked flaky.
- Selector missing is separate from app assertion failure.
- App server unavailable is separate from app bug.
- Classification is visible in CLI and UI.

## RQA-0107: Trusted Slack And GitHub Output

**Why:** The product succeeds only if engineers trust the posted issue.

**Build:**

- Create a compact issue card format.
- Include severity, confidence, affected count, replay link, timeline summary, generated test status, likely files, and actions.
- For GitHub/Linear, include stable dedupe markers.
- For Slack, post grouped updates instead of repeated noise.

**Likely files:**

- `src/retrace/notification_sinks.py`
- `src/retrace/issue_sinks.py`
- `src/retrace/commands/api.py`
- `tests/test_notification_sinks.py`
- `tests/test_issue_sinks.py`

**Acceptance criteria:**

- New issues notify once.
- Regressions notify again with previous resolved timestamp.
- Slack text is readable without opening Retrace.
- GitHub issue body contains replay, timeline, test, and repair links.

## RQA-0108: UI Redesign For Issue Workflow

**Why:** The current local UI is functional but not a serious product surface.

**Build:**

- Replace single-page utility layout with workflow-focused navigation.
- Views: Dashboard, Issues, Issue Detail, Replays, Tests, Runs, Settings.
- Keep it lightweight and local-first.
- Make issue detail the primary surface.

**Likely files:**

- `src/retrace/commands/ui.py`
- eventual split files under `src/retrace/ui_static/`
- `tests/test_ui_replay_specs.py`
- `tests/test_ui_github_repos.py`

**Acceptance criteria:**

- Issue detail shows replay, timeline, generated test, repair task, and external links.
- Test panel shows linked failures.
- Settings remain usable.
- Existing UI tests pass or are updated.

---

# Stage 2: Browser Harness AI UI Tester

## RQA-0201: First-Class Browser Harness Adapter

**Why:** Browser Harness should be integrated as a structured engine, not only invoked through a string command.

**Build:**

- Add adapter interface for Browser Harness.
- Capture structured run output.
- Normalize steps, screenshots, console output, network output, and final status.
- Preserve raw harness logs as artifacts.

**Likely files:**

- `src/retrace/browser_harness.py`
- `src/retrace/tester.py`
- `src/retrace/commands/tester.py`
- `tests/test_browser_harness.py`

**Acceptance criteria:**

- Harness runs produce structured `TesterRunResult`.
- Harness artifacts appear in run manifest.
- Harness failures can create canonical failures.

## RQA-0202: AI Explore Mode With Test Proposal

**Why:** Exploration should propose tests, not only run a one-off task.

**Build:**

- Add suite exploration output schema.
- Convert discovered flows into draft specs.
- Rank flows by criticality.
- Let users accept/edit generated specs.

**Likely files:**

- `src/retrace/tester.py`
- `src/retrace/commands/tester.py`
- `src/retrace/commands/ui.py`
- `tests/test_cli_tester.py`

**Acceptance criteria:**

- `retrace tester create-suite` can create multiple draft specs.
- Draft specs include reason and source exploration run.
- User can run accepted specs.

## RQA-0203: Browser Harness Failure Identification

**Why:** AI UI tester results should feed the same failure layer as real user failures.

**Build:**

- Convert harness failed goals/assertions into canonical failures.
- Attach transcript, screenshots, network logs, console logs, and final DOM/snapshot evidence.
- Link generated repair tasks.

**Likely files:**

- `src/retrace/failures.py`
- `src/retrace/browser_harness.py`
- `src/retrace/tester.py`
- `tests/test_browser_harness.py`

**Acceptance criteria:**

- A failed Harness run creates or updates a failure.
- Duplicate harness failures dedupe by fingerprint.
- Repair can consume harness evidence.

## RQA-0204: Auth And Environment Profiles

**Why:** Useful UI testing requires reliable login/setup.

**Build:**

- Define reusable auth profiles.
- Support form login, JWT, headers, and custom script steps.
- Store secrets only in env vars.
- Share auth profiles across replay-derived, Harness, native, and visual specs.

**Likely files:**

- `src/retrace/tester.py`
- `src/retrace/config.py`
- `src/retrace/script_steps.py`
- `src/retrace/commands/ui.py`
- `tests/test_tester_native.py`

**Acceptance criteria:**

- A spec can reference an auth profile.
- Secret values are not written into spec files.
- Failed auth is classified distinctly.

## RQA-0205: Harness Versus Native Engine Policy

**Why:** Retrace needs clear engine selection rules.

**Build:**

- Define when to use Harness, native HTTP, Playwright, explore, or visual.
- Add `execution_engine = auto` policy.
- Explain engine choice in run output.

**Likely files:**

- `src/retrace/tester.py`
- `src/retrace/commands/tester.py`
- `README.md`

**Acceptance criteria:**

- Exact deterministic steps prefer native/Playwright.
- Exploratory prompts prefer Browser Harness.
- API-only specs use API runner.
- CLI explains why an engine was selected.

---

# Stage 3: Local Backend/API Testing

## RQA-0301: API Test Spec Schema

**Why:** API testing needs first-class specs, not ad hoc HTTP smoke checks.

**Build:**

- Add API spec type.
- Support method, URL, query, headers, body, auth, expected status, JSON body assertions, schema assertions, latency threshold.
- Support setup and teardown script steps.

**Likely files:**

- `src/retrace/api_testing.py`
- `src/retrace/tester.py`
- `tests/test_api_testing.py`

**Acceptance criteria:**

- API specs can be saved and listed.
- API specs can run locally.
- Results include request/response artifacts with redaction.

## RQA-0302: Generate API Tests From Failed Network Calls

**Why:** This is the natural bridge from UI failures to backend/API testing.

**Build:**

- Extract failed fetch/XHR evidence.
- Generate API regression specs.
- Include headers/auth placeholders safely.
- Link API spec to source failure.

**Likely files:**

- `src/retrace/replay_specs.py`
- `src/retrace/api_testing.py`
- `src/retrace/evidence.py`
- `tests/test_replay_specs.py`

**Acceptance criteria:**

- A replay issue with a failed API request can create an API spec.
- Sensitive headers are excluded or env-var referenced.
- The API spec is linked as coverage for the failure.

## RQA-0303: OpenAPI Import

**Why:** Teams with existing API contracts should get value quickly.

**Build:**

- Import OpenAPI JSON/YAML.
- Generate baseline smoke specs for selected paths.
- Generate schema assertions from responses.
- Mark imported specs as contract-derived.

**Likely files:**

- `src/retrace/openapi_import.py`
- `src/retrace/commands/tester.py`
- `tests/test_openapi_import.py`

**Acceptance criteria:**

- OpenAPI import creates runnable specs.
- User can filter by path/method.
- Generated specs use configured base URL.

## RQA-0304: API Failure To Repair Task

**Why:** API failures should feed repair exactly like UI failures.

**Build:**

- Convert failed API test runs into canonical failures.
- Attach request/response evidence.
- Score source files using route matching.
- Generate repair prompts with API reproduction.

**Likely files:**

- `src/retrace/api_testing.py`
- `src/retrace/fix_suggestions.py`
- `src/retrace/matching/scorer.py`
- `tests/test_api_testing.py`
- `tests/test_matching_scorer.py`

**Acceptance criteria:**

- Failed API test creates a failure.
- Route path boosts likely source files.
- Repair prompt includes exact API reproduction.

---

# Stage 4: Open-Source Error Monitoring And Incident Grouping

## RQA-0401: Browser Error Capture

**Why:** Retrace should catch frontend exceptions without relying only on console logs.

**Build:**

- Capture `window.onerror`.
- Capture `unhandledrejection`.
- Include stack, message, URL, line/column, trace context, session ID.
- Redact configured patterns.

**Likely files:**

- `packages/browser/src/index.ts`
- `src/retrace/detectors/console_error.py`
- `tests/test_detectors/test_console_error.py`

**Acceptance criteria:**

- Browser exceptions become replay evidence.
- Stack traces are available for matching.
- Sensitive values are redacted.

## RQA-0402: Trace Context Capture

**Why:** UI failures and backend traces must connect.

**Build:**

- Read or create trace context for outgoing requests.
- Capture `traceparent` response/request metadata when available.
- Store trace IDs on evidence and failures.

**Likely files:**

- `packages/browser/src/index.ts`
- `src/retrace/enrichment.py`
- `src/retrace/storage.py`
- `tests/test_enrichment.py`

**Acceptance criteria:**

- Failed network evidence includes trace ID when available.
- Failure details display trace IDs.
- Repair prompt includes trace context.

## RQA-0403: External Error Webhook Ingest

**Why:** Users may already have Sentry/PostHog/Grafana/Datadog alerts.

**Build:**

- Add generic webhook endpoint.
- Add provider adapters for Sentry and PostHog first.
- Normalize external alerts into failures.
- Store external IDs for dedupe.

**Likely files:**

- `src/retrace/commands/api.py`
- `src/retrace/monitoring_ingest.py`
- `src/retrace/storage.py`
- `tests/test_monitoring_ingest.py`

**Acceptance criteria:**

- Sentry-style webhook creates or updates a failure.
- PostHog exception webhook creates or updates a failure.
- Duplicate external alerts update the same failure.

## RQA-0404: Incident Grouping

**Why:** Superlog-style value comes from grouping noise into one actionable incident.

**Build:**

- Group failures by stack frame, route, service, trace, deploy, and fingerprint.
- Add incident object.
- Link failures to incidents.
- Add severity rollup.

**Likely files:**

- `src/retrace/incidents.py`
- `src/retrace/storage.py`
- `tests/test_incidents.py`

**Acceptance criteria:**

- Multiple equivalent alerts group into one incident.
- Incident shows affected failures and evidence.
- Incident can generate one repair task.

## RQA-0405: Deploy Correlation

**Why:** Recent deploy context is essential for incident repair.

**Build:**

- Store deploy markers with SHA, branch, environment, author, timestamp.
- Ingest from GitHub Actions/webhook or CLI.
- Link failures/incidents to nearest deploy.
- Include changed files in repair context.

**Likely files:**

- `src/retrace/deploys.py`
- `src/retrace/commands/api.py`
- `src/retrace/storage.py`
- `tests/test_deploys.py`

**Acceptance criteria:**

- A deploy marker can be recorded.
- Failures after deploy link to that deploy.
- Repair prompt includes recent changed files.

## RQA-0406: OpenTelemetry-Compatible Ingest

**Why:** Long-term monitoring needs standards-based ingest.

**Build:**

- Start with OTLP JSON or simplified collector-compatible endpoint.
- Accept logs and traces.
- Store only compact excerpts locally by default.
- Link spans/logs to failures by trace ID.

**Likely files:**

- `src/retrace/otel_ingest.py`
- `src/retrace/commands/api.py`
- `src/retrace/storage.py`
- `tests/test_otel_ingest.py`

**Acceptance criteria:**

- OTLP-like log payload can be ingested.
- OTLP-like trace payload can be ingested.
- Evidence can link to trace/span IDs.

---

# Stage 5: Repair Across All Failure Identifiers

## RQA-0501: Unified Repair Bundle

**Why:** Repair should work the same for replay, test, API, incident, and CI failures.

**Build:**

- Create repair bundle builder.
- Include failure summary, evidence, reproduction, linked tests, likely files, deploy context, external thread context, and validation commands.
- Keep prompt-injection defenses.

**Likely files:**

- `src/retrace/repair.py`
- `src/retrace/prompts/fix_prompt.py`
- `tests/test_repair.py`
- `tests/test_fix_prompt.py`

**Acceptance criteria:**

- A repair bundle can be built for replay issues.
- A repair bundle can be built for API failures.
- Evidence is quoted as untrusted data.

## RQA-0502: Better Code Matching

**Why:** Repair quality depends on finding the right code.

**Build:**

- Add framework route manifest parsing.
- Add sourcemap stack mapping.
- Add CODEOWNERS and recent git blame/churn signals.
- Improve symbol extraction.

**Likely files:**

- `src/retrace/matching/scorer.py`
- `src/retrace/matching/routes.py`
- `src/retrace/matching/sourcemaps.py`
- `tests/test_matching_scorer.py`

**Acceptance criteria:**

- Stack traces strongly rank mapped source files.
- API routes rank route handlers.
- CODEOWNERS can be included in repair context.

## RQA-0503: Draft PR Repair Runner

**Why:** Eventually the product should create a branch or draft PR, not only a prompt.

**Build:**

- Add repair runner interface.
- Support local sandbox execution.
- Run an agent with repair bundle.
- Capture diff, changed files, tests run, and result.
- Optionally create draft PR.

**Likely files:**

- `src/retrace/repair_runner.py`
- `src/retrace/commands/repair.py`
- `src/retrace/issue_sink_clients.py`
- `tests/test_repair_runner.py`

**Acceptance criteria:**

- Dry-run repair produces an agent prompt and planned commands.
- Local repair can run in a checkout and capture diff.
- Draft PR creation is gated behind explicit config/CLI flag.

## RQA-0504: Validation Command Inference

**Why:** Agents need to verify fixes with the right tests.

**Build:**

- Infer commands from repo files.
- Prefer linked Retrace specs.
- Include package manager test commands.
- Include targeted API/UI specs.

**Likely files:**

- `src/retrace/repair.py`
- `src/retrace/repo_inspection.py`
- `tests/test_repair.py`

**Acceptance criteria:**

- Repair task includes relevant validation commands.
- Commands are explainable.
- Commands avoid destructive behavior.

---

# Stage 6: QA-Focused PR Review

## RQA-0601: GitHub App Foundation

**Why:** PR review needs webhook-based integration, not only CLI commands.

**Build:**

- Add GitHub App configuration.
- Handle PR comment webhook.
- Trigger review on mention.
- Store review runs.

**Likely files:**

- `src/retrace/github_app.py`
- `src/retrace/commands/api.py`
- `src/retrace/storage.py`
- `tests/test_github_app.py`

**Acceptance criteria:**

- `@retrace review` comment starts a review run.
- Invalid signatures are rejected.
- Review run status is stored.

## RQA-0602: PR Diff To QA Risk Analysis

**Why:** Retrace review should be differentiated from generic code review.

**Build:**

- Parse changed files and diff hunks.
- Link changed files to prior failures.
- Identify affected routes/components/API endpoints.
- Recommend which existing tests to run.
- Recommend missing tests.

**Likely files:**

- `src/retrace/pr_review.py`
- `src/retrace/matching/scorer.py`
- `src/retrace/storage.py`
- `tests/test_pr_review.py`

**Acceptance criteria:**

- Review output lists affected flows.
- Review output links prior failures touching changed files.
- Review output recommends existing Retrace specs.
- Review output suggests new UI/API tests where coverage is missing.

## RQA-0603: Inline QA Comments

**Why:** Developers need review feedback in the PR, not only in Retrace UI.

**Build:**

- Post summary comment.
- Post inline comments for missing tests or risky changes.
- Include commands to generate or run Retrace specs.
- Support reactions to dismiss or accept suggestions later.

**Likely files:**

- `src/retrace/issue_sink_clients.py`
- `src/retrace/pr_review.py`
- `tests/test_pr_review.py`

**Acceptance criteria:**

- PR summary comment is posted.
- Inline comments reference exact diff lines where possible.
- Comments are idempotent across reruns.

## RQA-0604: PR Review Sandbox

**Why:** OpenReview-style value comes from running tools in an isolated checkout.

**Build:**

- Clone PR branch into isolated workspace.
- Install dependencies optionally.
- Run selected tests.
- Capture logs and artifacts.
- Clean up workspace.

**Likely files:**

- `src/retrace/sandbox.py`
- `src/retrace/pr_review.py`
- `tests/test_sandbox.py`

**Acceptance criteria:**

- Sandbox can run a configured command.
- Logs are captured as artifacts.
- Failure output feeds canonical failures when relevant.

---

# Stage 7: General Code Review

## RQA-0701: Review Skill System

**Why:** Generic review should be extensible without bloating prompts.

**Build:**

- Add review skills directory.
- Load only matching skills.
- Provide built-in QA, security-lite, API, frontend accessibility, and test-quality skills.

**Likely files:**

- `src/retrace/review_skills.py`
- `src/retrace/pr_review.py`
- `tests/test_review_skills.py`

**Acceptance criteria:**

- Skills have metadata and instructions.
- Review loads only relevant skills.
- Users can add local skills.

## RQA-0702: Simple Fix Suggestions

**Why:** Review can eventually suggest simple code changes.

**Build:**

- Generate GitHub suggestion blocks.
- Gate auto-commit behind explicit approval.
- Restrict to small, low-risk changes initially.

**Likely files:**

- `src/retrace/pr_review.py`
- `src/retrace/issue_sink_clients.py`
- `tests/test_pr_review.py`

**Acceptance criteria:**

- Review can post suggestion blocks.
- Suggestions are tied to line comments.
- Auto-push is disabled by default.

---

# Cross-Cutting Requirements

## OSS Quality

Every feature should preserve:

- local-first operation
- BYOK LLM providers
- self-host Docker Compose path
- documented schemas
- fixture-driven tests
- privacy defaults
- no secrets in artifacts
- readable generated files

## UI Quality

The UI should become workflow-first:

- dashboard for current risk
- issue list
- issue detail
- replay viewer
- timeline
- tests
- runs
- repair tasks
- integrations
- settings

The UI should not be a marketing surface. It should be a dense, useful developer tool.

## Privacy And Security

Required before broader public adoption:

- default input masking
- configurable redaction
- evidence retention settings
- project/environment isolation
- SDK key rotation
- service token scopes
- audit log for external actions
- explicit approval for PR creation and code pushes

## Integrations Priority

Order:

1. GitHub issues/PRs
2. Slack
3. Linear
4. Sentry webhook
5. PostHog events/exceptions
6. GitHub Actions
7. OpenTelemetry-compatible ingest
8. Datadog/Grafana webhooks

## CLI Shape

Long-term CLI should converge toward:

```bash
retrace capture ...
retrace identify ...
retrace test ...
retrace monitor ...
retrace repair ...
retrace review ...
```

Existing commands should remain as aliases until migration is safe.

## Data Flow

```text
capture sources
  -> evidence
  -> failures
  -> reproductions
  -> tests
  -> repair tasks
  -> external threads
  -> verification
  -> resolved/regressed state
```

All modules should either add evidence, create failures, generate tests, create repair tasks, or verify fixes.

## Definition Of A Great Retrace Issue

A great issue contains:

- clear title
- severity and confidence
- affected users/sessions
- first and last seen
- failure timeline
- replay or reproduction
- generated regression test
- linked latest test run
- likely source files with rationale
- logs/traces/errors
- repair task
- external Slack/GitHub/Linear thread
- resolved/regressed history

## Definition Of A Great Repair Task

A great repair task contains:

- one clear failure or incident
- compact evidence bundle
- reproduction steps
- exact test to run or create
- likely files ranked with rationale
- recent deploy or PR context
- prompt for coding agents
- validation commands
- expected output
- residual risk field

---

# Near-Term Recommended Build Order

## Milestone A: Product Spine

1. RQA-0001 Canonical Failure Schema
2. RQA-0002 Evidence Store
3. RQA-0004 Failure-Test Coverage Links
4. RQA-0101 Failure Timeline View

## Milestone B: Excellent User-Failure Tests

1. RQA-0102 Durable Selector Inference
2. RQA-0103 Improve Replay-To-Test Generation
3. RQA-0104 Detector Confidence And Reason Codes
4. RQA-0105 False Positive Tuning
5. RQA-0106 Flake Classification

## Milestone C: Useful Developer Output

1. RQA-0003 RepairTask Model
2. RQA-0107 Trusted Slack And GitHub Output
3. RQA-0501 Unified Repair Bundle
4. RQA-0502 Better Code Matching

## Milestone D: Browser Harness Productization

1. RQA-0201 First-Class Browser Harness Adapter
2. RQA-0202 AI Explore Mode With Test Proposal
3. RQA-0203 Browser Harness Failure Identification
4. RQA-0204 Auth And Environment Profiles

## Milestone E: API Testing

1. RQA-0301 API Test Spec Schema
2. RQA-0302 Generate API Tests From Failed Network Calls
3. RQA-0304 API Failure To Repair Task
4. RQA-0303 OpenAPI Import

## Milestone F: Monitoring

1. RQA-0401 Browser Error Capture
2. RQA-0402 Trace Context Capture
3. RQA-0403 External Error Webhook Ingest
4. RQA-0404 Incident Grouping
5. RQA-0405 Deploy Correlation
6. RQA-0406 OpenTelemetry-Compatible Ingest

## Milestone G: Agentic Repair And Review

1. RQA-0503 Draft PR Repair Runner
2. RQA-0504 Validation Command Inference
3. RQA-0601 GitHub App Foundation
4. RQA-0602 PR Diff To QA Risk Analysis
5. RQA-0603 Inline QA Comments
6. RQA-0604 PR Review Sandbox

---

# What Not To Build Yet

Do not prioritize these until the core loop is excellent:

- generic code review
- full Datadog replacement
- multi-tenant hosted billing
- mobile-native testing
- broad enterprise admin controls
- auto-merge repairs
- large-scale metrics storage

These may matter later, but they will dilute the product before the evidence-to-test-to-repair loop is trusted.

---

# Success Milestones

## Open-Source Credibility

Retrace is credible open source when a developer can:

1. Install locally.
2. Add the browser SDK.
3. Trigger a real UI failure.
4. See a high-quality issue timeline.
5. Generate a regression test.
6. Run it locally and in CI.
7. Get likely code locations.
8. Generate a repair task.
9. Fix the issue.
10. Verify it stays fixed.

## AI QA Suite Credibility

Retrace is credible as an AI QA suite when:

1. User failures, AI UI tests, API tests, monitoring incidents, CI failures, and PR review all produce the same failure/repair artifacts.
2. Each loop improves the other loops.
3. Repair tasks can consume evidence from any source.
4. Tests can be generated from any failure.
5. PR review can use production failure and test history.
6. Incidents can be repaired and verified through the same workflow.

## Commercial Optionality

The open-source product should remain valuable without a hosted service.

Possible future commercial layers:

- hosted storage and workers
- managed browser execution
- team dashboards
- long-term retention
- advanced integrations
- managed QA service
- enterprise auth/audit
- repair sandbox hosting

These should not be required for the core open-source loop.

