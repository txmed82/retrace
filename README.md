<p align="center">
  <img src="assets/retrace-banner.svg" alt="Retrace banner" width="100%" />
</p>

# Retrace

Your real users are your QA team. Retrace finds the bugs they hit,
writes the tests, and opens the fix PR.

One **unified incident queue** across five signal sources — replays,
UI tests, API tests, error monitoring (Sentry-compatible + OTel), and
PR review — then one command closes the loop:

> **user bug → auto-generated test → AI fix PR**

```bash
retrace qa auto --repo your-org/your-app
```

New here? See [`docs/quickstart.md`](docs/quickstart.md) for the
5-minute walkthrough. Want to know what's coming next, or claim a
roadmap item to work on? See [`docs/roadmap.md`](docs/roadmap.md).

Retrace is an open-source UI reliability loop: it pulls PostHog session
recordings or ingests first-party browser SDK replays, detects likely
breakage, clusters repeated failures, generates replay-backed regression
specs, matches issues to likely source files, and outputs actionable fix
prompts for coding agents.

## What Retrace Does

1. Capture live user browser sessions from PostHog or the `@retrace/browser`
   SDK.
2. Detect UX failures such as console errors, failed network calls, rage
   clicks, dead clicks, blank renders, error toasts, and abandonment after
   errors.
3. Cluster repeated failures into replay-backed issues with severity,
   affected users, session links, and evidence.
4. Surface every signal — replay finding, UI test failure, API test failure
   — as a single `Incident` shape.
5. Generate deterministic UI regression specs from real replay interactions
   so failures become testable behavior, not anecdotes.
6. Link each issue to a GitHub or local checkout, rank likely source files,
   and produce Codex/Claude prompts that include replay evidence and
   candidate code.
7. Open a draft PR (`gh`) with the fix prompt embedded so a coding agent or
   human can finish the loop.
8. Re-run generated tests after fixes and track issues as `new`, `ongoing`,
   `regressed`, or `resolved`.

The intended end state is a BYOK, self-hostable open-source product that
closes the loop between live user UI errors, automated UX testing,
automated UI regression testing, and coding-agent repair workflows. See
[docs/open-source-product-plan.md](docs/open-source-product-plan.md) for
the full proposal and quality bar.

## What You Get

- **Unified incidents** across replay findings, UI test failures, API
  test failures, error-monitor alerts (Sentry-compatible + OTel), and
  PR-review findings — same shape, same CLI, same `gh`-backed fix PR.
- **One-click reproduction.** Retrace converts an incident's recipe into a
  Browser Harness UI test, runs it, and confirms whether the bug still
  surfaces.
- **Fix PRs, not prompts in a folder.** Retrace scores the connected repo,
  writes a fix prompt, opens a branch, and creates a draft PR via `gh`.
  Optionally invokes a local agent (`claude`, `codex`) to apply changes
  before pushing.
- **Zero-config install.** One CLI command for setup, one `<script>` tag
  for replay capture.
- Session-level bug detection from rrweb data with regression tracking.
- LLM-written summaries and reproduction context.
- Local UI with rrweb replay, culprit files, and copyable prompts.
- Local Browser Harness UI tester with saved reusable specs.

## End-to-End Workflow

1. Run `retrace quickstart` (zero-config) or `retrace init`/`retrace ui` if
   you want to wire PostHog, LLM, and GitHub keys interactively.
2. Ingest live user sessions through PostHog (`retrace run`) or first-party
   SDK replay batches (`retrace api serve` plus `@retrace/browser`).
3. Process replays into signals and replay-backed issues, surfaced as
   unified `Incident` records.
4. Generate replay-derived regression specs with
   `retrace qa reproduce <INC-ID>` (or the legacy
   `retrace tester from-replay-issue <bug_public_id>` / local UI).
5. Connect a repository with `retrace github connect --repo <org/name>
   --local-path /path/to/repo`.
6. Run `retrace qa fix <INC-ID> --repo <org/name>` (or
   `retrace qa auto` for the full pipeline) to score the repo,
   render a fix prompt, and open a draft PR via `gh`. `retrace suggest-fixes`
   remains for the legacy report-based flow.
7. Apply fixes (or let `--apply auto` invoke `claude`/`codex`), run the
   generated specs plus the normal test suite, then mark issues resolved
   and let verification catch regressions.

## Quickstart (60 seconds)

Requires Python 3.11+.

```bash
uv venv
uv pip install -e ".[dev]"
retrace quickstart
```

`retrace quickstart` writes a minimal `config.yaml`, initializes the local
store, mints a browser-safe SDK key, and prints a ready-to-paste
`<script>` tag for your app's `<head>`:

```html
<script type="module">
  import { init } from "https://esm.sh/@retrace/browser@latest";
  init({
    apiKey: "rtpk_…",
    ingestUrl: "http://127.0.0.1:8788/api/sdk/replay",
  });
</script>
```

Then:

```bash
retrace api serve          # start the replay ingest API
retrace ui                 # (in another terminal) open the local UI
```

Interact with your app. Retrace turns the resulting replays into incidents.

## The killer demo

Once you have an open incident, run:

```bash
retrace github connect --repo your-org/your-app --local-path /path/to/checkout
retrace qa auto --repo your-org/your-app
```

That single command:

1. Picks the highest-priority open incident.
2. Auto-generates a UI test from the incident's reproduction recipe and
   runs it via Browser Harness.
3. If the bug reproduces, scores the repo, renders a fix prompt, opens a
   branch, and creates a draft PR.
4. Optionally invokes a local coding agent (`--apply auto`) to apply
   changes inside the branch before pushing.

