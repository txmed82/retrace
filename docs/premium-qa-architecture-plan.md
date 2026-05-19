# Premium QA Architecture Plan

**Status:** Definitive direction document  
**Date:** 2026-05-18  
**Audience:** vibe coders, indie developers, and contributors building Retrace  

## Thesis

Retrace should become the free, open-source QA architecture for small teams that
ship with coding agents: real-user failure capture, deterministic reproduction,
agent-ready repair context, and verification in one local-first loop.

The premium bar is not "more AI." The premium bar is evidence quality:

```text
production signal -> incident -> replay/test evidence -> likely code -> repair task -> verified fix
```

The product should feel like Sentry, PostHog, Playwright, OpenReplay,
PR-Agent, Pullfrog, Langfuse, and Vercel-style evals were compressed into one
small-team workflow, with every expensive or hosted assumption removed.

## Current Position

Retrace already has more of the right architecture than a normal alpha:

- Unified `qa_incidents` across replay findings, UI tests, API tests,
  Sentry-compatible/OTel monitor events, and PR review findings.
- First-party browser replay capture plus PostHog replay import.
- Deterministic detectors for console/network/render/interaction failures.
- Native HTTP and Playwright-backed tester execution.
- API testing, OpenAPI import, HAR import, and API suite storage.
- Source-map upload, deploy correlation, alert rules, retention, digest, and
  issue sinks.
- GitHub/local repo linking, code matching, prompt generation, worktree-based
  repair, and draft PR creation.
- Browser SDK, Python SDK, Docker, CI, issue templates, security policy,
  contribution docs, and broad automated coverage.

The immediate credibility issue was a red default branch after modularization.
That is now fixed locally by restoring repository row mappers, tester facade
exports, deterministic evidence IDs for tests, deploy correlation updates, and
consensus/classification behavior.

## External References To Steal From

### Pullfrog: GitHub-Native Agent Orchestration

Pullfrog positions itself as an open-source orchestration layer for async
development inside GitHub. Its strongest ideas for Retrace are:

- GitHub as the control plane: trigger work from issues, PRs, comments, CI
  failures, and reviews.
- BYOK and model-agnostic operation.
- Automated triggers for CI failures and PR reviews.
- Self-healing PRs with loop prevention.
- GitHub Actions as user-owned compute, secrets, and cost boundary.

Reference: https://pullfrog.mintlify.app/

### Vercel Next Evals: Agent Quality As Versioned Fixtures

Vercel's Next evals package agent tasks as self-contained projects with a
`PROMPT.md`, source files, and withheld `EVAL.ts` assertions. The runner uses
memoization so new models or evals run only missing pairs.

Retrace should copy the shape, not the domain:

- Every generated reproduction should become a self-contained eval fixture.
- The prompt given to an agent and the assertions used to judge the result must
  be separate artifacts.
- Non-product failures such as timeouts/infra errors should be classified apart
  from real model or app failures.
- Results should be exportable as benchmark data.

Reference: https://github.com/vercel/next-evals-oss

### Vercel AI SDK: Provider Abstraction And Developer Ergonomics

Vercel AI SDK wins by being a clean TypeScript abstraction over many models and
agent workflows, with a large ecosystem and frequent releases.

Retrace should copy:

- A small, stable provider interface for local and hosted coding agents.
- Provider-specific adapters behind one API.
- Great examples for Next.js, React, Python, and plain script users.
- Versioned SDKs with boring upgrade paths.

Reference: https://github.com/vercel/ai

### Playwright: Deterministic Browser Runtime

Playwright is the execution substrate to trust: browser isolation, auto-waiting,
web-first assertions, resilient locators, traces, screenshots, videos, and
parallelism.

Retrace should not compete with Playwright. It should generate better Playwright
inputs from real incidents and preserve incident evidence beside Playwright
artifacts.

Reference: https://github.com/microsoft/playwright

### Cypress: App-Centric Testing UX

Cypress's durable insight is that browser testing adoption depends on fast local
feedback and approachable authoring, not just CI.

Retrace should copy:

- A local UI that makes tests, failures, screenshots, and logs inspectable.
- Low ceremony install and a "tested with Retrace" style success path.
- Component/API/e2e language in docs that users already understand.

Reference: https://github.com/cypress-io/cypress

### OpenReplay And PostHog: Session Replay Plus Product Context

OpenReplay's best ideas are self-hosted replay, network/console/state context,
privacy controls, and integrations that connect frontend behavior to backend
logs. PostHog's best ideas are an all-in-one developer product stack, a clear
free/open-source posture, feature flags/experiments, and replay as product
evidence rather than a separate QA silo.

Retrace should not become product analytics. It should ingest enough session and
product context to rank QA incidents by user impact.

References:

- https://github.com/openreplay/openreplay
- https://github.com/PostHog/posthog

