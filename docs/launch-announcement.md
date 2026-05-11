# Launch announcement template

Three drafts: HN Show HN, an X/Twitter thread, and a one-liner for
indiehackers / Discords. Pick what fits each surface; lift the bullets
freely.

---

## Show HN draft

**Title** (≤80 chars):

> Show HN: Retrace – an OSS QA tool that turns user bugs into draft PRs

**Body** (Markdown, ≤500 words):

```text
Hi HN — I built Retrace because every existing "AI QA" tool I tried solved
one slice of the loop and left the rest manual. Sentry catches errors but
won't write a regression test. Playwright runs the test but doesn't know a
real user hit the bug. Cursor writes the fix but doesn't know which file
the regression actually broke. So I built one tool that does all of it.

Retrace is open source and self-hostable. It catches bugs from five
sources — rrweb replays, UI tests, API tests, Sentry-compatible / OTel
ingest, and PR review — and unifies them into one Incident queue. Then
one command runs the loop:

    retrace qa auto --repo your-org/your-app

…which picks the top open incident, auto-generates a Browser Harness UI
test that reproduces it, and (if confirmed) opens a draft PR via `gh`
with a fix prompt + likely culprit files. Optionally invokes a local
agent (`claude`, `codex`) inside a git worktree to apply changes before
pushing.

The repo: https://github.com/txmed82/retrace

Quickstart prints both a replay <script> tag AND a Sentry-compatible
DSN against the same workspace, so paste-replacing Sentry is one step:

    retrace quickstart
    # then paste the two snippets it prints into your <head>

What's wired up:

- Replay capture (rrweb) + 8 heuristic detectors (rage click, dead
  click, console error, network 4xx/5xx, error toast, blank render,
  abandonment).
- AI-driven UI testing (Browser Harness + native runner + visual
  explorer) with reusable specs and OpenAPI import for API tests.
- Sentry-compatible DSN + OTel logs/traces ingest + alert rules.
- PR review (`retrace review --post-comment`) — diff parsing, route
  detection for JS/TS/Python (FastAPI/Flask/Django) and Ruby (Rails),
  prior-failure linkage, missing-test recommendations.
- Fix-PR step runs inside a git worktree, so your branch is never
  touched.

What's deliberately NOT there:

- No paid tier. BYOK; the repo is the product.
- No cloud yet. Self-host first.
- Code review uses templated comments, not a paid AI reviewer.

I'd love feedback — especially from anyone who's used multiple of
{Sentry, Playwright, Greptile/CodeRabbit, Datadog} and felt the same
fragmentation.
```

**Don'ts on HN**:

- Don't oversell as a "replacement" for any one tool — frame as a
  unifier.
- Don't lead with the AI angle. Lead with the loop being closed.
- Reply to every top-level comment in the first 90 minutes if you can.

---

## X / Twitter thread

Six tweets. Each ≤275 chars. Paste in order.

> 1/6
>
> i shipped Retrace today — an open-source QA tool that catches real
> user bugs, writes the test, and opens the fix PR.
>
> one command: `retrace qa auto --repo your-org/your-app`
>
> github.com/txmed82/retrace

> 2/6
>
> the wedge: 5 signal sources (replays, UI tests, API tests, Sentry
> errors, PR review) all land in one Incident queue. you stop chasing
> alerts in 4 different dashboards.

> 3/6
>
> `retrace quickstart` prints both an rrweb script tag and a
> Sentry-compatible DSN against the same workspace. paste both, done.

> 4/6
>
> the fix-PR step runs inside a `git worktree`, so your checked-out
> branch is never touched. it commits the prompt + suspected files,
> optionally runs `claude` or `codex` inside, and opens a draft PR
> via `gh`.

> 5/6
>
> route detection works for express, next.js, fastapi, flask, django,
> rails. so the prior-failure linkage in PR review isn't js-only.

> 6/6
>
> open source. self-hostable. BYOK. would love feedback.
>
> github.com/txmed82/retrace

---

## One-liner (Discord / indiehackers / Slack)

> Hey — built [Retrace](https://github.com/txmed82/retrace), an OSS QA
> tool that unifies real-user bugs + UI tests + API tests + Sentry +
> PR review into one incident queue, then closes the loop with `retrace
> qa auto` → auto-generated regression test → draft PR. Self-hostable,
> BYOK. Would love eyes from anyone who's juggling >1 of those tools.