Inspect incidents at any time:

```bash
retrace qa list
retrace qa show INC-XXXXXX
retrace qa reproduce INC-XXXXXX           # just the test step
retrace qa fix INC-XXXXXX --repo …        # just the PR step
```

The fix step runs inside a temporary `git worktree`, so your working tree
and current branch are never touched and repeat runs are idempotent.

## Backend tests (`retrace tester api-*`)

API test failures flow into the same `qa_incidents` table as UI tests,
replay findings, and monitor alerts — `retrace qa auto` handles backend
regressions for free.

```bash
retrace tester api-create --name "login should be 200" \
  --method POST --url https://api.example.com/login \
  --body-json '{"email":"demo@example.com","password":"hunter2"}' \
  --expected-status 200 \
  --json-assertion '{"path":"$.token","exists":true}'

retrace tester api-run <spec_id>
```

You can also import an OpenAPI spec to bootstrap a suite:

```bash
retrace tester api-import-openapi --spec ./openapi.yaml
retrace tester api-suite-list
```

When a spec fails it persists a canonical failure (with redacted
payloads) and mirrors it into `qa_incidents` so `retrace qa list` sees
it next to your replay and UI bugs.

## Error monitoring (Sentry-compatible + OTel)

Retrace ingests frontend and backend error events into the same
`qa_incidents` model. Three ingest surfaces are live:

- **Sentry-compatible DSN.** `retrace quickstart` prints a DSN you can
  drop into a `Sentry.init({ dsn })` call. Events land in `failures` /
  `incidents` and are mirrored to `qa_incidents`.
- **OpenTelemetry logs + traces.** `POST /v1/logs` and `POST /v1/traces`
  on the API server accept the OTLP JSON shape.
- **Monitoring webhooks.** Sentry alert webhooks, PostHog alerts, and a
  generic webhook payload are normalised by `monitoring_ingest.py`.

Stack frames are symbolicated via uploaded source maps
(`retrace api upload-source-map`), and alert rules
(`alert_rules.py`) gate which events become user-visible incidents.

### Replace Sentry in 5 minutes

`retrace quickstart` mints both a replay SDK key **and** a
Sentry-compatible DSN against the same workspace. Two snippets, paste
both:

```html
<!-- 1. Replay capture (rrweb-based) -->
<script type="module">
  import { init } from "https://esm.sh/@retrace/browser@latest";
  init({
    apiKey: "rtpk_…",
    ingestUrl: "http://127.0.0.1:8788/api/sdk/replay",
  });
</script>

<!-- 2. Drop-in Sentry replacement (any Sentry SDK works) -->
<script src="https://browser.sentry-cdn.com/7.114.0/bundle.min.js"></script>
<script>
  Sentry.init({ dsn: "http://rtpk_…@127.0.0.1:8788/<project_id>" });
</script>
```

That's the swap. Frontend errors flow into `qa_incidents` alongside
replay sessions, UI/API test failures, and PR review findings. Run
`retrace qa list` to see them; `retrace qa auto --repo org/app` to
reproduce + open a fix PR.

Backend SDKs (Python `sentry-sdk`, Node `@sentry/node`, etc.) work the
same way — the init call differs per language but the DSN is the only
thing that changes. For example:

```python
# Python
sentry_sdk.init(dsn="http://rtpk_…@127.0.0.1:8788/<project_id>")
```

```javascript
// Node / browser / React / Vue / etc.
Sentry.init({ dsn: "http://rtpk_…@127.0.0.1:8788/<project_id>" });
```

## Code review (`retrace review`)

Run Retrace's PR analyzer against a diff. With `--llm` (default ON
when an LLM is configured), it adds an AI summary, a per-file
walkthrough, and inline suggestions on top of the templated analysis.
Without an LLM, it still parses changed files, infers affected flows,
links prior failures that match the diff, and recommends missing
tests. Findings file `qa_incidents` so the same `qa auto` flow can
turn "PR touches a flaky surface" into a draft fix PR.

```bash
# Read a diff from a file, an URL, or stdin
retrace review --diff /tmp/pr.diff
gh pr diff 42 | retrace review --diff -
retrace review --pr https://github.com/org/repo/pull/42
retrace review --pr 42 --repo org/repo --file-incidents

# AI review + post to the PR:
retrace review --pr https://github.com/org/repo/pull/42 \
  --llm --post-comment --run-affected-tests

# Same, but ask the LLM to rank/dedupe its own suggestions in a second
# pass (doubles cost only when there is overflow):
retrace review --pr <…> --llm --llm-self-critique --post-comment

# Turn the LLM off explicitly even when configured:
retrace review --pr <…> --no-llm
```

The diff is run through Retrace's PII redactor before it leaves your
host — passwords, bearer tokens, common API-key shapes, and long
opaque tokens are masked before the LLM call. Large diffs are
chunked on file boundaries; anything over ~32k tokens is skipped
with an explicit "diff too large" note.

Quality guardrails applied to every review:

- **Line-validity filter** — inline suggestions whose `(path, line)`
  doesn't appear on the new side of the diff are dropped, so the model
  can't post a comment on a line that doesn't exist.
- **Suggestion + risk caps** — at most 3 inline suggestions and 5 risk
  notes survive per review, preferring suggestions that include
  concrete `suggested_code`.
- **Optional self-critique** (`--llm-self-critique`) — one extra LLM
  call ranks/dedupes when there's overflow.
- **Prior-review memory** — non-empty reviews are persisted to
  `data/retrace.db`. The next review on a PR touching the same files
  folds the prior risk notes into its prompt, so we don't keep
  re-flagging the same issue PR after PR.

