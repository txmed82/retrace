# Retrace roadmap

**Last updated:** 2026-05-11
**Maintained by:** the engineering team
**How to evolve this file:** when you finish an item, set its status to
`DONE`, add a date, link the PR, and append a one-line entry to the
**Update log** at the bottom. When new items appear, slot them into the
right lane (P0 / P1 / P2) and update the **Now / Next / Later** table.

---

## Mission

Retrace is the open-source QA loop: capture real-user bugs, write the
regression test, open the fix PR. Five signal sources → one
`qa_incidents` queue → one command (`retrace qa auto`).

The roadmap below closes the gaps the 2026-05-11 audit surfaced, in
priority order, **while continuously studying and stealing from the
best OSS competitors in each pillar.**

## Out of scope (for now)

- **Mobile SDKs** (iOS / Android / React Native / Flutter). Browser
  and backend Python/Node only until the desktop story is loveable.
- **Hosted cloud version.** Self-host first.
- **Paid tier.** OSS-only until the product earns it.

---

## Current state snapshot (2026-05-11)

After PRs #112–#124:

| Pillar | Score | One-line state |
|---|---|---|
| Replay capture + repair | 6.5/10 | Solid rrweb capture; worktree-based fix PRs work end-to-end. Browser-only SDK. |
| AI UI testing | 7/10 | Three engines; visual baselines added; no parallelism, no flake quarantine. |
| API testing | 5/10 | OpenAPI import works; no env profiles UI, no request recording, no contract diff. |
| Code review | 3/10 | **LLM-free** today. Polyglot route detection + `--post-comment` works, but no AI-driven review. |
| Error monitoring | 4/10 | Sentry-compat DSN + OTel ingest; no real-time alerts, no quotas, no native non-JS SDK. |

The roadmap items below are ordered to lift the lowest-scoring pillar
first, since that's the credibility hole an OSS launch will be judged
on.

---

## Development cycle — "Study → Steal → Improve"

Every roadmap item below uses the same dev loop. Don't skip steps.

1. **Read this entry top-to-bottom.** Understand the OSS comparables
   and the takeaways listed.
2. **Clone the comparables into `external/` (gitignored).** Pin to the
   commit SHA in this doc — if a doc lists `pin: <sha>`, use it
   verbatim. If `pin: HEAD`, take whatever's current and update this
   doc with the SHA you used.

   ```bash
   mkdir -p external && cd external
   git clone --depth 1 https://github.com/<org>/<repo>.git <repo>
   ( cd <repo> && git checkout <sha> )
   ```

3. **Read only the files this doc lists.** Don't get lost in the
   whole repo. Write a 5-bullet study note to
   `docs/study-notes/<repo>.md`. What the file does, what's clever,
   what's worth taking, what's NOT worth taking, what we'd improve.
4. **Open a branch named `feat/<item-id>-<slug>`.**
5. **Write code following the "Our improvement" plan below.** Keep
   scope tight to the item's acceptance criteria — don't bundle
   adjacent improvements.
6. **Write tests first or alongside.** Each item lists explicit test
   requirements.
7. **Run the local gates: `uv run pytest -q` AND `uv run ruff check
   src tests`.** Both must be green.
8. **Push, open PR via `gh pr create`.** Body lists the OSS sources
   you studied + acceptance-criteria checklist.
9. **Watch CI + CodeRabbit.** Fix every actionable finding before
   merge.
10. **Squash-merge** and update this doc's **Update log**.

> The point of "Study → Steal → Improve" is NOT to copy code. It's to
> avoid re-inventing patterns that 5 other teams have spent years
> on. Bring our spin: tighter scope, fewer deps, fits the
> `qa_incidents` model.

---

## Now / Next / Later

| Lane | Items |
|---|---|
| **Now (this week)** | P0.1 LLM-powered PR review · P0.2 Python SDK · P0.3 GitHub Actions templates · P0.4 Browser SDK breadcrumbs |
| **Next (weeks 2–3)** | P1.1 Real-time alerts · P1.2 Perceptual visual diff · P1.3 Diff-aware affected tests · P1.4 API tester env profiles UI |
| **Later (month 2+)** | P2.2 Rate limiting + sampling · P2.4 Multi-tenancy + audit log (P1.5 ✅, P2.3 ✅ done; P2.1 deferred — GitHub-only at launch) |

---

# P0 — biggest credibility wins

## P0.1 — LLM-powered PR review

