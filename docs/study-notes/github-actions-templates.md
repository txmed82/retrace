# Study note: third-party GitHub Actions templates

**Studied 2026-05-11.** Cross-referenced four reference repos to settle
the shape and ergonomics of Retrace's three composite actions.

## What we read

- [`getsentry/action-release`](https://github.com/getsentry/action-release) (HEAD) —
  Sentry's official "create a release + upload source maps" action.
  Docker-based, opinionated env var naming (`SENTRY_AUTH_TOKEN`,
  `SENTRY_ORG`, `SENTRY_PROJECT`), wraps `sentry-cli`.
- [`codecov/codecov-action`](https://github.com/codecov/codecov-action) (HEAD) —
  the gold standard for "drop in, no setup." JS action, fetches a
  pinned uploader binary at runtime, ~5s cold start.
- [`super-linter/super-linter`](https://github.com/super-linter/super-linter) (HEAD) —
  the heavyweight "run many things in one composite" approach. Slow
  by design; not the model we want.
- [`qodo-ai/pr-agent` workflows](https://github.com/qodo-ai/pr-agent/tree/main/.github/workflows) (HEAD) —
  PR-Agent's own dogfooding workflow. Posts a PR review comment on
  `pull_request_target`. Same UX shape we're targeting for `pr-review`.

## Takeaways we took

1. **Composite shell actions over Docker / Node.**
   - Docker actions add 10–30s of image pull on cold runs and require
     a registry (GHCR). Pain for a young OSS project still moving fast.
   - Node actions need a compile step + checked-in `dist/` (super-linter
     and codecov both struggle with this).
   - Composite shell actions are just YAML + shell. Cold start ≈ 1s.
     The runner already has `bash`, `curl`, `jq`, `python` — that's all
     we need.

2. **Opinionated secret/env-var naming.**
   - Sentry standardized on `SENTRY_*`. We mirror with `RETRACE_*`:
     `RETRACE_API_BASE_URL`, `RETRACE_SERVICE_TOKEN`, `RETRACE_LLM_API_KEY`.
     Names show up verbatim in the docs and copy-paste examples so users
     don't have to invent a convention.

3. **Pin the input contract with a test.**
   - PR-Agent renames inputs occasionally and breaks downstream
     workflows. We added `tests/test_github_actions_templates.py` which
     fails CI on any input rename / drop. The contract is documented in
     `docs/github-actions.md` and pinned in the test — neither drifts
     silently.

4. **One narrow action per concern, not a mega-action.**
   - super-linter bundles 50+ linters; cold start is 90s. Users who
     just want one of them pay for all 50.
   - We split into three (`pr-review`, `source-map-upload`, `qa-auto`).
     A user who only wants source-map uploads runs a 30s job, not a
     6-minute one.

5. **Curl + jq for the hot-path action; Python for the heavy ones.**
   - `source-map-upload` is the most-frequently-fired action (every
     deploy). We deliberately keep it Python-free — pure bash + curl +
     jq. The contract test asserts no `setup-python` step creeps in.
   - `pr-review` and `qa-auto` need the Retrace CLI, so they install
     Python; the cost is amortized over the heavier work those actions
     do.

6. **Fork safety left to the consumer.**
   - We don't pick `pull_request` vs `pull_request_target` for the
     user — that's a workflow-level decision. Docs lay out the two
     reasonable patterns (no-secrets templated review on
     `pull_request`, vs LLM review with a first-party-only `if` guard
     on `pull_request`).

7. **JSON in/out of `retrace review`.**
   - Sentry's action returns nothing structured; users `cat` the log
     to find URLs. We thread `--json` through `pr-review` so the
     action can expose `comment-url` as a real step output —
     consumers can `if: steps.review.outputs.comment-url != ''` etc.

## What we deliberately don't take

- **Node-based wrappers.** sentry-cli is bundled as a Node tool which
  forces every user onto Node-on-runner cold start. Composite shell
  actions skip that tax.
- **Docker images.** We were tempted to ship a `retrace/action:v1`
  image so the action wouldn't need a Python install per run, but
  that means publishing to GHCR on every commit AND tagging discipline
  we don't have yet. Composite shell with a pinned git ref is the
  right tradeoff for v1.
- **A single "do everything" action.** super-linter's model collapses
  user choice into a checklist of toggles. Three separate actions
  with a focused interface each beats a 30-input mega-action.
- **Implicit branches/refs.** sentry-cli auto-detects release names
  from `GITHUB_REF`. We require `sha` as an input (defaulted to
  `github.sha`) so the user can override for monorepos with
  per-app release names.

## Files in this PR that map back

- `.github/actions/pr-review/action.yml` ← PR-Agent's `pr-agent.yaml`
  workflow shape, but as a reusable action so consumers don't copy
  the YAML.
- `.github/actions/source-map-upload/action.yml` ← Sentry's
  `action-release` source-map flow, but as a curl-only composite.
- `.github/actions/qa-auto/action.yml` ← original to Retrace (no
  direct competitor; this is the killer-demo action).
- `tests/test_github_actions_templates.py` ← inspired by how PR-Agent
  pins its own settings via tests — we extend the idea to action
  input contracts.
- `docs/github-actions.md` ← codecov's docs are the readability bar.
