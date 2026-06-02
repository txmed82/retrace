# Retrace

Your real users are your QA team. Retrace finds the bugs they hit, writes the tests, and opens the fix PR.

![CI](https://github.com/txmed82/retrace/actions/workflows/ci-cd.yml/badge.svg)
![License](https://img.shields.io/github/license/txmed82/retrace)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Docker](https://img.shields.io/badge/docker-multi--arch-blue)

## What it does

- **Capture** live user sessions from the browser SDK or PostHog replay.
- **Detect** UX failures: console errors, broken network calls, rage clicks, dead clicks, blank renders, error toasts, and session abandonment.
- **Cluster** repeated failures into replay-backed issues with severity and affected users.
- **Auto-generate** deterministic UI regression specs so real bugs become repeatable tests.
- **Fix** by linking each issue to a repo, ranking likely source files, and opening a draft PR with a fix prompt.
- **Track** issues as new, ongoing, regressed, or resolved, and re-run specs after fixes.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/txmed82/retrace/master/install.sh | bash
```

Or with Docker Compose:

```bash
curl -fsSL https://raw.githubusercontent.com/txmed82/retrace/master/install.sh | bash -s -- --docker
```

Then:

```bash
retrace quickstart   # mints an SDK key and prints a ready-to-paste <script> tag
retrace api serve     # start the ingest API
retrace ui             # open the local dashboard
```

## Quick demo

```bash
retrace github connect --repo your-org/your-app --local-path /path/to/repo
retrace qa auto --repo your-org/your-app
```

This picks the highest-priority open incident, reproduces it with a generated test, scores the repo, and opens a draft fix PR.

## Why Retrace

| Problem | How Retrace helps |
|---------|------------------|
| Users find bugs first | Session replay + automatic detection |
| Reproducing bugs is slow | Replay-derived regression specs |
| Debugging takes hours | Fix prompts with replay evidence + likely source files |
| Too many tools | One incident queue for replay / UI test / API test / error monitor / PR review |
| Budget constraints | Self-hostable, BYOK for LLM and third-party services |

## Architecture

```
Browser / PostHog → Ingest API → Replay Processing → Detectors → Clusterer →
  Unified Incidents → Auto-Repro → Repo Scoring → Fix Prompt → Draft PR
```

## Documentation

- [Install](https://txmed82.github.io/retrace/install)
- [Usage](https://txmed82.github.io/retrace/usage)
- [Quickstart](https://txmed82.github.io/retrace/quickstart)
- [Architecture](https://txmed82.github.io/retrace/architecture)

## License

[MIT](LICENSE)
