# Retrace — Design Spec (v0.1)

**Status:** Draft
**Date:** 2026-04-19
**Author:** Colin

## What it is

Retrace is an open-source, self-hosted CLI that turns real user session recordings into a markdown bug report. It pulls session replays from PostHog on a cron, runs cheap heuristic detectors over rrweb events to find sessions that look broken, sends flagged sessions to a user-configured LLM (local via llama.cpp by default) for human-readable explanation and clustering, and writes the findings to a dated markdown file that links back to PostHog for the full replay.

The pitch: **your real users are your QA team. Retrace finds the bugs they hit.**

## Who it's for

"Vibe developers" — solo builders and small teams shipping with AI assistance who don't have QA engineers and for whom enterprise UI testing (QA Wolf, Bug0) is out of reach. The tool should feel as easy to stand up as `supabase init` or `stripe login`.

## v0.1 scope

**In:**
- CLI tool (Python, installed via `uv` / `pipx`, distributed as a Docker Compose stack for cron use)
- PostHog Cloud and self-hosted PostHog as the only session source
- OpenAI-compatible LLM interface (llama.cpp, ollama, LM Studio, OpenAI, or anything else speaking the spec)
- Heuristic-first pipeline: cheap detectors scan every session, LLM only processes sessions with ≥1 signal
- Output: dated markdown files grouped by severity, with PostHog replay URLs
- Interactive `retrace init` setup with live connection validation
- `retrace run` for one-shot execution (cron-driven)
- `retrace doctor` to re-validate config

**Out (future work, designed for but not built):**
- Pluggable sinks (Slack, GitHub Issues, Linear, Jira) — architecture supports it, no adapters ship in v0.1
- Web dashboard / hosted version
- Codebase-aware bug-fix prompts (Coderabbit-style suggestion mode)
- Non-PostHog session sources (direct rrweb SDK, Sentry replays, LogRocket)
- Event-triggered / webhook mode (v0.1 is cron-only)
- Aggregate funnel/drop-off insights (separate subsystem, separate spec)

## Architecture

Single-process pipeline, each stage a pure function over the previous stage's output:

```
PostHog API ──► Ingester ──► Signal Extractor ──► Clusterer ──► LLM Analyst ──► Sink
                   │              │                    │              │            │
                   ▼              ▼                    ▼              ▼            ▼
              raw session    {session_id,          grouped by    structured     markdown
              events JSON    signals: [...]}       fingerprint   findings JSON  report
```

Each stage reads from and writes to local SQLite + flat files, so any stage can be replayed against stored data without re-fetching from PostHog.

### 1. Ingester

- Calls `GET /api/projects/{project_id}/session_recordings` with `?date_from=<last_cursor>` since the last successful run
- For each new recording, fetches the event snapshots via `/api/projects/{project_id}/session_recordings/{id}/snapshots`
- Stores raw snapshot JSON at `data/sessions/<session_id>.json`, metadata row in SQLite (`sessions` table)
- Respects `max_sessions_per_run` — newest first, drops the rest (with a warning)
- Persists `last_run_cursor` so reruns don't duplicate

### 2. Signal Extractor

Pure functions over rrweb event streams. Each detector returns zero or more `Signal` records:

```python
@dataclass
class Signal:
    session_id: str
    detector: str        # "console_error", "rage_click", etc.
    timestamp_ms: int    # offset from session start
    url: str             # URL at time of signal
    details: dict        # detector-specific payload
```

**Detectors shipping in v0.1:**

| Detector | What it catches |
|---|---|
| `console_error` | `console.error` / uncaught exceptions in rrweb's `6` (plugin) events |
| `network_4xx` | XHR/fetch requests with status 400-499 |
| `network_5xx` | XHR/fetch requests with status 500-599 |
| `rage_click` | ≥3 clicks within 1s on same target coordinates |
| `dead_click` | Click followed by no DOM mutation AND no network request within 2s |
| `error_toast` | New DOM node with `role=alert`, or class matching `/toast\|snackbar\|error\|alert/i`, or text matching error regex |
| `blank_render` | URL change + ≥2s elapsed + page has <10 visible DOM nodes |
| `session_abandon_on_error` | Session ends within 5s of any above signal |

