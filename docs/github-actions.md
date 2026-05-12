# GitHub Actions

Retrace ships three drop-in composite actions under
[`.github/actions/`](../.github/actions/) so you can wire it into your
CI without writing custom workflow logic. All three are composite
shell-step actions — no Docker images, no Node.js dependencies, no
container pulls. They run in seconds on the same runner as the rest
of your CI.

| Action | Purpose | When to run |
|---|---|---|
| [`pr-review`](#pr-review) | Post Retrace's templated + LLM PR review as a PR comment | `pull_request` events |
| [`source-map-upload`](#source-map-upload) | Record a deploy marker and upload all `*.map` files to your Retrace server | After your build, before deploy |
| [`qa-auto`](#qa-auto) | Run the killer-demo flow: top open QA incident → auto-generated UI test → draft fix PR | `workflow_dispatch` (operator-triggered) |

## Quickstart

If you want **just** the LLM PR review on every pull request, paste
this into `.github/workflows/retrace-review.yml`:

```yaml
name: Retrace review

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write   # required for --post-comment

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: txmed82/retrace/.github/actions/pr-review@main
        with:
          # Optional: enable the LLM review. Drop these three to keep
          # the templated-only review (changed files, affected flows,
          # prior failures, missing tests).
          llm-base-url: https://api.openai.com/v1
          llm-model: gpt-4o-mini
          llm-api-key: ${{ secrets.RETRACE_LLM_API_KEY }}
```

That's it — 8 functional lines of YAML and one secret. The action
checks out + installs Retrace from the `main` branch (pin to a tagged
release for stability once we ship them).

## `pr-review`

### Inputs

| Name | Required | Default | Notes |
|---|---|---|---|
| `pr-number` | no | `${{ github.event.pull_request.number || github.event.number }}` | The PR to review. |
| `repo` | no | `${{ github.repository }}` | `owner/name`. |
| `post-comment` | no | `true` | Set `false` for dry runs. Requires `pull-requests: write`. |
| `run-affected-tests` | no | `false` | Run Retrace tester specs that cover affected flows. Slow on big PRs. |
| `use-llm` | no | `auto` | `on` / `off` / `auto` (default: enable iff `llm-base-url`+`llm-api-key` are set). |
| `llm-self-critique` | no | `false` | One extra LLM call to rank/dedupe suggestions when there's overflow. |
| `llm-provider` | no | `openai_compatible` | Or `anthropic` / `openrouter`. |
| `llm-base-url` | no | `""` | Required when `use-llm=on`. |
| `llm-model` | no | `""` | e.g. `gpt-4o-mini`, `claude-sonnet-4-20250514`. |
| `llm-api-key` | no | `""` | **Pass as a secret.** |
| `python-version` | no | `3.11` | |
| `retrace-ref` | no | `main` | Git ref of `txmed82/retrace`. Pin to a tag in prod. |
| `working-directory` | no | `.` | Where the checked-out repo lives — for route detection. |

### Outputs

| Name | Notes |
|---|---|
| `comment-url` | URL of the posted PR comment (empty when `post-comment=false`). |

### Fork-safety: `pull_request` vs `pull_request_target`

`pull_request` runs the action in a sandbox without access to
secrets — secure but breaks `--post-comment`. `pull_request_target`
runs with secrets available but checks out the **base** branch, not
the PR's HEAD. Two reasonable patterns:

**Pattern A — no secrets** (templated review only, no LLM):

```yaml
on:
  pull_request:
permissions:
  pull-requests: write
```

The action's `auto` LLM mode notices the empty `llm-api-key` and
skips the LLM call entirely. The templated review (changed files,
affected flows, prior failures, missing tests) still ships.

**Pattern B — first-party PRs only** (LLM enabled, no fork risk):

```yaml
on:
  pull_request:
permissions:
  pull-requests: write
jobs:
  review:
    if: github.event.pull_request.head.repo.full_name == github.repository
    # ...rest as before
```

The `if` guard skips the LLM call on fork PRs while keeping it on
first-party branch PRs. Sentry's official action uses the same
shape.

## `source-map-upload`

### Inputs

| Name | Required | Default | Notes |
|---|---|---|---|
| `api-base-url` | yes | — | Your Retrace server, e.g. `https://retrace.example.com`. |
| `service-token` | yes | — | Service token with `source_maps:write` + `deploy:write`. **Pass as a secret.** |
| `environment-id` | yes | — | e.g. `env_production`. |
| `source-map-dir` | yes | — | Recursively uploaded. |
| `sha` | no | `${{ github.sha }}` | Used as the source-map `release` and deploy SHA. |
| `artifact-prefix` | no | `""` | URL prefix to prepend to each map's `artifact_url`. |
| `branch` | no | `${{ github.ref_name }}` | |
| `author` | no | `${{ github.actor }}` | |
| `record-deploy` | no | `true` | Skip if you already recorded the deploy in a prior step. |
| `fail-on-upload-error` | no | `true` | Set `false` to keep the build green even if some maps fail. |

### Outputs

| Name | Notes |
|---|---|
| `uploaded-count` | Number of source maps successfully uploaded. |
| `skipped-count` | Files skipped (non-JSON, failed upload with `fail-on-upload-error=false`, etc.). |
| `deploy-public-id` | `public_id` of the recorded deploy marker. |

### Example

```yaml
name: Upload source maps + record deploy

on:
  push:
    branches: [main]

jobs:
  upload:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: npm ci && npm run build
      - uses: txmed82/retrace/.github/actions/source-map-upload@main
        with:
          api-base-url: ${{ secrets.RETRACE_API_BASE_URL }}
          service-token: ${{ secrets.RETRACE_SERVICE_TOKEN }}
          environment-id: env_production
          source-map-dir: dist
          artifact-prefix: https://cdn.example.com/static
```

### Why this one is dependency-free

Source-map uploads are the most common Retrace CI step, so we
deliberately built this action on `curl` + `jq` only. No Python
install (saves ~10s per run), no retrace CLI install (~30s). A typical
Next.js build directory of 80 maps uploads in under 30s.

## `qa-auto`

### Inputs

| Name | Required | Default | Notes |
|---|---|---|---|
| `repo` | no | `${{ github.repository }}` | Connected repo. |
| `incident-id` | no | `""` | Specific `INC-…` id; otherwise picks the top open incident. |
| `project-id` | no | `""` | For multi-workspace stores. |
| `environment-id` | no | `""` | For multi-workspace stores. |
| `base-branch` | no | `""` | Fix PR's base. Empty = repo default. |
| `app-url` | no | `""` | App URL the generated UI test should target. |
| `execution-engine` | no | `harness` | `harness` / `native` / `auto`. |
| `apply-with` | no | `""` | Invoke a coding agent: `""` / `auto` / `claude` / `codex`. |
| `draft` | no | `true` | Open the fix PR as draft. |
| `no-pr` | no | `false` | Skip the PR; just produce the fix prompt. |
| `python-version` | no | `3.11` | |
| `retrace-ref` | no | `main` | |
| `working-directory` | no | `.` | |

### Outputs

| Name | Notes |
|---|---|
| `incident-id` | Public id of the incident worked on. |
| `pr-url` | URL of the opened fix PR (empty if `no-pr=true`). |

### Example

```yaml
name: Retrace qa auto

on:
  workflow_dispatch:
    inputs:
      incident-id:
        description: "INC-XXX id (leave empty for top open incident)"
        required: false
        default: ""

jobs:
  qa-auto:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: txmed82/retrace/.github/actions/qa-auto@main
        with:
          incident-id: ${{ inputs.incident-id }}
          apply-with: claude
```

### Caveats

- The action runs the full UI test flow inside the GitHub-hosted
  runner. For Playwright-based tests, the runner needs the
  Chromium dependencies installed — the action's `retrace[dev]`
  install picks those up.
- `apply-with: claude` / `codex` requires those CLIs to be
  installed and authenticated on the runner; add the relevant
  setup step before the action.
- `qa-auto` is intentionally `workflow_dispatch`-only by default.
  Don't wire it to `pull_request` or `schedule` until you have a
  dedicated environment for the side effects (branches, draft PRs).

## Pinning

Until we tag releases, the actions live on `main` and we'll honor
the contracts pinned by
[`tests/test_github_actions_templates.py`](../tests/test_github_actions_templates.py).
Once we cut `v0.1.0`, you can `uses: txmed82/retrace/.github/actions/pr-review@v0.1.0`
and the test contract guarantees the inputs/outputs you wired won't
silently change under you.

## Where to next

- [Python SDK](python-sdk.md) — for `retrace_sdk.capture_exception()`
  from your FastAPI / Flask / Django service.
- [Roadmap](roadmap.md) — P0.3 was these actions; P0.4 is browser-SDK
  breadcrumbs.
