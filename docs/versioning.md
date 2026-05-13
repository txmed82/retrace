# Versioning + breaking-change policy

This document is the contract for what users can rely on across
Retrace versions and what stays internal.

## Shipping artifacts

| Artifact | Version source | Stable as of |
|---|---|---|
| `retrace` CLI / server | `src/retrace/__init__.py` `__version__` (mirrored to `pyproject.toml`) | v0.x — pre-stable |
| `retrace_sdk` (Python) | `packages/python-sdk/retrace_sdk/__init__.py` `__version__` | v0.x — pre-stable |
| `@retrace/browser` (npm) | `packages/browser/package.json` `version` | v0.x — pre-stable |
| `config.yaml` schema | Documented by the Pydantic models in `retrace.config` | v0.x — pre-stable |
| Ingest HTTP endpoints | `/api/sdk/*`, `/api/sentry/*`, `/api/otel/v1/*` | v0.x — pre-stable |
| Storage schema | `retrace.storage.SCHEMA` | **explicitly NOT stable** at any version |

> "Pre-stable" means semver does not yet hold. Minor releases CAN
> contain breaking changes. We will document them; we will not
> silently break.

## Semver stance, post-v1.0

When any artifact reaches its `1.0` cut, semver applies:

- **MAJOR (`X.0.0`)** — breaking change to the documented surface.
  - Removed CLI subcommand or flag
  - Removed SDK public API
  - Required new fields in an ingest envelope
  - Removed `config.yaml` key
- **MINOR (`x.Y.0`)** — backwards-compatible additions.
  - New CLI subcommand or flag
  - New optional SDK API
  - New optional `config.yaml` key (default value preserves prior
    behavior)
  - New ingest endpoint or new optional fields
- **PATCH (`x.y.Z`)** — bug fixes only. No surface change.

Each shipping artifact versions **independently**. The CLI can be
at `1.4.2` while `@retrace/browser` is at `2.0.0`. They share a
codebase, not a version.

## What's promised stable (post-v1.0)

These are the surfaces users build against:

1. **CLI subcommand tree.** `retrace tester run`, `retrace review`,
   `retrace qa auto`, etc. — names + flags + exit codes.
2. **SDK public API.** The functions / methods documented in
   `docs/python-sdk.md` and the `RetraceClient` / `init()` surface
   in `@retrace/browser`. The `RetraceBrowserOptions` /
   `RetracePythonOptions` shapes are the contract.
3. **Ingest envelope shapes.** What an SDK or `curl` can POST and
   expect to land. Adding optional fields is non-breaking;
   changing semantics of an existing field is.
4. **`config.yaml` keys.** Adding new keys is non-breaking;
   renaming or removing an existing key is.
5. **JSON shapes emitted to stdout by CLI commands.** When a
   command emits structured output (e.g. `retrace data retention
   apply` → JSON), downstream tooling parses it; the keys are part
   of the contract.

## What is explicitly NOT promised stable

These surfaces can change in any release without notice:

1. **Storage schema** (`SCHEMA` in `retrace.storage`). The schema
   is an implementation detail of the storage backend. Tools that
   poke at the sqlite file directly are on their own; use the
   storage API (`Storage.list_*`, etc.) instead.
2. **Internal Python module paths.** `retrace.foo.bar.baz` is not
   importable contract unless it's surfaced in the public API
   docs.
3. **Undocumented CLI flags** — anything beginning with `_` or
   prefixed `--debug-*` or `--internal-*`.
4. **Run-artifact directory layout** beyond what's needed for
   `retrace data backup` to round-trip.
5. **LLM prompt versions / shapes.** The PR-review prompts are
   tuned for current models and will change.

## Deprecation window

When a key / flag / endpoint is going to be removed:

1. **Minor release N**: the new replacement ships. The old surface
   continues to work, but using it emits a `DeprecationWarning`
   (Python) or a console warning (CLI / browser SDK).
2. **Minor release N+1**: the old surface still works, the
   warning becomes louder (the CLI emits to stderr; the browser
   emits to console.warn).
3. **Major release N+2**: the old surface is removed.

For SDK keys / config keys: minimum **one minor release** of
warnings. For CLI flags: same. For ingest envelope changes that
would invalidate older SDK clients: minimum **two minor releases**
of warnings (one full release cycle so users have time to update
clients in production).

We DO NOT remove a deprecated surface inside a patch release.
Ever.

## How breaking changes get communicated

1. `CHANGELOG.md` at the repo root — every release notes its
   breaking changes at the top.
2. `retrace doctor` will surface deprecated config keys that
   appear in the user's `config.yaml`.
3. GitHub Releases — the release body links the relevant
   `CHANGELOG.md` section.
4. The `docs/study-notes/` directory captures architectural
   rationale when a breaking change is significant.

## When v1.0 happens

Each artifact reaches `1.0` when:

- The public API has been stable across at least one minor cycle
  with no breaking changes
- At least one real (non-trivial) user has been running it in
  production for 30 days
- The CHANGELOG has a "1.0 readiness" section that lists what's
  in scope and what isn't

The CLI, the Python SDK, and the browser SDK are likely to reach
`1.0` independently. The `config.yaml` schema follows the CLI's
version.

## Version check

The current CLI version is always:

```bash
retrace --version
# → retrace <X.Y.Z>
```

Programmatic access:

```python
import retrace
retrace.__version__
```

```ts
import { VERSION } from "@retrace/browser";
console.log(VERSION);
```