Detectors are individual modules under `retrace/detectors/*.py`, registered via a simple registry. Each can be toggled off in config. Writing a new detector is "add a file, register it."

### 3. Clusterer

Groups sessions by fingerprint to avoid "same bug reported 47 times." Fingerprint is a tuple of:
- Signal detector types (sorted, deduped)
- Normalized URL (path only, query stripped)
- Primary error message (if present, truncated to 200 chars)

Output: `Cluster` records with `{fingerprint, session_ids[], signal_summary, first_seen, last_seen, affected_count}`.

Clusters with `affected_count >= min_cluster_size` (default 1) proceed to the LLM stage. Solo-occurrence clusters may be demoted to an "unclustered" section in the report.

### 4. LLM Analyst

For each cluster, builds a prompt containing:
- Cluster summary (which detectors fired, affected session count, time range)
- Representative session: sequence of user actions (clicks, inputs, navigations) leading up to the signal
- DOM text snapshot at the error moment (truncated to fit context window)
- Network activity near the error

Prompts the LLM to return structured JSON:

```json
{
  "title": "Sign-up fails with 'Email already exists' for new users",
  "severity": "high",               // critical | high | medium | low
  "category": "functional_error",   // functional_error | visual_bug | performance | confusion
  "what_happened": "...",
  "likely_cause": "...",
  "reproduction_steps": ["...", "..."],
  "confidence": "high"              // high | medium | low
}
```

**LLM interface:** OpenAI-compatible Chat Completions (`/v1/chat/completions`). This covers llama.cpp's server, ollama, LM Studio, vLLM, OpenAI directly, and Anthropic/Gemini/anything else via LiteLLM as a proxy. Config fields: `llm.base_url`, `llm.model`, `llm.api_key` (optional — llama.cpp doesn't require one).

JSON-mode / structured output is preferred when the backend supports it, falling back to prompted JSON + lenient parsing (strip code fences, retry once on malformed).

Retry policy: 2 retries on timeout / 5xx, exponential backoff. On repeated failure, the cluster is written to the report with detector signals only and a `[LLM unavailable]` note — the pipeline never silently drops findings.

### 5. Sink

Interface (future-proofing for Slack/GitHub/Linear):

```python
class Sink(Protocol):
    def write(self, run: Run, findings: list[Finding]) -> None: ...
```

v0.1 ships one implementation: `MarkdownSink`. Writes `reports/<YYYY-MM-DD-HHMM>.md`:

```markdown
# Retrace report — 2026-04-19 14:00

Scanned 47 sessions from 2026-04-19 08:00 to 14:00.
Flagged 12 sessions across 4 clusters.

## 🔴 Critical

### Sign-up fails with "Email already exists" for new users
- **Affected:** 8 users
- **First seen:** 09:12
- **URL:** /signup
- **What happened:** ...
- **Reproduction:** ...
- **Sessions:** [replay 1](posthog-url), [replay 2](posthog-url), ...

## 🟠 High
...
```

## Config & init UX

**Two files:**
- `config.yaml` — non-secret, commit-safe (PostHog host, project ID, LLM URL/model, cron window, detector toggles, output dir)
- `.env` — secrets (PostHog API key, LLM API key if any), gitignored by default

**`retrace init` flow:**

1. Prompt for PostHog host (default `https://us.i.posthog.com`), project ID, personal API key
2. Live-test: `GET /api/projects/{id}/` — confirm auth, show session count in last 24h
3. Prompt for LLM backend type (numbered menu: local / OpenAI / Anthropic / custom)
4. Prompt for base URL + model + optional API key (defaults vary by choice)
5. Live-test: one-shot chat completion with a trivial prompt, confirm response
6. Prompt for cron cadence (default 6h), max sessions per run (50), output directory (`./reports`)
7. Write `config.yaml` and `.env`, offer to run `retrace run` immediately as smoke test