**Status:** DONE 2026-05-11 · **PRs:** #126 (initial) + this PR (quality follow-up: line-validity, suggestion cap, self-critique, prior-review memory) · **Owner:** Claude

### Why this first

`retrace review` today has zero LLM content. It lists changed files,
links prior failures, recommends missing tests — all useful, but it's
the only modern "AI code review" tool that doesn't actually use an LLM
to read the code. **Closing this gap is the single biggest
credibility win.** PR-Agent and CodeRabbit have set the bar.

### OSS to study

| Repo | URL | Pin | What to read |
|---|---|---|---|
| **qodo-ai/pr-agent** | https://github.com/qodo-ai/pr-agent | HEAD | `pr_agent/tools/pr_reviewer.py`, `pr_agent/tools/pr_description.py`, `pr_agent/algo/pr_processing.py`, `pr_agent/algo/git_patch_processing.py`, `pr_agent/settings/pr_reviewer_prompts.toml` |
| **sweepai/sweep** | https://github.com/sweepai/sweep | HEAD | `sweepai/agents/`, `sweepai/handlers/on_review.py` |
| **semgrep/semgrep** | https://github.com/semgrep/semgrep | HEAD | `cli/src/semgrep/run_scan.py`, `cli/src/semgrep/output.py` |
| **github/codeql** | https://github.com/github/codeql | HEAD | Skim the rule format — for "what shape do good security findings have" |

### Takeaways to extract (write into `docs/study-notes/pr-agent.md`)

- Their prompt structure (system + user + diff chunks).
- How they handle large diffs (chunking strategy + token budgeting).
- Inline suggestions format — GitHub's `suggestion` code-block syntax.
- The "walkthrough" / "describe" / "review" / "ask" tool split — why
  separate concerns helps prompt quality.
- What we should NOT take: PR-Agent's settings system is heavy; we'll
  stay with our existing config.

### Our improvement plan

1. **New module:** `src/retrace/llm_pr_review.py`. Public API:

   ```python
   def llm_review(
       *,
       diff_text: str,
       analysis: PRReviewAnalysis,
       llm_client: LLMClient,
       max_tokens_per_chunk: int = 6000,
   ) -> LLMReviewResult: ...
   ```

   `LLMReviewResult` dataclass:
   - `summary: str` — 3–5 sentence "what this PR does"
   - `walkthrough: list[str]` — per-changed-file bullet
   - `inline_suggestions: list[InlineSuggestion]` —
     `(path, line, body, suggested_code)`
   - `risk_notes: list[str]` — security / complexity / correctness
   - `model: str`, `prompt_version: str` for observability

2. **Chunk diffs over `max_tokens_per_chunk`** by splitting on
   `--- a/<file>` boundaries. Keep file-aware context. Don't bisect
   inside a single hunk.

3. **Add `--llm` flag to `retrace review`** (default `auto`: on if
   `config.yaml` has an LLM configured, off otherwise). Wire into
   `commands/review.py` and fold the LLM output into `--post-comment`.

4. **PR comment formatting:** prepend the LLM summary above the
   existing "Affected flows / Prior failures / Missing coverage"
   sections. Inline suggestions emit as separate `gh pr review`
   comments using the GH suggestion-block format.

5. **Token guardrails:** hard-cap at 32k input tokens; bail with
   `"diff too large for LLM review"` if exceeded.

### Acceptance criteria

- [ ] `retrace review --pr <…> --llm --post-comment` produces a PR
      comment with a summary section and at least one inline
      suggestion when an LLM is configured.
- [ ] Falls back gracefully (no error) when no LLM key is configured —
      reverts to the templated-only review.
- [ ] Handles diffs > 6000 tokens via chunking; never sends > 32k
      tokens in one request.
- [ ] LLM output never contains PII from the diff (run through
      `redact_sensitive_text` before posting).
- [ ] Caches the (`diff_sha256`, `model`) → `LLMReviewResult` for 24h
      so a `gh pr` retry doesn't double-burn tokens.

### Tests

- `tests/test_llm_pr_review.py`:
  - Mocks `LLMClient.chat()`; asserts the prompt contains diff + analysis
  - Chunking: 3 file diff > limit → 3 requests; output stitched
  - No LLM configured: function returns `LLMReviewResult(summary="", ...)`
    cleanly
  - PII redaction: a `password=hunter2` in the diff is masked before
    being sent to the LLM
- Update `tests/test_review_cli.py` to assert `--llm` flag wires
  through

### Definition of Done

