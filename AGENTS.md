# AGENTS.md — Retrace

> Agent context for working on the Retrace open-source project.

## What this project is

Retrace is a self-hostable, CLI-first QA loop for indie developers. It captures real user sessions via a browser SDK, detects UX failures (console errors, broken network calls, rage clicks, blank renders, etc.), clusters them into replay-backed incidents, auto-generates regression tests, scores the connected source repo, and opens draft fix PRs via `gh`.

Key differentiator: one unified incident queue across replay findings, UI tests, API tests, error monitors (Sentry-compatible + OTel), and PR review — then one command reproduces and fixes.

## Tech stack

- **Backend**: Python 3.11+, uv, Click CLI, FastAPI-ish HTTP server, SQLite (default) + Postgres (optional)
- **Frontend**: Built-in local UI (`retrace ui`) served over HTTP, rrweb replay player
- **Browser SDK**: TypeScript, rrweb-based replay capture
- **Python SDK**: `packages/python-sdk/`, stdlib-only transport, FastAPI/Flask/Django integrations
- **Testing**: pytest, Playwright (Chromium), pytest-httpx
- **Lint**: ruff
- **CI/CD**: GitHub Actions — ruff, pytest, e2e, Postgres smoke, Docker multi-arch build + GHCR publish
- **Docs**: Docusaurus (in `website/`), deployed to GitHub Pages
- **Install**: `install.sh` supports `--local` (uv) and `--docker` (Docker Compose)

## Project layout

| Path | Purpose |
|------|---------|
| `src/retrace/` | Main Python codebase — CLI, API, storage, replay, testers, detectors,LLM glue |
| `packages/browser/` | TypeScript browser SDK |
| `packages/python-sdk/` | Python retrace-sdk package |
| `tests/` | pytest suite (969 passing at last check) |
| `docker/` | Dockerfile + entrypoint.sh for multi-service compose stack |
| `docs/` | Markdown docs consumed by both the repo and Docusaurus |
| `website/` | Docusaurus site config + generated build |
| `.github/workflows/` | CI (`ci-cd.yml`) + Pages deploy (`deploy.yml`) |
| `.github/actions/` | Composite Actions: `pr-review`, `source-map-upload`, `qa-auto` |
| `install.sh` | One-liner installer for local and Docker installs |
| `config.example.yaml` | Non-secret configuration template |
| `.env.example` | Secret variables template |

## How to install locally

```bash
uv venv
uv pip install -e ".[dev]"
```

Validate:

```bash
ruff check src tests
pytest -q
```

## How the docs site works

- Docusaurus config lives in `website/docusaurus.config.ts`
- Docs content lives in `docs/` **and** `website/docs/`
- `website/docs/` contains the curated top-level pages (index, install, usage)
- `docs/*.md` contains the full reference material (quickstart, roadmap, architecture, etc.)
- The `deploy.yml` workflow builds and publishes to `txmed82.github.io/retrace/`
- GitHub Pages source is set to GitHub Actions (not a branch)

## CI/CD health checks

- `ci-cd.yml` runs ruff, pytest, e2e tests, Postgres smoke, Docker build (multi-arch linux/amd64 + linux/arm64), browser SDK build, Python SDK tests, Playwright runner tests
- `cd-ghcr` publishes `ghcr.io/txmed82/retrace:latest` and `sha-XXX` on push to `master`
- `deploy.yml` deploys Docusaurus site on push to `master`

## Known rough edges

- Version is `0.1.0a1` — signals alpha. Should advance to `0.1.0` or higher for credibility.
- GitHub Pages will serve from `txmed82.github.io/retrace/` once enabled and merged.
- `conduct@retrace.dev` is referenced in CODE_OF_CONDUCT.md but the domain is not owned.
- No Discord/Slack community link yet (CODE_OF_CONDUCT notes "if/when it exists").

## Agent workflow reminders

- When adding new docs, update `website/sidebars.ts` if they belong in the top-level navigation.
- When changing `config.example.yaml` or `.env.example`, update `docs/install.md` and `website/docs/install.md` if necessary.
- When adding new CLI commands, update `AGENTS.md` command tables and `website/docs/usage.md`.
- When modifying CI, verify `docker/Dockerfile` still builds and multi-arch still works.
- Do not commit generated `data/`, `reports/`, `.env`, or secrets.

## Quick command reference

```bash
retrace quickstart              # zero-config setup + SDK key
retrace run                     # ingest PostHog sessions
retrace api serve               # run the ingest API server
retrace ui                      # open local dashboard
retrace qa list                 # open incidents
retrace qa auto --repo org/app  # reproduce + fix PR pipeline
retrace tester create ...       # create a UI test spec
retrace tester run <spec_id>    # run a spec
retrace review --pr <url>        # PR diff analysis
retrace digest                  # daily markdown report
retrace doctor                  # health checks
```