The same logic is wired to the GitHub-App webhook (`github_app.py`) so
you can also drop Retrace on a PR with no CLI step. See
[`docs/github-app.md`](docs/github-app.md) for the install walkthrough
(create the App, set the webhook secret, install on a repo, trigger
with `@retrace review`).

## Visual regression baselines (`retrace tester baseline`)

Capture a known-good screenshot for each tester step, then have
subsequent runs compare against it. Mismatches produce `*-diff.png`
artifacts that `retrace qa auto` already treats as a confirmed-bug
signal.

```bash
# After a clean run, accept its screenshots as the baseline:
retrace tester baseline accept <spec_id> --run-dir data/ui-tests/runs/<run>

# On a later run, compare:
retrace tester baseline compare <spec_id> --run-dir data/ui-tests/runs/<later-run>

# List spec baselines on disk:
retrace tester baseline list
```

### Perceptual comparison (optional)

Install the `[image]` extra (Pillow + numpy) for perceptual SSIM
comparison + an annotated red-overlay diff PNG that shows the regions
that actually changed:

```bash
pip install 'retrace[image]'

retrace tester baseline compare <spec_id> \
  --run-dir data/ui-tests/runs/<later-run> \
  --mode auto \
  --threshold 0.95
```

`--mode auto` (default) uses perceptual when the extra is installed
and sha256 otherwise. `--mode sha256` forces byte equality (the
pre-P1.2 behavior). `--threshold 0.95` is the SSIM floor — gentle on
sub-pixel rendering and antialiasing, sensitive to real layout
shifts. Per-screenshot SSIM scores come back in the JSON output for
borderline runs.

## Repair lifecycle (`retrace repair`)

Once an incident has a fix prompt + likely files, the repair runner
takes over: it can invoke a local coding agent inside a sandbox, run
the project's validation commands, and verify the fix.

```bash
retrace repair list
retrace repair show <task_id>
retrace repair run <task_id>
retrace repair verify <task_id>
```

`retrace qa fix` and the repair flow can both reach the same incident —
QA owns the test, repair owns the diff.

## Ticket sinks (Jira / Linear / GitHub Issues)

Promote any incident into an external tracker, then keep state in sync.

```bash
retrace api promote-issue <issue_id> --sink github --repo org/repo
retrace api sync-tickets
retrace api verify-resolved
retrace api resolve-issue <issue_id>
```

Sinks are configured under `notifications:` and `issue_sinks:` in
`config.yaml`. Slack and webhook notifications fire on incident
lifecycle events.

## Daily digest (`retrace digest`)

A 30-second markdown rollup of new / regressed / resolved incidents
and the top-impact bugs by affected users. Wire it into a cron or
schedule it from the hosted control plane.

```bash
retrace digest --since 24h --out ./reports/digest.md
```

## Deploy correlation (`retrace api record-deploy`)

Record a deploy with its SHA + changed files; Retrace correlates
recent failures to the deploy that introduced them.

```bash
retrace api record-deploy --sha $GIT_SHA --changed-file src/foo.ts \
  --changed-file src/bar.ts
```

The deploy reference shows up on the incident detail and feeds the
PR-review prior-failure linkage.

## Legacy PostHog flow

If you already use PostHog session replay, the original pipeline still
works alongside the first-party SDK:

```bash
retrace init        # interactive PostHog + LLM setup
retrace run         # pull recent sessions and write a report
```

Report output:

- `./reports/YYYY-MM-DD-HHMMSS.md`

Try the replay-backed workflow without production traffic:

```bash
retrace demo seed
retrace tester list
retrace ui
```

`retrace demo seed` creates a local replay-backed checkout failure, processes it
through deterministic detectors, writes a replay-derived UI regression spec,
creates a tiny local demo source tree, connects it as `local/demo-checkout`, and
writes Codex/Claude fix prompts under `reports/fix-prompts`. Use `retrace
tester list` to inspect the generated spec; run `retrace tester
from-replay-issue <issue_public_id>` only when you want to generate another spec
from the same issue. Pass `--no-generate-fix-prompts` if you only want replay
and regression-test seed data.

For hosted or shared self-host installs, generate a complete browser/app-error
onboarding manifest:

```bash
retrace api onboard-hosted \
  --api-base-url https://retrace.example.com \
  --project Web \
  --environment production
```

The manifest creates a browser SDK key, a scoped service token, a Sentry DSN,
and copyable setup snippets for browser replay capture, Sentry-compatible error
ingest, monitoring webhooks, source-map uploads, alert rules, incident
lifecycle actions, and retention cleanup. Hosted control planes can also create
the same one-time manifest with `POST /api/onboarding/hosted?environment_id=...`
using an `admin` service token.

## Local UI (Onboarding + Replay + Prompts)

```bash
retrace ui
```

Open:

- `http://127.0.0.1:8787`

From the UI you can:

- Set/edit PostHog host, project ID, and API key
- Set/edit LLM provider, base URL, model, and API key
- Save settings to `config.yaml` + `.env`
- Run system checks for:
  - PostHog connectivity
  - LLM connectivity
  - `gh` installed/authenticated
  - first-party replay ingest API readiness
- Copy suggested terminal commands when `gh` is missing/not authed
- Copy the `retrace api serve` command when the ingest API is down
- Connect a GitHub-style `owner/name` repo and local checkout path for code
  matching
- Connect a path-only local codebase when the source is not hosted on GitHub
- Create write-only browser SDK keys and copy `@retrace/browser` install/init
  snippets for first-party replay capture