- All acceptance items checked
- Full suite green (`pytest -q`)
- `ruff check src tests` clean
- CodeRabbit review addressed
- README + `docs/quickstart.md` mention `--llm`
- `docs/study-notes/pr-agent.md` exists with the 5-bullet study note

---

## P0.2 — Python SDK with framework integrations

**Status:** DONE 2026-05-11 · **PR:** this PR · **Owner:** Claude

### Why now

The Sentry-compat ingest path means any `sentry-sdk` user can already
send events to Retrace. But:

- It's a Sentry library, not ours.
- It doesn't capture Retrace-specific context (workspace, repo
  reference, deploy SHA).
- It can't double as the replay-batch SDK if we ever want server-
  rendered captures.

A native `retrace-sdk` opens the door for FastAPI / Flask / Django
users — the majority of indie backend devs.

### OSS to study

| Repo | URL | Pin | What to read |
|---|---|---|---|
| **getsentry/sentry-python** | https://github.com/getsentry/sentry-python | HEAD | `sentry_sdk/scope.py`, `sentry_sdk/hub.py`, `sentry_sdk/integrations/django.py`, `sentry_sdk/integrations/fastapi.py`, `sentry_sdk/integrations/flask.py`, `sentry_sdk/transport.py` |
| **rollbar/pyrollbar** | https://github.com/rollbar/pyrollbar | HEAD | `rollbar/__init__.py` — simpler reference for transport |
| **honeybadger/honeybadger-python** | https://github.com/honeybadger-io/honeybadger-python | HEAD | `honeybadger/core.py` for context-locals approach |
| **logfire** | https://github.com/pydantic/logfire | HEAD | OTel-first Python — for the modern API surface |

### Takeaways to extract (write into `docs/study-notes/sentry-python.md`)

- Their context-locals approach (`Hub`, `Scope`, `with sentry_sdk.push_scope()`)
- How integrations register themselves (`Integration` subclasses,
  monkey-patching on import)
- Transport pattern (background thread queue + flush)
- What we should NOT take: their public API has decade of legacy; we
  can ship a smaller surface

### Our improvement plan

1. **New package:** `packages/python-sdk/`. Layout:

   ```
   packages/python-sdk/
     retrace_sdk/
       __init__.py          # init(), capture_exception(), add_breadcrumb(), set_user/tags/context
       transport.py         # background-thread HTTP queue
       scope.py             # contextvars-based scope
       integrations/
         __init__.py
         _base.py
         fastapi.py
         flask.py
         django.py
         requests.py
         logging.py         # stdlib logging.Handler
     tests/
     pyproject.toml
     README.md
   ```

2. **Public API** (deliberately small):

   ```python
   import retrace_sdk

   retrace_sdk.init(
       dsn="http://rtpk_…@127.0.0.1:8788/<project_id>",
       integrations=[retrace_sdk.FastAPIIntegration()],
       traces_sample_rate=1.0,
       max_breadcrumbs=50,
       release="v1.2.3",
   )

   retrace_sdk.add_breadcrumb(category="auth", message="login attempt", level="info")
   retrace_sdk.set_user({"id": "u_123"})
   retrace_sdk.capture_exception(exc)
   ```

3. **Transport:** Sentry-envelope POST to the existing
   `/api/sdk/sentry/<project>` endpoint (we already accept that
   shape). Use a background thread + bounded queue; drop the oldest
   on overflow.

4. **Integrations:**
   - **FastAPI:** middleware that captures unhandled exceptions +
     records request breadcrumb.
   - **Flask:** `Flask.errorhandler` hook.
   - **Django:** middleware via `django.urls.resolve()` for route
     awareness.
   - **requests:** wrap `Session.send` to add network breadcrumbs.
   - **logging:** a `logging.Handler` that turns `ERROR`+ logs into
     events.

5. **Publish path:** add to `pyproject.toml` workspace; document
   `pip install retrace-sdk` even before we publish to PyPI (uv pip
   install from path works for early adopters).

### Acceptance criteria

- [ ] Installing `retrace-sdk` and calling `retrace_sdk.init()` + a
      raise produces a `qa_incident` in the local store (via the
      Sentry-compat ingest).
- [ ] FastAPI/Flask/Django smoke each capture an unhandled exception
      from a single route.
- [ ] Breadcrumb buffer caps at `max_breadcrumbs`.
- [ ] Transport flushes on `atexit`; no event loss in tests.
- [ ] Source-map / release / environment honored in the envelope.

### Tests

