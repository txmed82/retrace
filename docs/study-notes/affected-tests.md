# Study note: diff-aware affected-test selection

**Studied 2026-05-12.**

## What we read

- [nrwl/nx](https://github.com/nrwl/nx) (HEAD) —
  `packages/nx/src/command-line/affected/` for the project-graph
  approach. nx tracks file→project edges and prunes the test set
  based on the dep graph.
- [bazel](https://docs.bazel.build/versions/main/build-event-protocol.html)'s
  affected-targets docs — same idea, BUILD-file based.

## Takeaways we took

1. **Intersect on routes, not on files.** nx works on file→project
   edges; we don't have a project graph but we DO have a parsed
   route manifest + per-route flow detection from the existing
   `infer_affected_flows()`. Intersecting API spec URL paths with
   detected routes is a clean diff-aware filter that doesn't
   require new infrastructure.
2. **Strict-prefix path matching.** A naive substring rule would
   match `/api/login` against `/api/login-history`. We use
   "equality OR strict-prefix with `/` delimiter" so
   `/api/users` matches `/api/users/42` but not `/api/users-export`.
3. **Cap at 6 affected specs per run.** Matches `_run_affected_tests`'s
   existing UI cap — a PR touching dozens of routes still finishes
   in a sane wall-clock time.
4. **Best-effort error handling per spec.** Same pattern as
   `_run_affected_tests`: a malformed spec degrades per-spec into a
   `status="error"` row, doesn't abort the whole review.
5. **`--include-api` defaults to ON.** Most users running
   `--run-affected-tests` want broad coverage; opting OUT is the
   exotic case. The flag exists so a noisy API suite can be excluded
   without disabling UI tests.

## What we deliberately don't take

- **A full project graph.** nx's graph approach is overkill for our
  shape — we'd need to model frameworks (FastAPI / Express / etc.)
  to know which file owns which route. The existing
  `infer_affected_flows()` heuristic already does this on a
  best-effort basis and is good enough for the diff-aware selection
  use case.
- **A path-template/parameter matcher.** OpenAPI defines `/users/{id}`
  paths; we don't currently parameterize, so a spec on `/users/42`
  matches an `/api/users` flow under the strict-prefix rule
  organically. If users start writing specs with `{id}` in the URL
  we'll need to extend the matcher to treat path-params as
  wildcards.

## Files in this PR that map back

- `src/retrace/pr_review.py` — added `AffectedAPISpec` dataclass,
  `affected_api_specs(analysis, specs_dir)`, and the path-extraction
  + matching helpers. The matcher is pure-Python and avoids
  pulling any URL library beyond stdlib `urlsplit`.
- `src/retrace/commands/review.py` — new `--include-api` flag on
  `retrace review`; `_run_affected_api_tests()` mirrors
  `_run_affected_tests()` semantics; comment body + JSON output
  gain `affected_api_test_results`.
- `tests/test_pr_review_affected_api_specs.py` — 11 tests pinning
  the matching rules (esp. the strict-prefix non-match for
  `login-history`).
