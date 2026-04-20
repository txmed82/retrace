# Retrace

Your real users are your QA team. Retrace finds the bugs they hit.

Retrace pulls user session recordings from PostHog, runs eight heuristic detectors over rrweb events, clusters similar sessions, sends each cluster to an OpenAI-compatible LLM for human-readable explanation, and writes a dated markdown bug report.

## Install (dev)

Requires Python 3.11+.

```bash
uv venv
uv pip install -e ".[dev]"
```

## Set up interactively

```bash
retrace init
```

Prompts for PostHog creds, LLM endpoint, cadence, output paths. Live-validates connections before writing. Produces `config.yaml` + `.env`.

Then run:

```bash
retrace run
```

Read the report at `./reports/YYYY-MM-DD-HHMMSS.md`.

## Keep it running on a cron

```bash
docker compose up -d
```

Respects `RETRACE_CRON` env var (default every 6 hours). Mounts your `config.yaml`, `.env`, and writes `data/` + `reports/` to the host.

## Health check

```bash
retrace doctor
```

Re-runs all the validations `init` did. Exits non-zero if anything is broken.

## Detectors (v0.1)

| Detector | What it catches |
|---|---|
| `console_error` | `console.error` or uncaught exceptions |
| `network_4xx` | 4xx HTTP responses (ignores 401 noise) |
| `network_5xx` | 5xx server errors |
| `rage_click` | 3+ rapid clicks on the same target |
| `dead_click` | Click with no DOM mutation or network followup |
| `error_toast` | DOM nodes with `role=alert`, toast/error classes, or error-like text |
| `blank_render` | URL dwelt ≥2s with very few DOM nodes |
| `session_abandon_on_error` | Session ends within 5s of any of the above |

Toggle any via `config.yaml`.

## How it works

```
PostHog API → Ingester → Detectors → Clusterer → LLM Analyst → Markdown Sink
```

- **Ingester** follows PostHog's pagination, stores rrweb events atomically to disk + metadata to SQLite.
- **Detectors** emit structured signals. No LLM at this stage.
- **Clusterer** groups sessions sharing the same signal fingerprint (detector set + URL path + primary error message) so "100 users hit the same toast" becomes one finding with `affected_count: 100`.
- **LLM Analyst** takes one representative session per cluster and returns a structured bug report.
- **Sink** writes the report grouped by severity. One file per run.

## Design docs

- `docs/superpowers/specs/2026-04-19-retrace-design.md` — product spec
- `docs/superpowers/plans/2026-04-19-retrace-plan-a-vertical-slice.md` — Plan A (v0.1-alpha)
- `docs/superpowers/plans/2026-04-20-retrace-plan-b-polish-and-breadth.md` — Plan B (v0.1)