- `packages/python-sdk/tests/test_init.py` — happy path
- `tests/integration/test_python_sdk_e2e.py` (in main test tree) —
  start a `retrace api serve` subprocess, exercise the SDK, assert a
  `qa_incident` lands

### Definition of Done

- All acceptance items checked
- New CI job: `python-sdk-tests` runs `pytest` inside
  `packages/python-sdk/`
- `docs/python-sdk.md` walkthrough
- README links it

---

## P0.3 — GitHub Actions templates

**Status:** DONE 2026-05-11 · **PR:** this PR · **Owner:** Claude

### Why now

Every roadmap item before this multiplies in value once users can drop
Retrace into their CI in 30 seconds. Today they have to read the CLI
docs and write their own workflow file.

### OSS to study

| Repo | URL | Pin | What to read |
|---|---|---|---|
| **getsentry/action-release** | https://github.com/getsentry/action-release | HEAD | `action.yml`, scripts under `scripts/`, source-map upload flow |
| **codecov/codecov-action** | https://github.com/codecov/codecov-action | HEAD | The "drop in, no setup needed" UX |
| **github/super-linter** | https://github.com/super-linter/super-linter | HEAD | How they handle the "we'll run lots of things" composite action |
| **qodo-ai/pr-agent** action | https://github.com/qodo-ai/pr-agent (`.github/workflows/pr-agent.yaml`) | HEAD | The exact PR-review-on-comment shape we want |

### Takeaways

- A single composite action vs. multiple narrower actions.
- The opinionated `secrets.RETRACE_*` naming.
- Running on `pull_request_target` vs `pull_request` for fork safety.

### Our improvement plan

Ship **three** templates under `.github/actions/` (composite actions
shipped in this repo, usable via `uses: txmed82/retrace/.github/actions/<name>@v1`):

1. `.github/actions/pr-review/action.yml` — runs
   `retrace review --pr ${{ github.event.number }} --post-comment --run-affected-tests`
   on `pull_request` events.
2. `.github/actions/source-map-upload/action.yml` — wraps
   `retrace api record-deploy --sha $GITHUB_SHA --source-map-dir <dir>`
   after a deploy.
3. `.github/actions/qa-auto/action.yml` — manual `workflow_dispatch`
   trigger that runs `retrace qa auto --repo <repo>` on a chosen
   incident id.

Plus documentation under `docs/github-actions.md` with paste-able
example workflows.

### Acceptance criteria

- [ ] A user adding `.github/workflows/retrace.yml` with 8 lines of
      YAML and `secrets.RETRACE_SDK_KEY` gets a PR comment on every
      PR.
- [ ] Source-map upload runs in < 30s on a typical Next.js build
      directory.
- [ ] `qa-auto` workflow respects `--id` input and falls back to
      "top open incident" when omitted.

### Tests

- `tests/test_github_actions_templates.py` — parses each `action.yml`,
  asserts inputs/outputs match what the docs claim.

### Definition of Done

- Three composite actions live in `.github/actions/`
- `docs/github-actions.md` shipped
- README links them
- A scratch repo successfully runs each on a synthetic PR

---

## P0.4 — Browser SDK breadcrumbs

**Status:** DONE 2026-05-11 · **PR:** this PR · **Owner:** Claude

### Why now

Replay capture is already strong, but when an error event lands
through the Sentry-compat path, we don't enrich it with the click /
fetch / console breadcrumbs Sentry users expect. This makes error
incidents shallower than they need to be.

### OSS to study

| Repo | URL | Pin | What to read |
|---|---|---|---|
| **getsentry/sentry-javascript** | https://github.com/getsentry/sentry-javascript | HEAD | `packages/browser/src/integrations/breadcrumbs.ts`, `packages/core/src/scope.ts`, `packages/browser/src/transports/fetch.ts`, `packages/browser/src/integrations/globalhandlers.ts` |
| **rrweb-io/rrweb** | https://github.com/rrweb-io/rrweb | HEAD | The custom-event plugin pattern (we already use this for click/network) |
| **datadog/browser-sdk** | https://github.com/DataDog/browser-sdk | HEAD | Different model — single SDK for rum + logs + errors. Skim for breadcrumb buffer impl. |

### Takeaways

- Console / network / click / navigation hooks (we already have most).
- Bounded ring buffer (Sentry caps at 100).
- Including breadcrumbs in error events automatically.

### Our improvement plan

1. **Add to `packages/browser/src/index.ts`:**
   - `addBreadcrumb(b: Breadcrumb): void`
   - Auto-capture history (pushState/popstate) navigation breadcrumbs
   - Ring buffer of 50 (configurable via `maxBreadcrumbs`)
