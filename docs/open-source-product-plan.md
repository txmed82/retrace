# Retrace Open-Source Product Plan

Retrace is intended to be a full open-source UI reliability product: live user
UI failure capture, replay-backed issue triage, automated UI regression testing,
and coding-agent repair prompts in one BYOK, self-hostable loop.

## Product Definition

Retrace should answer four questions for a team running a web product:

- What UI failures are real users hitting right now?
- Which failures are repeated enough to deserve engineering attention?
- What deterministic UI tests reproduce those failures?
- Where in the codebase should an engineer or coding agent start fixing them?

The product is not just a replay viewer and not just a test runner. Its value is
the closed loop from live evidence to automated verification.

## Current Working Surface

Retrace already has the core pieces of that loop:

- PostHog session recording ingestion for existing production traffic.
- First-party `@retrace/browser` SDK ingest for teams that want direct replay
  capture without requiring PostHog as the source.
- Heuristic detectors for console, network, interaction, render, toast, and
  abandonment signals.
- Replay-backed issue storage with status tracking.
- Local UI for onboarding, replay inspection, issue review, replay processing,
  regression spec generation, and prompt copying.
- Native and browser-backed UI tester workflows, including replay-derived
  regression specs.
- GitHub/local repository matching with likely culprit files.
- Evidence-aware Codex and Claude prompt generation.
- Docker Compose services for API, UI, worker, browser runner, and cron.

## Definitive Proposal

Build Retrace into an open-source product with one primary promise:

> When users hit UI failures in production, Retrace captures the failure,
> converts it into an issue and regression test, identifies likely source code,
> and hands a coding agent a constrained repair prompt with enough evidence to
> fix and verify the bug.

That requires hardening six product areas.

### 1. Capture

Capture must be trustworthy, privacy-aware, and easy to install.

- Keep PostHog as the fastest path for teams with existing recordings.
- Make `@retrace/browser` the first-party path for direct rrweb capture.
- Default to masked inputs and support block/mask selectors.
- Capture durable target metadata such as `data-testid`, `data-test`,
  `data-qa`, role, name, aria label, id, text, and element shape.
- Store replay batches locally or in self-host object storage.
- Document SDK key rotation and the read/write split between public SDK keys
  and service tokens.

### 2. Detect

Detection should be deterministic first, LLM-assisted second.

- Keep cheap detectors as the source of truth for every replay.
- Use LLMs to summarize, group, and explain only after deterministic signals
  identify a likely issue.
- Preserve raw evidence: stack frames, URLs, selectors, timestamps, HTTP status,
  console messages, trace IDs, and distinct user IDs when available.
- Add detector confidence and reason codes so contributors can improve behavior
  without changing the whole pipeline.

### 3. Triage

The UI should make a failure easy to understand in under a minute.

- Show issue status, affected users, occurrence count, first/last seen, and
  severity.
- Put replay, signal timeline, console/network evidence, and generated
  regression spec in one issue view.
- Support transitions: `new`, `ongoing`, `resolved`, and `regressed`.
- Promote issues to GitHub or Linear with stable dedupe IDs.
- Generate daily digests for new, resolved, and regressed failures.

### 4. Test Generation

Replay-derived tests should be robust enough for CI.

- Convert SDK click/input metadata into durable selectors before falling back to
  rrweb coordinates or text.
- Prefer `data-testid`, `data-test`, and `data-qa` selectors.
- Generate native specs for deterministic page, request, and assertion checks.
- Use Playwright/browser runner specs for interactive flows.
- Link generated specs back to issue and replay IDs.
- Re-run resolved issue specs automatically and mark failures as regressed.

### 5. Code Linking

Code matching should narrow the search space without pretending to be certain.

- Connect GitHub repositories and local checkouts.
- Rank candidate files using stack frames, route paths, component names,
  messages, selectors, and error evidence.
- Include file paths, symbols, line hints when available, and rationale in fix
  artifacts.
- Keep matching explainable so a coding agent can inspect and reject weak
  candidates.
- Support other code hosts later through the same repository metadata interface.

### 6. Agent Prompts

Agent prompts should be evidence-aware, scoped, and safe.

- Include replay evidence, detector signals, stack frames, logs, trace IDs, and
  candidate code files.
- Quote untrusted user-facing text and evidence as data, not instructions.
- Ask the agent to verify the issue against evidence before editing.
- Require a focused fix, a regression test, validation commands, and residual
  risk notes.
- Emit provider-specific prompt files for Codex, Claude, and future coding
  agents while keeping a common JSON artifact.

## Open-Source Quality Bar

Retrace should be considered full fledged when a new contributor can self-host
it, capture a real UI failure, generate a regression test, and use a coding
agent prompt without private infrastructure.

Required quality standards:

- One-command local setup and Docker Compose setup.
- Clear BYOK documentation for PostHog, LLM providers, GitHub, Linear, and SDK
  keys.
- Passing unit tests, replay-spec tests, prompt safety tests, browser SDK build,
  and Playwright runner tests in CI.
- Fixture-driven detector tests for every supported signal.
- Stable schemas for replay batches, issues, generated specs, and fix prompts.
- Privacy defaults that avoid storing sensitive input text by default.
- Contributor docs for adding detectors, test runners, sinks, and code matchers.
- Issue templates and examples that let users report detector false positives,
  SDK bugs, and generated-test failures.

## Milestone Roadmap

1. Product docs and onboarding: make the README, SDK docs, and product plan tell
   the same story.
2. SDK hardening: publish browser SDK packages, document key rotation, add
   integration examples for React, Next.js, and plain script tags.
3. Replay issue UX: improve issue detail pages with evidence timelines,
   generated spec status, candidate files, and prompt artifacts.
4. Regression reliability: expand Playwright runner coverage, stabilize
   selector generation, and add CI examples for generated specs.
5. Code intelligence: improve matching with sourcemaps, stack trace mapping,
   route manifests, and framework-specific component discovery.
6. Agent workflows: add prompt previews, artifact downloads, and optional MCP
   tools that expose findings, specs, candidates, and validation commands.
7. Self-host operations: document retention, storage sizing, backups, upgrades,
   cron jobs, service tokens, and object storage.
8. Community readiness: add contribution guides, governance, security policy,
   release notes, and example apps with seeded failures.

## Non-Goals

- Replace general analytics tools.
- Replace full observability platforms.
- Auto-merge code changes without tests and human review.
- Store secrets in generated prompt artifacts.
- Treat LLM output as the source of truth for whether a failure happened.

## Success Criteria

The project is working-quality open source when the demo path below works from a
fresh checkout:

1. Run `retrace demo seed` from a fresh checkout to create a local replay-backed
   issue and generated UI regression spec without external services.
2. Start Retrace locally with `retrace ui` or Docker Compose and inspect the
   replay-backed issue.
3. Create an SDK key and install `@retrace/browser` in a sample app.
4. Trigger a real UI failure in the browser.
5. Process the replay into a replay-backed issue.
6. Generate a UI regression spec from that issue.
7. Connect a GitHub/local checkout.
8. Generate fix prompts with likely source files.
9. Apply a fix with a coding agent.
10. Run the generated UI spec in CI or locally.
11. Mark the issue resolved and verify it does not regress.
