# Retrace

Your real users are your QA team. Retrace finds the bugs they hit.

## What it does

Pulls user session recordings from PostHog, runs lightweight heuristic detectors over the rrweb events (console errors, 5xx responses, rage clicks), sends flagged sessions to an OpenAI-compatible LLM (llama.cpp, ollama, LM Studio, OpenAI, etc.), and writes a dated markdown bug report you can paste into Slack or a PR.

This is **v0.1-alpha** — a focused vertical slice. The full roadmap (clustering, `retrace init` wizard, `retrace doctor`, Docker Compose, more detectors, notification sinks) is in `docs/superpowers/specs/2026-04-19-retrace-design.md`.

## Install

Requires Python 3.11+.

```bash
uv tool install retrace
# or for development:
uv venv && uv pip install -e ".[dev]"
```

## Setup

1. Copy the config templates:
   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```
2. Fill in your PostHog host, project ID, and a personal API key (`phx_...`) in `.env` and `config.yaml`.
3. Point `llm.base_url` at a running OpenAI-compatible server. For llama.cpp:
   ```bash
   llama-server -m ./models/llama-3.1-8b-instruct.gguf --host 0.0.0.0 --port 8080
   ```
4. Run it:
   ```bash
   retrace run
   ```
5. Read the report at `./reports/YYYY-MM-DD-HHMMSS.md`.

## Config

See `config.example.yaml` for the full shape. Key knobs:

- `run.lookback_hours` — how far back the first run goes (subsequent runs pick up from the last cursor).
- `run.max_sessions_per_run` — per-run cap. If hit, the cursor rewinds to the oldest processed session so the next run picks up the remainder.
- `detectors.*` — toggle individual detectors on/off.
- `llm.base_url` / `llm.model` / `llm.api_key` — point at any OpenAI-compatible endpoint.

## How it works

```
PostHog API ──► Ingester ──► Detectors ──► LLM Analyst ──► Markdown Sink
```

- **Ingester** — pulls new recordings since the last cursor, stores raw rrweb events atomically to `data/sessions/<sid>.json` and metadata to SQLite.
- **Detectors** — `console_error`, `network_5xx`, `rage_click` scan events and produce signals. No LLM involvement at this stage.
- **LLM Analyst** — only sessions with ≥1 signal reach the LLM. It sees a windowed summary of user actions around the issue and the signal payloads, and returns a structured `Finding` (title, severity, what happened, reproduction steps).
- **Sink** — groups findings by severity and writes a dated markdown report with deep links back to the PostHog replay for each session.

On each run, per-session errors are isolated (a bad session doesn't kill the batch), and the run's status is recorded in the SQLite `runs` table.

## What's not in v0.1-alpha

All tracked for Plan B:

- Clustering (one finding per flagged session for now)
- `retrace init` interactive wizard — edit `config.yaml` by hand
- `retrace doctor` for config/connectivity validation
- Docker Compose + cron-in-container deployment
- 5 more detectors (`network_4xx`, `dead_click`, `error_toast`, `blank_render`, `session_abandon_on_error`)

## Design docs

- `docs/superpowers/specs/2026-04-19-retrace-design.md` — full product spec
- `docs/superpowers/plans/2026-04-19-retrace-plan-a-vertical-slice.md` — the implementation plan this repo was built from