- Send a test replay with the generated SDK key to verify first-party ingest
- Browse findings from latest report
- Replay stored rrweb events
- Inspect first-party replay sessions and replay-backed issues
- Process queued first-party replay batches into signals and issues
- Generate native regression specs from replay-backed issues
- Inspect likely culprit files and copy Codex/Claude prompts

## BYOK Model

Retrace is designed for bring-your-own-key operation. Runtime secrets live in
`.env` or your deployment environment; `config.yaml` stores non-secret settings.

Common secret variables:

- `RETRACE_POSTHOG_API_KEY` for PostHog session recording ingestion
- `RETRACE_LLM_API_KEY` for OpenAI-compatible local or hosted providers
- `RETRACE_OPENAI_API_KEY`, `RETRACE_ANTHROPIC_API_KEY`, or
  `RETRACE_OPENROUTER_API_KEY` for named hosted providers
- `RETRACE_GITHUB_API_KEY`, `RETRACE_GITHUB_TOKEN`, or `GITHUB_TOKEN` for
  GitHub issue filing and repository metadata
- `RETRACE_LINEAR_API_KEY` for Linear issue filing

First-party browser capture uses write-only public SDK keys generated from the
local UI or `retrace api create-sdk-key`. Those keys can submit replay batches
but cannot read replay data or issues.
The `@retrace/browser` SDK captures console logs, failed network calls, clicks,
inputs, `window.onerror`, and `unhandledrejection` evidence. Browser exception
messages and stacks are redacted for common secrets and PII before ingestion.
Outgoing fetch/XHR calls carry W3C `traceparent` context when possible, and
captured network evidence preserves request/response trace IDs for repair
prompts and log correlation.

### Breadcrumb trail

The SDK keeps a 50-entry ring buffer of recent activity — clicks,
console output, fetch/XHR completions, history navigations, and
manually-added entries via `client.addBreadcrumb({...})`. When an
exception fires (or an external Sentry SDK sends an event with
`event.breadcrumbs.values`), the trail is attached and the server-side
ingest promotes it to `IncidentEvidence`:

- `category="console"` breadcrumbs → `console_excerpts`
- `category="http|fetch|xhr"` with `status >= 400` or `data.error` → `network_failures`
- The full raw trail also survives on `failure.metadata.breadcrumbs` for
  the repair prompt and dashboard to read.

Manual breadcrumbs follow the Sentry shape so any code that already
calls `Sentry.addBreadcrumb({...})` works as-is against Retrace:

```ts
import { init } from "@retrace/browser";
const client = init({ apiKey: "rtpk_…", maxBreadcrumbs: 50 });

client.addBreadcrumb({
  category: "auth",
  message: "login attempt",
  level: "info",
  data: { user_id: "u_123" },
});
```

Privacy: clicks on elements matching `privacy.maskTextSelector` record
the breadcrumb with the element's selector but without its visible text,
so a masked element can't leak content via the trail.

## Fix Suggestions Workflow

1. Connect repo metadata from the local UI or CLI:

```bash
retrace github connect --repo <org/name> --branch main --local-path /path/to/repo
```

2. Generate fix suggestions from latest report:

```bash
retrace suggest-fixes --latest --repo <org/name> --out ./reports/fix-prompts
```

Or generate fix suggestions directly from a replay-backed issue:

```bash
retrace suggest-fixes --replay-issue <bug_public_id> --repo <org/name> --out ./reports/fix-prompts
```

For multi-project installs, include the replay issue scope printed by the API or
`retrace demo seed`:

```bash
retrace suggest-fixes \
  --replay-issue <bug_public_id> \
  --project-id <project_id> \
  --environment-id <environment_id> \
  --repo <org/name> \
  --out ./reports/fix-prompts
```

Artifacts:

- `reports/fix-prompts/*.json`
- `reports/fix-prompts/*.codex.md`
- `reports/fix-prompts/*.claude.md`

## Core Commands

- `retrace quickstart` — zero-config setup; mints an SDK key and prints a `<script>` tag
- `retrace qa list|show|reproduce|fix|auto` — unified QA incidents across replay / UI / API / monitor / review
- `retrace review` — analyze a PR diff, link prior failures, recommend missing tests, file `qa_incidents`
- `retrace tester ...` — UI tests, AI suite drafts, API tests, API suites, OpenAPI import
- `retrace repair list|show|run|verify` — drive the local coding-agent repair loop
- `retrace digest` — daily markdown rollup of new / regressed / resolved incidents
- `retrace init` / `retrace doctor` — interactive PostHog + LLM setup; health checks
- `retrace run` — one-shot PostHog ingestion + detection + clustering
- `retrace demo seed` — seed a local replay-backed incident and generated spec
- `retrace ui` — local browser UI: onboarding, replay player, findings, fix prompts
- `retrace mcp serve` — single MCP server (findings + tester tools)
- `retrace github ...` — repo metadata management
- `retrace suggest-fixes ...` — legacy report-based candidate matching + prompt generation
- `retrace api serve` — first-party replay ingest + Sentry-compat + OTel endpoints
- `retrace api create-sdk-key` / `create-service-token` / `onboard-hosted` — browser SDK keys + service tokens + hosted-readiness manifest
- `retrace api promote-issue` / `sync-tickets` / `verify-resolved` / `resolve-issue` — Jira/Linear/GitHub Issues integration
- `retrace api record-deploy` — record a deploy SHA + changed files so failures correlate to releases
- `retrace api upload-source-map` — symbolicate frontend stack frames
- `retrace api import-posthog-replays` — pull PostHog sessions into the first-party replay store

