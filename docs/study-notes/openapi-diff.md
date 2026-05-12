# Study note: OpenAPI contract diff

**Studied 2026-05-12.**

## What we read

- [openapi-diff (Tufin)](https://github.com/Tufin/oasdiff) — the
  reference for breaking-vs-safe classification rules. Go binary;
  not a dep, but the ruleset is the canonical one in the OpenAPI
  community.
- [schemathesis](https://github.com/schemathesis/schemathesis) —
  OpenAPI-fuzz reference; informed how we walk `$ref`.

## Takeaways we took

1. **Breaking-vs-safe classification kept tight to industry norms.**
   We adopted the five breaking kinds that all OAS-aware tools
   agree on (`operation_removed`, `required_request_field_added`,
   `response_schema_field_removed`, `success_status_removed`,
   `enum_value_removed`). No exotic kinds; if a tool we trust
   doesn't agree it breaks, we don't either.
2. **One level of `$ref` resolution.** Real-world specs ref
   `#/components/schemas/X` from `requestBody` constantly. We walk
   the ref once; deeper indirection stays as the raw `$ref` dict
   because chasing it adds complexity without changing the
   classification we'd produce (further nested schemas would
   double-fire findings).
3. **Severity = `"high"` for every breaking change.** Easy
   filtering and matches user intuition: any contract regression
   is "ship-blocking by default."
4. **Per-change `qa_incident` filing via the existing bridge.**
   Reuses `sync_qa_incident_from_pr_review_finding` so contract
   regressions flow through the same `qa list` / `qa auto` rails
   as everything else. No new incident kind needed.
5. **Determinism via sorted iteration everywhere.** Every loop
   over `set(...)` is `sorted(set(...))` so two runs against the
   same documents produce identical output. Makes the CLI safe to
   wire into CI on `--fail-on-breaking`.

## What we deliberately don't take

- **Multi-level `$ref` resolution / external `$ref` URIs.**
  oasdiff handles both. We don't because (a) external refs
  introduce network I/O at diff time and (b) deeper local refs are
  rare in our target customer's specs. Easy follow-up if needed.
- **Per-content-type schema diff.** We prefer `application/json` and
  fall back to whatever's there. If a spec serves both JSON and
  form-encoded with different shapes, the diff matches whichever
  comes first. Real-world OAS specs almost always have one content
  type per operation.
- **Path-template parameter sensitivity.** `/users/{id}` and
  `/users/{user_id}` are different paths in our diff today, which
  is the strict interpretation. oasdiff has a "treat path params
  by position" mode; we don't, because in practice param renames
  ARE breaking changes (URL builders typically reference the
  param name).
- **Deprecation-aware filtering.** oasdiff respects `deprecated:
  true` (a deprecated operation removed isn't flagged as breaking).
  We don't yet — every removal is breaking. Easy add when users
  ask.

## Files in this PR that map back

- `src/retrace/openapi_diff.py` ← original; classification rules
  taken from oasdiff. Loader mirrors `openapi_import.load_openapi_document`
  but skips the "must include paths" assertion (a brand-new spec
  can legitimately add paths to a previously empty doc).
- `src/retrace/commands/tester.py` (`tester api-diff` command) ←
  the user-facing surface. `--fail-on-breaking` defaults ON so CI
  consumers get a non-zero exit on the first contract regression.
- `tests/test_openapi_diff.py` — 21 tests covering every breaking
  kind, the ref-walker, JSON/YAML loader rejection, determinism,
  and the `ContractChange.title` rendering used in incident titles.