2. **On error event (Sentry-compat path):** include the last 50
   breadcrumbs in the envelope so the server-side `monitoring_ingest`
   can promote them to `IncidentEvidence.console_excerpts` and
   `network_failures`.
3. **Update `monitoring_ingest.py`** to read those breadcrumbs and
   stuff them into the failure's `metadata` so the bridge picks them
   up.

### Acceptance criteria

- [ ] A click → network call → console error sequence produces an
      incident whose evidence has 3 ordered breadcrumb entries.
- [ ] Ring buffer respects `maxBreadcrumbs` cap.
- [ ] No breadcrumb is captured for masked elements
      (`maskTextSelector`).

### Tests

- `packages/browser/tests/breadcrumbs.test.ts` (we'll add a vitest
  setup if it doesn't exist).
- `tests/test_monitoring_ingest_breadcrumbs.py` — given a synthetic
  envelope with breadcrumbs, the qa_incident's evidence reflects them.

### Definition of Done

- All acceptance items checked
- SDK release version bump
- `README.md` Sentry section mentions the breadcrumb capture

---

# P1 — scale & breadth (weeks 2–3)

## P1.1 — Real-time alert fanout

**Status:** DONE 2026-05-12 · **PR:** #131 · **Owner:** Claude

### OSS to study

- **GlitchTip** (https://gitlab.com/glitchtip/glitchtip-backend) —
  Sentry-compat alerting. Files: `alerts/`, `events/views/`.
- **SigNoz** (https://github.com/SigNoz/signoz) — OTel alert manager.
- **Grafana/alerting** (https://github.com/grafana/grafana) —
  routing/grouping/silencing model.

### Plan

1. Extend `alert_rules.py` so a rule trip can fan out to:
   - Slack (existing webhook sink)
   - PagerDuty Events v2
   - Discord webhook
   - Generic JSON webhook
2. New CLI: `retrace monitor route add --rule <name>
   --target slack://… --severity high+`.
3. Add a small in-process scheduler (cron-style) that re-evaluates
   stateful alerts (e.g. "more than N errors in 5min").

### Acceptance

- [ ] An alert rule with `action=alert` + a Slack route posts a card
      on next rule trip.
- [ ] Deduplication: same fingerprint within 5 minutes doesn't
      double-post.

---

## P1.2 — Perceptual visual diff (Pillow + SSIM)

**Status:** DONE 2026-05-12 · **PR:** this PR · **Owner:** Claude

### Why

`tester baseline compare` uses sha256 today. Antialiasing /
sub-pixel rendering across hosts trips false positives. Need
perceptual diff with a configurable threshold.

### OSS to study

- **pyssim** (https://github.com/jterrace/pyssim) — small reference.
- **kornelski/dssim** (https://github.com/kornelski/dssim) — quality
  reference.
- **mapbox/pixelmatch** (https://github.com/mapbox/pixelmatch) — JS
  reference; algorithm doc is canonical.

### Plan

1. Add an optional `[image]` extra to `pyproject.toml` (Pillow + numpy).
2. New `visual_baseline.perceptual_diff(baseline, current, threshold=0.95)`
   — returns SSIM score + annotated diff PNG (red overlay where
   pixels differ).
3. `compare_run_to_baseline` uses perceptual diff when the extra is
   installed, falls back to sha256 otherwise.

### Acceptance

- [ ] A 1-pixel offset doesn't trip a diff under default threshold.
- [ ] A real layout shift does.
- [ ] Annotated diff PNG is the actual visual diff (not the raw new
      image).

---

## P1.3 — Diff-aware affected-test selection

**Status:** DONE 2026-05-12 · **PR:** #133 · **Owner:** Claude

### Why

`retrace review --run-affected-tests` runs `existing_tests` today. But
we have *route* data from PR review and *API spec* data from the
tester — we could intersect them and run only the API specs whose
routes the PR touches.

### OSS to study

- **nrwl/nx** (https://github.com/nrwl/nx) — affected-graph
  computation. Files: `packages/nx/src/command-line/affected/`.
- **bazelbuild/bazel** affected-target docs.

### Plan

1. `pr_review.affected_api_specs(analysis, store)` — given a
   PRReviewAnalysis, return the API spec ids whose URL routes
   intersect `analysis.affected_flows`.
2. `--run-affected-tests` extends to API specs.
3. CLI accepts `--include-api/--no-include-api`.

---

## P1.4 — API tester env profiles UI + request recording

**Status:** DONE 2026-05-12 · **PRs:** #134 (contract-diff `retrace tester api-diff`) + this PR (env-profile management CLI + HAR-import recorder). Browser-based request-builder UI deferred to a polish PR once we have signal from real users. · **Owner:** Claude

### OSS to study

- **usebruno/bruno** (https://github.com/usebruno/bruno) — files: `packages/bruno-app/src/components/Environments/`, `packages/bruno-cli/src/commands/run.js`
- **Orange-OpenSource/hurl** (https://github.com/Orange-OpenSource/hurl) — capture/replay flow.
- **schemathesis** for OpenAPI fuzz inspiration.

### Plan

1. Local UI: env profile editor (under Tester tab).
2. `retrace tester record --url <…>` — opens the local UI, lets you
   click around an API in a request builder, saves matching specs.
3. Contract diff: `retrace tester api-diff --new openapi.yaml
   --old openapi.prev.yaml` — emits `qa_incidents` for breaking
   changes.

---

## P1.5 — Postgres adapter

**Status:** DONE 2026-05-12 · **PRs:** #135 (foundation) + this PR (full chassis — real `PostgresBackend.connect()` via psycopg, SQL dialect translation at execute time, portable SCHEMA, CI Postgres service, end-to-end smoke tests). Per-table polish in future PRs as edge cases surface. · **Owner:** Claude

### Why

SQLite is fine to ~100k events/day. Past that, contention bites. Many
self-host teams will want Postgres.

### OSS to study

- **getsentry/sentry** — for "this works at scale" reference.
- **dagster-io/dagster** — multi-backend storage abstraction. Files:
  `python_modules/dagster/dagster/_core/storage/`.
- **alembic** for migrations.

### Plan

1. Abstract `storage.py` behind a `Backend` protocol (today's class
   becomes `SqliteBackend`).
2. New `PostgresBackend` with the same surface.
3. Alembic migrations replace the inline `init_schema` migrations.
4. CI runs the full suite against both backends.

### DO NOT
- Don't refactor `storage.py` in one giant PR. Slice by table
  (failures, qa_incidents, replay_*).

---

# P2 — long-tail polish (month 2+)

## P2.1 — GitLab + Bitbucket integration

**Status:** DEFERRED — GitHub-only at launch. Revisit once there's a real
user on GitLab or Bitbucket; not worth the ~1 week of provider-abstraction
work speculatively. · **ETA when revived:** 3 days each (~1 week total)

### OSS to study

- **gitlab.com/gitlab-org/gitlab** API docs (no need to clone).
- **bitbucket-website-builds** Atlassian SDK reference.
- **renovatebot/renovate** (https://github.com/renovatebot/renovate)
  — uses ALL THREE providers. Files: `lib/modules/platform/{github,gitlab,bitbucket}/`. This is the canonical reference for a
  platform-abstracted PR tool.

### Plan

1. Mirror `github_app.py` shape into `gitlab_app.py` and
   `bitbucket_app.py`.
2. Abstract the common "post-comment / draft-PR / fetch-diff"
   operations into a `PullRequestProvider` protocol.
3. `auto_fix.propose_fix_for_incident` accepts any provider.

---

## P2.2 — Rate limiting + sampling on ingest

**Status:** NOT STARTED · **ETA:** 2 days

### OSS to study

- **envoyproxy/envoy** ratelimit docs (no clone).
- **github.com/redis/redis-py-cluster** token-bucket reference.
- **GlitchTip** sampling implementation.

### Plan

1. Per-SDK-key token bucket in SQLite (`sdk_key_rate_limits`).
2. `Retry-After` header on `/api/sdk/error` and `/api/sdk/replay`
   when exceeded.
3. `--sample-rate` on `@retrace/browser` `init()`.

---

## P2.3 — Retention + backups

**Status:** DONE 2026-05-12 · **PR:** this PR · **Owner:** Claude

### Plan

1. `retrace data retention apply` — purges replay batches + failures
   older than N days (configurable per project).
2. `retrace data backup --to <path>` — sqlite + data dir tarball.
3. Cron in Docker Compose. *(left for ops docs — the CLI is the
   primitive; how a self-host operator schedules it is their call.)*

### What shipped

- `src/retrace/retention.py` — `RetentionPolicy` + `apply_retention()`
  orchestrator. Touches the app-error domain via the existing
  `Storage.prune_app_error_retention()` (per project+env pair), plus
  two new global helpers `Storage.prune_replay_batches()` and
  `Storage.prune_otel_events()`. Filesystem sweep removes per-run
  directories under `ui-tests/runs/` and `api-tests/runs/`; specs,
  baselines, queues are NEVER touched.
- `src/retrace/backup.py` — `create_backup()` writes a `.tar.gz` of
  a consistent sqlite snapshot (via the online `BACKUP` API, not raw
  bytes — survives concurrent writes) plus the `data_dir` contents.
  Postgres backups deferred (use `pg_dump` against the DSN).
- `src/retrace/config.py` — new `RetentionConfig` block.
- `src/retrace/commands/data.py` — `retrace data retention apply [--dry-run]`
  and `retrace data backup --to PATH`.
- 26 new tests across `tests/test_retention.py`, `tests/test_backup.py`,
  `tests/test_data_cli.py` (dry-run / sweep / round-trip /
  CLI happy + error paths).

### Per-project retention is NOT modeled yet

The roadmap mentions "configurable per project" but the schema
doesn't carry per-project retention columns. Today the policy is
install-global via the `retention:` block in `config.yaml`. Adding
`project_retention_policies` is a separate slice — wait for a real
multi-project user to ask before paying that complexity.

---

## P2.4 — Multi-tenancy + audit log

**Status:** NOT STARTED · **ETA:** 7 days

### OSS to study

- **gravitational/teleport** (https://github.com/gravitational/teleport) — audit log + RBAC reference. Files: `lib/auth/`, `lib/audit/`.
- **Authentik** (https://github.com/goauthentik/authentik) — OSS auth.

### Plan

1. User table + per-project memberships.
2. Roles: viewer, editor, admin.
3. Audit log of state-changing operations.
4. Optional OAuth (GitHub, Google) via PKCE.

---

# How to claim and execute a roadmap item

```bash
# 1. Pick an item that's NOT STARTED, set its status to IN PROGRESS
#    in this file, with your name and the date.
$EDITOR docs/roadmap.md
git checkout -b chore/claim-<item-id>
git commit -am "claim <item-id>"
git push -u origin chore/claim-<item-id> && gh pr create

# 2. Open the implementation branch.
git fetch origin master
git checkout -b feat/<item-id>-<slug> origin/master

# 3. Clone OSS comparables and write study notes.
mkdir -p external
cd external && git clone --depth 1 https://github.com/<…>.git
cd .. && $EDITOR docs/study-notes/<repo>.md

# 4. Implement, test, lint.
uv pip install -e ".[dev]"
uv run pytest -q
uv run ruff check src tests

# 5. PR.
git add -A && git commit -m "<item-id>: <short title>"
git push -u origin feat/<item-id>-<slug>
gh pr create --title "<item-id>: <short title>" --body-file - <<'EOF'
## Summary
<…>

## OSS studied
- <repo>@<sha> — `docs/study-notes/<repo>.md`

## Acceptance criteria
- [x] <…>
- [x] <…>

## Test plan
- [x] new tests in tests/<…>
- [x] full suite green
- [x] ruff clean
EOF

# 6. Address CI + CodeRabbit. Merge when clean. Update this doc.
```

---

# Update log

| Date | Change | PR |
|---|---|---|
| 2026-05-11 | Roadmap doc created. P0–P2 items defined. Snapshot taken after PR #124. | #125 |
| 2026-05-11 | P0.1 LLM-powered PR review shipped: `retrace.llm_pr_review.llm_review`, `retrace review --llm/--no-llm`, PII-redacted diff, token-cap bail, file-aware chunking, in-process cache, structured `LLMReviewResult` with summary/walkthrough/inline suggestions/risk notes. 18 new tests. | #126 |
| 2026-05-11 | P0.1 follow-up: review-quality guardrails — line-validity filter, deterministic suggestion + risk caps, optional `--llm-self-critique`, prior-review memory via new `llm_pr_reviews` table. | #127 |
| 2026-05-11 | P0.2 Python SDK + FastAPI / Flask / Django / logging integrations. Stdlib-only runtime, background-thread transport with bounded queue + atexit flush. 53 tests + 1 e2e through the real Sentry-compat ingest. | #128 |
| 2026-05-11 | P0.3 GitHub Actions composite templates — `pr-review`, `source-map-upload` (curl + jq, dep-free), `qa-auto`. Contract pinned with 14 tests. | #129 |
| 2026-05-11 | P0.4 Browser SDK breadcrumbs — 50-entry ring buffer (Sentry-shape), `addBreadcrumb` public API, auto-capture for click/console/http/navigation/error, exception events carry the trail. Server-side `monitoring_ingest` promotes console + failed-HTTP breadcrumbs to `IncidentEvidence` and persists the raw trail on `failure.metadata.breadcrumbs`. | #130 |
| 2026-05-12 | P1.2 Perceptual visual diff — new `[image]` extra (Pillow + numpy); `visual_perceptual.perceptual_diff(...)` runs single-window SSIM with annotated red-overlay diff PNG. `compare_run_to_baseline(mode="auto", threshold=0.95)` uses perceptual when extra installed, falls back to sha256 otherwise. `retrace tester baseline compare --mode --threshold` CLI flags. | #132 |
| 2026-05-12 | P1.1 Real-time alert fan-out — `alert_dispatch.dispatch_alert()` posts fired alerts to Slack / Discord / PagerDuty Events v2 / generic webhook targets. New `alert_routes` + `alert_dispatches` tables; per-route severity floor + dedup window. `retrace monitor route add/list/delete/test` CLI. Wired into `ingest_monitoring_webhook` as a best-effort tail step. 13 new tests. | #131 |
| 2026-05-12 | P1.3 Diff-aware affected-test selection — new `pr_review.affected_api_specs(analysis, specs_dir)` intersects API spec URL paths with the PR's affected flows under an equality + strict-prefix matching rule (`/api/login` does NOT match `/api/login-history`). `retrace review --run-affected-tests` extended with `--include-api/--no-include-api`; JSON output + PR comment body carry `affected_api_test_results`. 11 new tests. | #133 |
| 2026-05-12 | P1.4 (partial) OpenAPI contract diff — new `retrace tester api-diff --new --old` emits breaking-vs-safe contract changes across two OpenAPI / Swagger documents (operation removed, required-request-field added, response-schema field removed, success-status removed, enum-value removed). Each breaking change files a `qa_incident` (`--no-file-incidents` to opt out). One-level `$ref` resolution; deterministic ordering. 24 new tests. Env-profile UI + `tester record` deferred. | #134 |
| 2026-05-12 | P1.5 (foundation) Postgres adapter chassis — new `retrace.storage_backend` module with `Backend` Protocol + `SqliteBackend` + `PostgresBackend` stub + URL factory. `Storage(...)` accepts `sqlite:///`, bare paths, and rejects `postgresql://` with a clean `NotImplementedError` pointing at this slice. `[postgres]` extra reserves `psycopg[binary]>=3.2`. Per-table migration ordering documented for follow-up PRs. 27 new tests; existing Storage behavior unchanged. | #135 |
| 2026-05-12 | P1.5 (full) Postgres adapter — real `PostgresBackend.connect()` via psycopg3, SQL dialect translation at execute time (`?` → `%s`, `datetime('now', ?)` → ISO-text `to_char(now() + interval)`, `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`), portable SCHEMA via `sql_schema.translate_schema` (`AUTOINCREMENT` → `BIGSERIAL`, ISO-text defaults). `Storage(postgresql://...)` works end-to-end. CI Postgres service container runs gated smoke tests covering `alert_routes`/`alert_dispatches`/`llm_pr_reviews` round-trips. 18 new tests (translation + WrappedConnection + schema translator + PG smoke). | #136 |
| 2026-05-12 | P1.4 (finish) env-profile management CLI + HAR import recorder — new `retrace tester env list/show/yaml` (read-only against `config.yaml`; emits paste-ready stanzas, never overwrites the hand-edited file). New `retrace tester record --har` ingests a browser DevTools HAR export into one APITestSpec per matching request (sensitive headers stripped, JSON bodies structured, `--include-host`/`--include-method`/`--exclude-path` filters). P2.1 GitLab/Bitbucket deferred — GitHub-only at launch. 41 new tests. | #137 |
| 2026-05-12 | P2.3 Retention + backups — `retrace data retention apply [--dry-run]` purges old rows across the app-error domain (per project+env via existing `prune_app_error_retention`) + globally for `replay_batches` and `otel_events`, plus a filesystem sweep that removes old `ui-tests/runs/` and `api-tests/runs/` subdirectories (specs / baselines / queues untouched). `retrace data backup --to PATH` writes a `.tar.gz` of a consistent sqlite snapshot (via the online BACKUP API) plus the `data_dir` contents. New `RetentionConfig` in `config.yaml`. 26 new tests. | this PR |

> Append a row whenever an item changes status or a new item is
> added. Keep newest at the bottom.