### Sentry: Error Identity And SDK Coverage

Sentry's strength is event identity: grouping, releases, source maps,
breadcrumbs, affected users, SDK breadth, and developer workflow integration.

Retrace should keep Sentry compatibility while focusing on a narrower promise:
turn errors into repro tests and repair tasks.

Reference: https://github.com/getsentry/sentry

### PR-Agent: Review Tools And Token-Aware Context

PR-Agent's useful patterns are separate tools (`describe`, `review`, `improve`,
`ask`), GitHub Actions/CLI/self-hosted deployment, PR compression, dynamic
context, self-reflection, and platform-agnostic review flows.

Retrace should apply these patterns to QA evidence: compress incident evidence,
rank source context, cap suggestions, and keep review comments actionable.

Reference: https://github.com/The-PR-Agent/pr-agent

### Langfuse: Observability, Evals, Datasets

Langfuse connects traces, prompts, evals, datasets, user feedback, and manual
labels. Retrace should copy the "observability plus evals" mental model for QA:
every real failure can become a dataset item, and every fix attempt can be
evaluated against withheld assertions.

Reference: https://github.com/langfuse/langfuse

## Product Principles

1. **Evidence before agents.** LLMs summarize and repair only after deterministic
   capture, detection, and reproduction produce durable artifacts.
2. **Generated tests are the product.** A detected bug is not valuable until it
   becomes a repeatable test or a clearly classified non-repro.
3. **GitHub-native, local-first.** Small teams should run this from a repo,
   issue, PR, or GitHub Action without adopting a hosted control plane.
4. **BYOK and provider-neutral.** OpenAI, Anthropic, local models, OpenRouter,
   and future coding agents must sit behind stable adapters.
5. **Privacy is a default, not a setting.** Inputs masked by default, secrets
   redacted before storage/prompting, and prompt artifacts marked safe/unsafe.
6. **No magical certainty.** Code matching must explain why files are likely,
   preserve confidence, and let agents reject weak candidates.
7. **OSS quality is the paid-tier substitute.** The free product must be the
   premium product: reproducible setup, docs, CI, examples, and real fixtures.

## Target Architecture

```text
Capture
  browser SDK, Python SDK, Sentry DSN, OTLP, PostHog import, PR webhooks

Normalize
  canonical failures, evidence rows, replay sessions, deploys, source maps

Triage
  qa_incidents, severity, affected users, grouping, lifecycle, digest, sinks

Reproduce
  replay-to-test, API specs, Playwright/native runner, visual baselines,
  flake classification, artifact manifests

Repair
  repo matching, source-map/code context, repair bundle, agent prompt,
  worktree, draft PR, validation commands

Verify
  generated spec reruns, CI integration, resolved/regressed lifecycle,
  eval dataset export
```

The boundary that matters: `qa_incidents` is the product spine. Every capture
surface, test engine, review finding, and repair workflow must read/write this
shape.

## Roadmap

### Phase 0: Restore Default-Branch Trust

Goal: the project must be boringly green before claiming reliability.

- Keep `master` green across `ruff`, full `pytest`, browser SDK tests/build,
  Python SDK tests, Playwright runner tests, Postgres smoke, Docker build, and
  e2e tests.
- Add a CI badge and "known green command set" to the README.
- Make branch protection require the same jobs listed in `.github/workflows`.
- Add a short maintainer rule: no roadmap work merges while default branch is
  red.

Acceptance:

- `uv run ruff check src tests`
- `uv run pytest -q`
- `cd packages/browser && npm ci && npm test && npm run build`
- CI passes on `master`.

### Phase 1: Killer Demo As A Contract

Goal: `retrace demo all && retrace qa auto --repo local/demo-checkout --no-pr`
must prove the architecture without external services.

Build:

- One seeded fixture for each signal source: replay, UI test, API test, monitor
  event, PR review.
- A local demo app with a real bug, source map, route, and generated repair
  candidate.
- Withheld assertions modeled after Vercel evals: agent sees prompt/evidence;
  verifier sees expected behavior.
- Exportable `qa-eval-result.json` with pass/fail, artifacts, classification,
  and cost metadata.

Why:

Vercel-style evals turn agent quality into repeatable fixtures. Retrace needs
the same for QA repair quality.

### Phase 2: Incident Detail UX

Goal: an indie developer can understand an incident in under one minute.

Build:

- Incident page with replay, timeline, console/network/error evidence, deploy
  marker, source-map frame, generated test, candidate files, repair task, and
  verification history.
- Artifact viewer for screenshots, Playwright traces, request/response bodies,
  source-map diagnostics, and prompt JSON.
- "Why this file?" explanations for code matching.
- "Promote to issue" and "open repair PR" flows from the same page.

Reference:

- OpenReplay-style replay plus DevTools context.
- Sentry-style event identity and release/source-map context.