## The Incident model

Every detector, test run, and error monitor signal converges on a single
`Incident` shape (see [`src/retrace/qa_incidents.py`](src/retrace/qa_incidents.py)):

```text
Incident
├─ identity         id, public_id (INC-XXXXXX), fingerprint
├─ context          project, environment, source_kind, status, severity
├─ symptom          title, summary, suspected_cause, expected/actual
├─ reproduction     ordered, typed steps (navigate/click/input/assert/api_call)
├─ evidence         replay session ids, stack frame, console, network, traces
└─ pipeline state   repro_status + spec/run ids, fix_status + branch + PR url
```

That single shape is what powers the killer demo: the same auto-repro and
auto-fix pipeline runs whether the incident started as a user replay, a
failed UI test, or a failed API contract test.

## License

Retrace is available under the MIT License. See [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the
GitHub issue templates for project-specific bug reports, detector feedback,
generated-test failures, feature requests, and pull request validation.

## Local UI Tester (Browser Harness First)

Retrace now includes a local-first tester workflow built around Browser Harness.

Create a reusable described test:

```bash
retrace tester create \
  --name "Signup flow" \
  --mode describe \
  --prompt "Go through signup and verify dashboard loads" \
  --app-url http://127.0.0.1:3000 \
  --start-cmd "npm run dev"
```

Create an AI suite-draft spec:

```bash
retrace tester create-suite \
  --name "Systematic app regression suite"
```

Run a saved spec:

```bash
retrace tester run <spec_id> --retries 1
```

Use the native HTTP runner for deterministic smoke specs without Browser Harness:

```bash
retrace tester create \
  --name "Homepage smoke" \
  --engine native \
  --app-url http://127.0.0.1:3000
```

Use `--engine auto` when the spec should declare intent and let Retrace choose
the runner. The run JSON includes `execution_engine` plus `engine_reason` so CI
logs explain the choice:

- Exact deterministic steps, assertions, and API-only HTTP steps run on the
  native engine. Retrace uses the native HTTP runtime unless the steps require a
  browser selector/action, then it uses Playwright.
- Exploratory goals run on the explore engine. Mark the spec fixture
  `requires_visual` or set `browser_settings.visual` to route exploratory work
  to the visual engine.
- Open-ended prompts without deterministic steps or exploratory goals use
  Browser Harness.
- Authenticated exploratory specs also use Browser Harness because that runner
  receives the auth context.
- Explicit `harness`, `native`, `explore`, or `visual` engine settings are
  honored and still explain the selected runtime.

List specs and runs:

```bash
retrace tester list
retrace tester runs
```

Queue a spec for the self-host browser runner:

```bash
retrace tester enqueue <spec_id>
retrace tester worker --once
```

Generate a native regression spec from a replay-backed issue:

```bash
retrace tester from-replay-issue <bug_public_id>
```

The local UI also exposes this from each replay issue detail view with
**Generate Regression Spec**. Generated specs keep links back to the source issue
and replay in their fixtures so resolved issues can later be re-verified.

Spec and run artifacts are stored under:

- `data/ui-tests/specs/*.json`
- `data/ui-tests/queue/*.json`
- `data/ui-tests/runs/*/run.json`
- `data/ui-tests/runs/*/harness.log`

UI support:

- `retrace ui` now includes a `Local UI Tester` panel for:
  - `Describe Test` mode (per-test prompt)
  - `AI Explore Full Suite` mode (systematic suite draft)
- Onboarding includes tester auth setup (none/form/JWT/custom headers), and secret fields keep existing values when left blank.
- Tester runs are flake-aware (retry count + flake classification shown in recent runs).
- Native tester runs write structured assertion and artifact metadata under the run directory.

API tests use first-class specs under `data/api-tests/`:

```bash
retrace tester api-create \
  --name "Health API" \
  --method GET \
  --url http://127.0.0.1:3000/api/health \
  --expected-status 200 \
  --json-assertion '{"path":"$.ok","equals":true}'

retrace tester api-list
retrace tester api-run <api_spec_id>
retrace tester api-run <api_spec_id> --repo-path ./my-app
retrace tester api-from-replay-issue <bug_public_id>
retrace tester api-import-openapi ./openapi.yaml --base-url http://127.0.0.1:3000
```

API specs support query params, headers, JSON bodies, bearer-token auth via env
vars, JSON body assertions, simple JSON schema assertions, latency budgets, and
setup/teardown script steps. Request and response artifacts are saved with
authorization, cookies, API keys, tokens, passwords, and secrets redacted.
`api-from-replay-issue` creates an API regression spec from a failed replay
network signal and links it to the source failure as coverage.
`api-import-openapi` imports OpenAPI JSON/YAML into contract-derived smoke specs,
with optional `--path-filter` and `--method` selection and response schema
assertions generated from the contract.
Failed `api-run` executions are persisted as canonical failures with request and
response evidence, a repair prompt containing the exact API reproduction, and
route-matched likely files when `--repo-path` is provided.

## First-Party Replay API

Retrace can ingest browser SDK replays directly and process them into replay-backed issues.

Create an SDK key from the local UI or CLI:

```bash
retrace api create-sdk-key --project Web --environment production
```

Run the ingest/read API:

```bash
retrace api serve
```

Browser SDK ingest endpoint:

- `POST /api/sdk/replay`

## Python SDK (backend exception capture)

Retrace ships a first-party Python SDK
([`packages/python-sdk`](packages/python-sdk/)) for capturing backend
exceptions, breadcrumbs, and user/tag context from FastAPI, Flask,
and Django apps. Wire-compatible with the Sentry envelope protocol,
so it speaks to the same `/api/sentry/<project_id>/envelope/` endpoint.

```bash
pip install retrace-sdk
# Optional framework extras
pip install 'retrace-sdk[fastapi]' 'retrace-sdk[flask]' 'retrace-sdk[django]'
```

```python
import retrace_sdk

retrace_sdk.init(
    dsn="http://rtpk_…@127.0.0.1:8788/<project_id>",
    release="v1.2.3",
    integrations=[
        retrace_sdk.FastAPIIntegration(),       # or Flask / Django
        retrace_sdk.LoggingIntegration(),
    ],
)

retrace_sdk.add_breadcrumb(category="auth", message="login attempt")
retrace_sdk.set_user({"id": "u_123"})

try:
    do_thing()
except Exception:
    retrace_sdk.capture_exception()
```

Full walkthrough: [`docs/python-sdk.md`](docs/python-sdk.md).

## Storage backend (SQLite default, Postgres optional)

Retrace stores everything in SQLite by default — the file lives at
`data/retrace.db` (or wherever `config.yaml` points). SQLite is
fine to ~100k events/day. Past that, point `Storage` at a Postgres
instance:

```bash
pip install 'retrace[postgres]'   # adds psycopg3

# Use a `postgresql://` URL anywhere a path was accepted:
RETRACE_DB="postgresql://user:pass@db.example.com:5432/retrace" \
  retrace api serve
```

How it works: `Storage(...)` detects the URL scheme and routes to a
`Backend` implementation (`SqliteBackend` or `PostgresBackend`). Both
implementations expose the same connection surface; SQL translation
happens at execute time so existing sqlite3-flavored queries
(`?` placeholders, `datetime('now', ?)`, `INSERT OR IGNORE`) work
against Postgres unchanged. Schema DDL goes through a similar
translator (`AUTOINCREMENT` → `BIGSERIAL`, ISO-text timestamp
defaults). CI runs the full storage smoke against a `postgres:16-alpine`
service container — see [`docs/study-notes/postgres-backend.md`](docs/study-notes/postgres-backend.md)
for the design.

## GitHub Actions

Three drop-in composite actions live under
[`.github/actions/`](.github/actions/):

- [`pr-review`](docs/github-actions.md#pr-review) — post Retrace's
  templated + optional LLM review as a PR comment on every pull
  request.
- [`source-map-upload`](docs/github-actions.md#source-map-upload) —
  record a deploy marker and upload all `*.map` files. Pure
  `curl` + `jq`, no Python install per run.
- [`qa-auto`](docs/github-actions.md#qa-auto) — `workflow_dispatch`
  trigger for the killer-demo flow: pick the top open QA incident,
  auto-generate a UI test that reproduces it, open a draft fix PR.

8-line quickstart:

```yaml
# .github/workflows/retrace-review.yml
name: Retrace review
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: txmed82/retrace/.github/actions/pr-review@main
```

Full reference + fork-safety patterns: [`docs/github-actions.md`](docs/github-actions.md).

Replay dashboard/read endpoints require a service token:

```bash
retrace api create-service-token --project Web --scope replay:read --scope issues:read --scope replay:write
```

Available read/process endpoints:

- `GET /api/replays?environment_id=...`
- `GET /api/replays/{replay_id}?environment_id=...`
- `GET /api/issues?environment_id=...`
- `GET /api/metrics`
- `POST /api/replays/process`

Monitoring webhook ingest also uses service tokens. Create a token with
`monitoring:write`, `ingest`, or `admin`, then point Sentry or PostHog exception webhooks at:

- `POST /api/monitoring/webhook/sentry?environment_id=...`
- `POST /api/monitoring/webhook/posthog?environment_id=...`

Retrace normalizes those alerts into canonical `monitor_incident` failures and
dedupes repeated alerts by provider external ID, so existing error-monitoring
alerts can feed the same evidence and repair workflow as replay and API failures.
Related monitor failures are also grouped into incidents by stack frame, route,
service, trace, deploy, and fingerprint. Each incident rolls up severity,
affected failures, evidence, and a single repair task.

### Alert fan-out (Slack / Discord / PagerDuty / webhook)

When an alert rule fires (`action=alert`), Retrace can fan it out to
one or more external channels. Routes are scoped by `(project,
environment)` and optionally by named rule. Each route ships its own
payload format (Slack Block Kit, Discord embeds, PagerDuty Events v2,
or a generic descriptive JSON for custom webhooks) and has a
configurable dedup window so a flapping fingerprint doesn't double-post.

```bash
# Wire production crashes to Slack and an archive webhook
retrace monitor route add \
  --name prod-slack \
  --kind slack \
  --url https://hooks.slack.com/services/T0/B0/X \
  --severity high

retrace monitor route add \
  --name archive \
  --kind webhook \
  --url https://archive.example.com/alerts

# PagerDuty Events v2 — `--secret` is the routing key
retrace monitor route add \
  --name oncall-pd \
  --kind pagerduty \
  --url https://events.pagerduty.com/v2/enqueue \
  --secret "$PAGERDUTY_ROUTING_KEY" \
  --severity critical

retrace monitor route list
retrace monitor route test prod-slack   # synthetic alert, bypasses dedup
retrace monitor route delete archive
```

Every dispatch attempt is logged in `alert_dispatches` for audit; the
table also drives the dedup gate. Network failures never roll back
the failure/incident write — the dispatcher swallows per-route errors
and records `status=failed` on the dispatch row.

Ingest endpoints use per-project, per-environment fixed-window rate limits before
expensive parsing or persistence. Default limits are 600 replay batches/minute
per SDK key, 600 Sentry-compatible events/minute per SDK key, 300 monitoring
webhooks/minute per service token/provider, and 30 source-map uploads/minute per
service token. Limited requests return `429` with `Retry-After`,
`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, and
`X-RateLimit-Window` headers.

Upload production source maps before or during deploys so minified browser
errors resolve to original source paths in failure metadata, incident grouping,
and repair prompts:

```bash
retrace api upload-source-map \
  --release "$GITHUB_SHA" \
  --artifact-url https://cdn.example.com/assets/app.min.js \
  dist/assets/app.min.js.map
```

Hosted/self-host API uploads use a service token with `source_maps:write`,
`ingest`, or `admin`:

- `POST /api/source-maps?environment_id=...`

Send JSON with `release`, `artifact_url`, optional `dist`, and `source_map`.
Sentry-compatible events map stack frames when their `release`, `dist`,
generated filename, line, and column match an uploaded Source Map v3 artifact.

App-error alert rules can mark matching incidents active or suppressed before
they reach downstream workflows:

- `GET /api/app-error-alert-rules?environment_id=...&limit=100&offset=0`
- `POST /api/app-error-alert-rules?environment_id=...`

`POST` upserts by rule `name` and returns `202`. Send JSON with `name`,
`action` (`alert` or `suppress`), optional `enabled`, optional integer
`precedence`, and match fields: `min_severity`, `provider`, `title_contains`,
`fingerprint_contains`, and `route_contains`. Rules with higher `precedence`
match first; ties keep original creation order. `min_severity` accepts `low`,
`medium`, `high`, or `critical`; when omitted, the rule has no severity floor.

Example:

```json
{
  "name": "Suppress low-priority beta checkout noise",
  "action": "suppress",
  "precedence": 10,
  "min_severity": "medium",
  "provider": "sentry",
  "route_contains": "/checkout"
}
```

Matching rule metadata is written onto the canonical failure and incident
response as `alert_state` and `alert_rule_name`.

App-error incidents have an explicit lifecycle. Use a service token with
`app_errors:write` or `admin` to resolve, ignore, reopen, triage, or mark an
incident investigating:

- `POST /api/app-errors/{incident_public_id}/lifecycle?environment_id=...`

Send JSON with either `action` (`resolve`, `ignore`, `reopen`, `triage`, or
`investigate`) or explicit `status` (`open`, `triaged`, `investigating`,
`resolved`, or `ignored`). Optional `reason`, `actor_type`, `actor_id`, and
object `metadata` are recorded in an append-only lifecycle history. The
transition updates linked monitoring failure statuses so retention, lists, and
repair workflows agree with the incident state. New matching failures reopen a
resolved or ignored incident and add a system lifecycle event.

App-error retention pruning is available for hosted or self-hosted cleanup jobs:

- `POST /api/app-errors/prune?environment_id=...`

Use a service token with `app_errors:write` or `admin`. The JSON body accepts
`failure_retention_days`, `evidence_retention_days`, `source_map_retention_days`,
`rate_limit_retention_hours`, and optional `dry_run`. Pruning removes old
resolved/ignored app-error failures, associated evidence and incident links,
stale source maps, and stale rate-limit rows while leaving active incidents
intact.

Deploy markers can be recorded from CI with `POST /api/deploys?environment_id=...`
or locally with `retrace api record-deploy --sha <commit> --changed-file <path>`.
Failures after the deploy are linked to the nearest marker, and incident repair
tasks include the deploy's changed files as likely repair context.

Retrace also accepts compact OpenTelemetry-style JSON at
`POST /api/otel/v1/logs?environment_id=...` and
`POST /api/otel/v1/traces?environment_id=...`. Logs and spans are stored as
local excerpts and linked into failure evidence when their trace/span IDs match
known failure metadata.

Process queued final replay batches locally:

```bash
retrace api process-replays --limit 25
```

## Self-Host Compose Stack

```bash
docker compose up -d
```

The compose stack now runs separate containers for:

- `api` on `http://127.0.0.1:8788`
- `ui` on `http://127.0.0.1:8787`
- `worker` for queued replay finalization
- `browser-runner` for queued UI test specs
- `cron` for scheduled PostHog ingestion/report generation

All services share mounted `config.yaml`, `.env`, `data`, and `reports` paths.
The SQLite database, replay blobs, UI test specs, run artifacts, and cache entries
live under `./data`; generated reports live under `./reports`.

Before upgrading a self-host install, stop the stack and back up `./data`,
`./reports`, `config.yaml`, and `.env`. Pull or build the new image, then run:

```bash
docker compose run --rm api retrace doctor
docker compose up -d
```

Size persistent storage around replay volume. A small team can usually start with
10-20 GB for `./data`; increase this when retaining high-traffic replay sessions,
native tester response artifacts, or long report history.

## MCP Server (Single Server, Multiple Tools)

Run:

```bash
retrace mcp serve
```

Supported MCP tools:

- `retrace.list_findings`
- `retrace.list_tester_specs`
- `retrace.create_tester_spec`
- `retrace.run_tester_spec`

## Detectors (v0.1)

- `console_error`
- `network_4xx`
- `network_5xx`
- `rage_click`
- `dead_click`
- `error_toast`
- `blank_render`
- `session_abandon_on_error`

Toggle detectors in `config.yaml`.

## Exploratory UI Tester (engine: `explore`)

The exploratory engine drives a real Playwright browser via a small bounded
tool surface (navigate, click, type, press, wait_for, snapshot, finish) and
asks the configured LLM, step by step, what to do next given the current
observation. Successful explorations persist a durable "skill" — the sequence
of tool calls that worked — under `data/ui-tests/skills/<host>/`, so future
runs against the same domain start from a known-good prefix.

Create an exploratory spec:

```bash
retrace tester create \
  --name "Signup explore" \
  --engine explore \
  --app-url http://127.0.0.1:3000 \
  --prompt "Complete signup and reach the dashboard"
```

Set `auto` to let Retrace pick: specs with `exact_steps` go native; specs with
`exploratory_goals` and no exact steps go through the explore engine.

For apps where the accessibility tree is sparse or hostile (canvas editors,
maps, custom shadow-DOM widgets), use `--engine visual`. The visual engine
drives the browser via screenshots and pixel coordinates instead of
selectors. It requires a multimodal LLM (Claude 3.5+ Sonnet, GPT-4o, etc.)
and skips step caching because pixel coords aren't portable. See
[docs/visual-execution-mode.md](docs/visual-execution-mode.md) for details.

Skills land at `data/ui-tests/skills/<host_slug>/<spec_id>.json` and are
surfaced to the model on subsequent runs as a "head-start."

## Daily Digest

Roll up replay activity in a window into a markdown report — what's new,
regressed, resolved, and the top open issues by impact.

```bash
retrace digest                                 # default 24h window, writes reports/
retrace digest --lookback-hours 168 --top 10   # weekly digest, top-10 open
retrace digest --notify                        # also fan out via notification sinks
retrace digest --format json                   # machine-readable summary
```

## Verifying Resolved Issues

Re-run repro specs against issues you marked resolved. If the spec still
fails, the issue transitions back to `regressed` and an `issue.regressed`
notification fires so the team knows the fix didn't stick.

```bash
retrace api verify-resolved --limit 10
retrace api verify-resolved --dry-run     # show plan only
retrace api resolve-issue bug_xxx         # mark resolved + fire issue.resolved
```

## Filing Replay Issues to Linear / GitHub

Retrace can promote replay-backed issues directly into Linear or GitHub. Provide
API keys the same way as PostHog — via `.env` or inline in `config.yaml`.

```bash
# .env
RETRACE_LINEAR_API_KEY=lin_api_...
RETRACE_GITHUB_API_KEY=ghp_...    # or RETRACE_GITHUB_TOKEN, or GITHUB_TOKEN
```

```yaml
# config.yaml
linear:
  team_key: ENG          # or team_id: <uuid>
  labels: [retrace]

github_sink:
  repo: acme/web
  labels: [retrace]
```

Promote an issue:

```bash
retrace api promote-issue --provider linear  bug_xxx
retrace api promote-issue --provider github  --repo acme/web  bug_xxx
retrace api promote-issue --provider github  --dry-run  bug_xxx   # stub, no API call
```

When no API key is configured (or `--dry-run` is passed), Retrace emits a stub
external ID/URL and dedupes locally — useful for scripted testing.

## Runtime Data

- `config.yaml` — non-secret config
- `.env` — secrets (`RETRACE_POSTHOG_API_KEY`, optional `RETRACE_LLM_API_KEY`, optional `RETRACE_LINEAR_API_KEY`, optional `RETRACE_GITHUB_API_KEY` / `RETRACE_GITHUB_TOKEN` / `GITHUB_TOKEN`, optional `RETRACE_NOTIFY_WEBHOOK_URL`, optional `RETRACE_NOTIFY_WEBHOOK_SECRET`, optional `RETRACE_NOTIFY_SLACK_WEBHOOK_URL`, optional tester auth secrets)
- LLM providers supported: `openai_compatible` (local/custom), `openai`, `anthropic`, `openrouter`
- `data/retrace.db` — run/session/findings metadata
- `data/sessions/*.json` — ingested rrweb events
- `reports/*.md` — findings reports
- `reports/fix-prompts/*` — generated fix artifacts

## CI/CD (GitHub Actions)

This repo includes `.github/workflows/ci-cd.yml` with:

- **CI on every push and pull request**
  - Python 3.11 setup
  - `uv` dependency install
  - `ruff check src tests`
  - `pytest -q`
- **Docker build validation on every push and pull request**
  - Builds `docker/Dockerfile` (no publish)
- **CD on pushes to `main`**
  - Publishes Docker image to GHCR:
    - `ghcr.io/<owner>/<repo>:sha-<commit>`
    - `ghcr.io/<owner>/<repo>:latest`

Notes:

- GHCR publishing uses `GITHUB_TOKEN` with `packages: write`.
- Secrets in `.env` are not used by CI/CD; keep runtime secrets in your deployment environment.

## Cron / Background Execution

```bash
docker compose up -d
```

The cron container runs four scheduled jobs by default:

- `RETRACE_CRON` (default `0 */6 * * *`) — `retrace run` (PostHog ingest + report)
- `RETRACE_DIGEST_CRON` (default `0 8 * * *`) — `retrace digest --notify`
- `RETRACE_VERIFY_CRON` (default `30 8 * * *`) — `retrace api verify-resolved`
- `RETRACE_SYNC_TICKETS_CRON` (default `15 * * * *`) — `retrace api sync-tickets`

Override any of them via the `cron` service environment in `docker-compose.yml`.

## Design Docs

- `docs/ai-qa-suite-source-of-truth.md`
- `docs/open-source-product-plan.md`
- `docs/superpowers/specs/2026-04-19-retrace-design.md`
- `docs/superpowers/plans/2026-04-19-retrace-plan-a-vertical-slice.md`

## Contributing

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [SECURITY.md](SECURITY.md)
