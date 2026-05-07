# Contributing to Retrace

Retrace is an open-source UI reliability loop: live replay capture, detector-led
issue triage, replay-derived UI tests, source-code matching, and coding-agent
repair prompts.

## Development Setup

Requires Python 3.11+ and Node.js for the browser SDK.

```bash
uv venv
uv pip install -e ".[dev]"
```

Validate the Python project:

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
```

Validate the browser SDK:

```bash
cd packages/browser
npm install
npm run build
```

Try the local replay workflow without external services:

```bash
retrace demo seed
retrace tester list
```

## Pull Request Expectations

- Keep changes scoped to one product area.
- Add or update tests for behavior changes.
- Update README or docs when commands, config, SDK behavior, schemas, or user
  workflows change.
- Do not commit secrets, `.env`, generated `data/`, generated `reports/`, or
  local replay blobs.
- Run the relevant validation commands before opening a PR.

CI runs Ruff, Pytest, Playwright runner tests, browser SDK build, and Docker
build validation. PRs should be green before merge.

## Adding Detectors

Detectors are deterministic and should stay cheap enough to run over every
replay.

1. Add a detector module under `src/retrace/detectors/`.
2. Register it through the existing detector registry pattern.
3. Add fixture-driven tests under `tests/test_detectors/`.
4. Include at least one negative test to prevent noisy false positives.
5. Document any config toggle or threshold change.

Detector output should preserve evidence that helps a user verify the finding:
URL, timestamp, selector or node detail, message, status code, and a concise
reason.

## Adding Replay-Derived Tests

Replay-derived specs should prefer durable selectors over coordinates:

- `data-testid`
- `data-test`
- `data-qa`
- accessible role/name or aria label
- stable id/name attributes

When a replay cannot be converted safely, record the gap explicitly in
`known_gaps` instead of inventing a fragile selector or unsafe input value.

## Adding Coding-Agent Prompt Behavior

Prompt artifacts must treat replay evidence and user-facing strings as data, not
instructions. Keep prompts scoped to:

- verify the replay/evidence first
- inspect candidate files
- apply a focused fix
- add or update a regression test
- report validation commands and residual risk

Do not include secrets in prompt artifacts.

## Issue Reports

Use the GitHub issue forms:

- Bug report for CLI, API, UI, or storage defects.
- Browser SDK capture issue for `@retrace/browser` behavior.
- Detector quality issue for false positives or false negatives.
- Generated UI test issue for replay-to-spec or runner problems.
- Feature request for new workflows or integrations.