### Phase 3: Test Generation Quality

Goal: generated tests should survive CI, not just demos.

Build:

- Selector ranking: `data-testid`, role/name, label, placeholder, stable ID,
  text fallback, coordinate fallback.
- Playwright trace capture on first retry.
- Flake quarantine with failure classification: app bug, test bug, auth
  failure, selector drift, timeout, environment failure.
- Parallel `run-all` with deterministic artifacts.
- Visual baseline accept/compare flow documented for CI.
- API sequence generation from captured network calls.

Reference:

- Playwright locators, traces, browser isolation, auto-waiting.
- Cypress local testing UX and status visibility.

### Phase 4: GitHub-Native Agent Loop

Goal: Retrace behaves like a QA-native Pullfrog.

Build:

- GitHub App commands:
  - `@retrace triage`
  - `@retrace reproduce`
  - `@retrace fix`
  - `@retrace verify`
  - `@retrace explain`
- GitHub Actions workflow templates for:
  - replay/API/test incident ingestion
  - generated spec reruns
  - PR review filing
  - self-healing failed Retrace PRs with attempt caps
- Loop prevention: max attempts per incident/PR, stale branch detection,
  validation gate before new commits.
- BYOK secrets stored in GitHub Actions secrets for fully user-owned compute.

Reference:

- Pullfrog's GitHub-first trigger model, self-healing PRs, and Actions-backed
  execution.
- PR-Agent's tool split and PR compression.

### Phase 5: Agent Provider And Repair Adapter Layer

Goal: coding-agent integration is swappable and testable.

Build:

- Common `RepairAgent` interface:
  - `prepare(bundle) -> command/spec`
  - `apply(worktree, bundle) -> result`
  - `validate(worktree, commands) -> result`
  - `summarize(result) -> incident event`
- Adapters for local `codex`, `claude`, shell command, and no-op prompt-only
  mode.
- Provider cost and token accounting for LLM review/repair prompts.
- Prompt safety tests that prove user evidence cannot become instructions.
- Repair eval fixtures exported like Vercel Next evals.

Reference:

- Vercel AI SDK provider ergonomics.
- Langfuse observability/eval separation.

### Phase 6: Self-Host Operations

Goal: users can run this as infrastructure, not a laptop script.

Build:

- Production Docker Compose profile with API, UI, worker, browser runner, cron,
  SQLite/Postgres, and optional object storage.
- Postgres backend moved from compatibility chassis to supported mode.
- Storage sizing guide: replay volume, retention, source maps, artifacts.
- Backup/restore and upgrade tests.
- Retention defaults for raw replay, redacted evidence, source maps, and
  generated artifacts.
- Health checks and `/readyz` surfaces for each service.

Reference:

- OpenReplay self-hosting posture.
- PostHog's explicit open-source/self-host tradeoff language.

### Phase 7: Community And Extension System

Goal: contributors can add detectors, runners, sinks, and matchers without
reading the whole codebase.

Build:

- Detector plugin contract with fixture tests and reason-code docs.
- Test runner plugin contract for native HTTP, Playwright, API, visual, and
  future mobile.
- Sink contract for GitHub Issues, Linear, Jira, Slack, webhooks.
- Matching contract for route manifests, source maps, stack traces, selectors,
  framework component graphs.
- Example apps:
  - Next.js checkout bug
  - SaaS dashboard auth bug
  - API contract regression
  - source-mapped frontend exception

Acceptance:

- A contributor can add one detector with one fixture and one docs page in under
  an hour.

## Non-Goals

- Replacing Playwright or Cypress as general-purpose test frameworks.
- Replacing PostHog as product analytics.
- Replacing Sentry as a broad multi-language observability platform.
- Auto-merging fixes without tests and human review.
- Treating LLM judgment as proof of failure.
- Building a paid cloud tier before the open-source loop is excellent.

## Launch Bar

Retrace is ready to call itself premium open-source QA architecture when:

- Fresh checkout demo works in 10 minutes without external services.
- Default branch is green for 30 consecutive days.
- At least five real-world fixture apps are covered.
- Generated Playwright/API specs run in CI with stable artifact output.
- `qa auto` can take a replay or monitor incident to a draft PR with a
  validation command.
- A failed repair attempt is classified and preserved as an eval result.
- Docs explain the architecture, threat model, privacy model, and extension
  points.
- The browser SDK and Python SDK have versioned packages and upgrade notes.

## Immediate Next Moves

1. Merge the default-branch fix and confirm GitHub CI is green.
2. Add the killer-demo contract as a CI e2e job.
3. Convert `docs/roadmap.md` into execution milestones that map to this
   architecture.
4. Build the incident detail UX around the single `qa_incidents` spine.
5. Add GitHub App command triggers and loop prevention.
6. Export repair attempts as eval fixtures.

