slug: /

# Retrace

Your real users are your QA team. Retrace finds the bugs they hit, writes the tests, and opens the fix PR.

## What it does

- Capture live user sessions from the browser or PostHog replay.
- Detect UX failures: console errors, broken network calls, rage clicks, dead clicks, blank renders, error toasts, and abandonment after errors.
- Cluster repeated failures into replay-backed issues with severity, affected users, and session links.
- Auto-generate deterministic UI regression specs from replay interactions so failures become tests.
- Link each issue to a GitHub repo, rank likely source files, and produce a fix prompt for a coding agent.
- Open a draft PR via `gh` so a human or agent can finish the loop.
- Track issues as new, ongoing, regressed, or resolved; re-run specs after fixes to catch regressions.

## When to use Retrace

| You need... | Retrace approach |
|-------------|---------------|
| Catch UI bugs before users report them | Browser SDK replay + 8 deterministic detectors |
| Turn user sessions into regression tests | Replay-derived deterministic specs |
| Fix issues with AI/human pair programming | Auto-generated fix prompts + draft PR |
| Monitor frontend/backend errors | Sentry-compatible + OTel ingest into the same incident model |
| Track visual regressions | Native + Playwright + perceptual baseline comparison |
| Review PRs for risky changes | Diff analysis + affected-test selection + optional LLM review |

## Get started

30-second install:

```bash
curl -fsSL https://raw.githubusercontent.com/txmed82/retrace/main/install.sh | bash
```

Or with Docker Compose:

```bash
curl -fsSL https://raw.githubusercontent.com/txmed82/retrace/main/install.sh | bash -s -- --docker
```

See the [Install guide](/install) for full options.

## Architecture

```
Browser SDK/PostHog → Ingest API → Replay Processing → Detectors → Clusterer →
  Unified Incidents → Auto-Repro → Repo Scoring → Fix Prompt → Draft PR
```

## License

[MIT](https://github.com/txmed82/retrace/blob/main/LICENSE)
