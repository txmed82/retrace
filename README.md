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
- Save settings to `config.yaml` + `.env`
- Run system checks for:
  - PostHog connectivity
  - `gh` installed/authenticated
- Copy suggested terminal commands when `gh` is missing/not authed
- Browse findings from latest report
- Replay stored rrweb events
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
- `retrace github ...` — repo metadata management
- `retrace suggest-fixes ...` — candidate matching + prompt generation

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

## Runtime Data

- `config.yaml` — non-secret config
- `.env` — secrets (`RETRACE_POSTHOG_API_KEY`, optional `RETRACE_LLM_API_KEY`)
- `data/retrace.db` — run/session/findings metadata
- `data/sessions/*.json` — ingested rrweb events
- `reports/*.md` — findings reports
- `reports/fix-prompts/*` — generated fix artifacts

## Cron / Background Execution

```bash
docker compose up -d
```

Uses `RETRACE_CRON` (default every 6 hours).

## Design Docs

- `docs/superpowers/specs/2026-04-19-retrace-design.md`
- `docs/superpowers/plans/2026-04-19-retrace-plan-a-vertical-slice.md`
