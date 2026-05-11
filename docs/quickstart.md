# Retrace — 5-minute quickstart

This page is the deep version of the README's quickstart. If you landed
here from HN, an indiehackers post, or a tweet — start here.

## What Retrace is

Retrace finds the bugs your real users hit and ships fixes for them.

It's one product across five signal classes, all converging on a single
`Incident` queue:

| Pillar           | Catches                                              |
|------------------|------------------------------------------------------|
| **Replay**       | Frontend bugs from real user sessions (rrweb).       |
| **UI testing**   | Failures from AI-driven Browser Harness specs.       |
| **API testing**  | HTTP contract test failures.                         |
| **Error monitor**| Sentry-compatible DSN + OpenTelemetry ingest.        |
| **PR review**    | Diffs that touch a flaky surface or skip coverage.   |

One CLI then drives the end-to-end loop:

```bash
retrace qa auto --repo your-org/your-app
```

→ picks the top incident → auto-generates a UI test that reproduces it →
opens a draft PR with the fix prompt and suspected files.

## Install

```bash
uv pip install -e ".[dev]"   # or: pip install retrace (when published)
```

## Bootstrap

```bash
retrace quickstart
```

One command:

- Writes a minimal `config.yaml` + `.env` in the current directory.
- Initializes a local SQLite store under `./data/retrace.db`.
- Mints a browser-safe SDK key.
- Prints two copy-paste snippets:
  - rrweb replay capture (`<script type="module">`)
  - Sentry-compatible DSN (`Sentry.init({ dsn: "…" })`)

Both point at the same workspace, so events from either path land in
the same incident queue.

> **TLS**: `retrace quickstart --api-scheme https --api-host
> retrace.example.com --api-port 443` for hosted deployments. The
> browser will reject `http://` DSNs from an HTTPS page.

## See the product

The fastest way to see all five pillars at once:

```bash
retrace demo all
retrace qa list
```

`demo all` seeds one incident per pillar (replay, UI test, API test,
error monitor, PR review). `qa list` shows the unified queue —
priority-ordered, one row per incident, source-tagged.

## Run the killer demo

```bash
# (1) Connect a real repo
retrace github connect --repo your-org/your-app --local-path /path/to/checkout

# (2) Start the API + UI
retrace api serve --host 127.0.0.1 --port 8788 &
retrace ui &     # http://127.0.0.1:8787

# (3) Run the loop
retrace qa auto --repo your-org/your-app
```

This:

1. Picks the highest-priority open incident.
2. Auto-generates a UI test from the incident's reproduction recipe and
   runs it via Browser Harness (or the native runner).
3. If the bug reproduces, scores the repo, builds a fix prompt, opens
   a `git worktree`, applies optional agent changes, and creates a
   draft PR via `gh`.

You can run any step on its own: `retrace qa reproduce <INC-…>`,
`retrace qa fix <INC-…> --repo <…>`, `retrace qa show <INC-…>`.

## What to wire up next

- **PostHog import** (existing replay data):
  `retrace api import-posthog-replays --since-hours 24`
- **GitHub App** (PR review without the CLI):
  see [`docs/github-app.md`](github-app.md)
- **Issue sinks** (Jira / Linear / GH Issues):
  `retrace api promote-issue <INC-…> --sink github --repo <org/repo>`
- **Daily digest** (markdown rollup):
  `retrace digest --since 24h`
- **Visual regression baselines**:
  `retrace tester baseline accept <spec> --run-dir <run>`

## Health check

```bash
retrace doctor
```

Reports presence of every pillar (SDK keys, tester specs, API specs,
monitor incidents, connected repos, `gh` availability) with friendly
next-step hints in every WARN.

## Where to ask for help

- File an issue: https://github.com/txmed82/retrace/issues
- Look at the unified [`Incident` model](../src/retrace/qa_incidents.py)
  if you want to understand how the pillars converge.
- For security reports: see [`SECURITY.md`](../SECURITY.md).

## Where Retrace explicitly is NOT

- A paid service. The repo is the product.
- A wrapper around a single LLM provider. BYOK: use any
  OpenAI-compatible endpoint, Anthropic, OpenRouter, or a local model.
- A replacement for your test runner. It produces Browser Harness +
  HTTP specs and drives the loop; the actual execution is whatever
  engine the spec declares.