**`retrace doctor`** re-runs the same validations against the persisted config — useful for debugging cron failures.

## Data layout

```
./
├── config.yaml
├── .env                       # gitignored
├── data/
│   ├── retrace.db             # sqlite: sessions, signals, clusters, runs
│   └── sessions/
│       └── <session_id>.json  # raw rrweb events, cached for replay/reanalysis
└── reports/
    └── 2026-04-19-1400.md
```

SQLite schema (sketch):

- `sessions(id, project_id, started_at, duration_ms, user_id, event_count, fetched_at)`
- `signals(id, session_id, detector, timestamp_ms, url, details_json)`
- `clusters(id, run_id, fingerprint_hash, signal_summary_json, affected_count, first_seen, last_seen)`
- `cluster_sessions(cluster_id, session_id)`
- `findings(id, cluster_id, title, severity, category, body_json, llm_model, confidence)`
- `runs(id, started_at, finished_at, sessions_scanned, clusters_found, status, error)`

## Deployment

**Local dev:** `uv tool install retrace` → `retrace init` → `retrace run`. No Docker required.

**Cron deployment:** `docker compose up -d` spins up one container that holds the CLI plus a `crond` running the user's chosen cadence. SQLite and reports mounted as volumes. A `docker-compose.yml` ships in the repo; `retrace init` generates the `.env` it reads.

Example `docker-compose.yml` (ships in repo):

```yaml
services:
  retrace:
    image: retrace:latest
    environment:
      - RETRACE_CRON=0 */6 * * *
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./.env:/app/.env:ro
      - ./data:/app/data
      - ./reports:/app/reports
```

Users wanting to point Retrace at their existing `llama.cpp` running on the host use `host.docker.internal` as the LLM base URL (documented in README).

## Module layout (Python)

```
retrace/
├── __main__.py              # CLI entrypoint (click or typer)
├── commands/
│   ├── init.py              # interactive setup
│   ├── run.py               # the pipeline
│   └── doctor.py            # validation
├── config.py                # config.yaml + .env loader, Pydantic models
├── ingester.py              # PostHog client + session fetcher
├── detectors/               # one file per detector + __init__ registry
│   ├── base.py              # Signal dataclass, Detector protocol
│   ├── console_error.py
│   ├── network_4xx.py
│   ├── rage_click.py
│   ├── dead_click.py
│   ├── error_toast.py
│   ├── blank_render.py
│   └── session_abandon.py
├── clusterer.py
├── llm/
│   ├── client.py            # OpenAI-compatible wrapper
│   └── prompt.py            # cluster → prompt, response → Finding
├── sinks/
│   ├── base.py              # Sink protocol
│   └── markdown.py
└── storage.py               # SQLite DAL
```

Stage boundaries are enforced by the dataclasses passing between them — each stage can be unit-tested in isolation with fixture JSON.

## Testing

- **Unit tests per detector** — each detector gets a small fixture rrweb event stream with known signals and asserts extraction. These are the highest-value tests: detector correctness is the whole product.
- **Integration test for pipeline** — one fixture session + fake LLM (returns canned JSON) → assert the markdown file matches golden output.
- **Live smoke test** via `retrace doctor` (not a test suite, but exercises the real integrations).

## Open questions (tracked for v0.2+, non-blocking)

- Vision model stage for "blank render" and "layout broken" cases where DOM heuristics miss the bug — needs video/screenshot rendering of rrweb, which is a meaningful engineering lift.
- Cost caps / token budgeting when users point at hosted LLMs.
- How to handle sessions that span multiple pages with errors on each — currently each signal stands alone; clustering may need to become session-aware.
- De-duplication across runs — if the same cluster appears in 3 consecutive reports, should it be suppressed or marked "ongoing"?

## Non-goals

- Replacing PostHog's existing rage-click / dead-click views. Those are fine; Retrace's value is **clustering + LLM explanation + actionable bug reports**, not the raw detection.
- Real-time alerting. Cron-only in v0.1.
- Recording sessions itself. PostHog (or a future rrweb SDK adapter) is always the source.
