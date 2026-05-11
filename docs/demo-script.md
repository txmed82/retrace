# 90-second demo script

This is the script for the launch demo video. Keep it tight; the goal
is to show every pillar landing in one queue, then the killer-demo
loop closing.

## Setup (do before recording)

```bash
mkdir /tmp/retrace-demo && cd /tmp/retrace-demo
uv pip install -e <path-to-retrace>
```

Have a real repo cloned at `/tmp/retrace-demo/myapp` for the `qa auto`
step. The repo doesn't have to be sophisticated — even an empty git
init works for the demo.

```bash
cd /tmp/retrace-demo
gh repo clone your-username/myapp
cd myapp && git config user.email demo@retrace.dev && cd ..
```

## Recording

Target: **90 seconds total**. Talk to camera, screen recording on.

### 0:00 — 0:05 — The promise

> "Retrace is an open-source QA tool that turns a real user bug into a
> draft PR. Five signal sources, one incident queue, one command to
> ship the fix."

### 0:05 — 0:20 — Quickstart + two paste-able snippets

```bash
retrace quickstart
```

Pause on the output:

> "One command. Writes config. Mints an SDK key. Prints a script tag
> for replay capture and a `Sentry.init` DSN for error monitoring —
> both pointing at the same workspace."

### 0:20 — 0:35 — `demo all` shows the queue

```bash
retrace demo all
retrace qa list
```

Pause on the list:

> "Five rows. One from a synthetic replay session. One from a UI test
> failure. One from an API test failure. One from a Sentry-style
> monitor alert. One from a PR-review finding. Same shape, same id
> namespace, same priority queue."

### 0:35 — 0:50 — `qa show` on one incident

```bash
retrace qa show <INC-…>     # pick the API test one
```

> "Severity, suspected cause, reproduction recipe, evidence pointers,
> and the operational state of the auto-repro + fix-PR pipeline."

### 0:50 — 1:25 — The killer demo

```bash
retrace github connect --repo your-username/myapp --local-path /tmp/retrace-demo/myapp
retrace qa auto --repo your-username/myapp
```

Talk over the output:

> "One command. Picks the top open incident. Generates a Browser
> Harness UI test that reproduces it. Runs it. If the bug surfaces,
> scores the repo for likely culprit files, writes a fix prompt,
> opens a git worktree, optionally runs `claude` or `codex` inside,
> commits, and opens a draft PR via `gh`. Your branch is untouched
> the whole time."

End on the PR URL.

### 1:25 — 1:30 — Outro

> "Open source. Self-hostable. BYOK. GitHub at
> github.com/txmed82/retrace."

## Things to NOT do on camera

- Don't claim Retrace replaces Sentry/Datadog/Cypress entirely. It
  *complements* them today; the wedge is unifying their signals into
  one incident queue + driving the fix-PR loop.
- Don't run `retrace doctor` or `retrace ui` unless you have ~60s to
  show what they do. The list view of `qa list` is enough for the
  demo.
- Don't run the full PostHog import path — it's a 30-second pause.

## Stretch (3-minute version)

If you have a longer slot, add these between sections:

- **0:35** Show `retrace review --pr <…> --post-comment` running
  against a real PR and the resulting comment.
- **1:15** Tab into `retrace ui` and click the new "QA Incidents" tab.
