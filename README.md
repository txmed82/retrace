<p align="center">
  <img src="assets/retrace-banner.svg" alt="Retrace banner" width="100%" />
</p>

# Retrace

Your real users are your QA team. Retrace finds the bugs they hit.

Retrace pulls PostHog session recordings, detects likely breakage with heuristic detectors, clusters similar failures, generates clear bug summaries, and outputs actionable fix prompts with likely culprit files.

## What You Get

- Session-level bug detection from rrweb data
- Clustering so repeated user failures become one issue
- LLM-written summaries and repro context
- Local UI with rrweb replay, culprit files, and copyable prompts
- GitHub repo matching via CLI-connected repo metadata
- Local Browser Harness UI tester with saved reusable specs
- Regression-state tracking for replay findings (`new`, `ongoing`, `regressed`, `resolved`)

## Quickstart

Requires Python 3.11+.

```bash
uv venv
uv pip install -e ".[dev]"
```

Set up and run:

```bash
retrace init
retrace run
```

Report output:

- `./reports/YYYY-MM-DD-HHMMSS.md`

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
- Copy suggested terminal commands when `gh` is missing/not authed
- Browse findings from latest report
- Replay stored rrweb events
- Inspect first-party replay sessions and replay-backed issues
- Process queued first-party replay batches into signals and issues
- Inspect likely culprit files and copy Codex/Claude prompts

## Fix Suggestions Workflow

1. Connect repo metadata (CLI):

```bash
retrace github connect --repo <org/name> --branch main --local-path /path/to/repo
```

2. Generate fix suggestions from latest report:

```bash
retrace suggest-fixes --latest --repo <org/name> --out ./reports/fix-prompts
```

Artifacts:

- `reports/fix-prompts/*.json`
- `reports/fix-prompts/*.codex.md`
- `reports/fix-prompts/*.claude.md`

## Core Commands

- `retrace init` — interactive setup + validation
- `retrace doctor` — health checks for config/services
- `retrace run` — one-shot ingestion, detection, clustering, report write
- `retrace ui` — local browser UI and onboarding/settings
- `retrace tester ...` — describe tests or generate suite drafts with Browser Harness
- `retrace mcp serve` — single MCP server with multiple tools (findings + tester)
- `retrace github ...` — repo metadata management
- `retrace suggest-fixes ...` — candidate matching + prompt generation

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

## First-Party Replay API

Retrace can ingest browser SDK replays directly and process them into replay-backed issues.

Create an SDK key:

```bash
retrace api create-sdk-key --project Web --environment production
```

Run the ingest/read API:

```bash
retrace api serve
```

Browser SDK ingest endpoint:

- `POST /api/sdk/replay`

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

- `docs/superpowers/specs/2026-04-19-retrace-design.md`
- `docs/superpowers/plans/2026-04-19-retrace-plan-a-vertical-slice.md`
