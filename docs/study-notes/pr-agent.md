# pr-agent (Qodo)

- **URL:** https://github.com/qodo-ai/pr-agent
- **Commit pinned:** `9ab2636f89952c69600dc2038d39f468de699fd0`
- **Studied while working on:** roadmap P0.1 (LLM-powered PR review)

## What it does that we don't

- The review itself is LLM-driven. A real model reads each diff hunk
  and emits a YAML-shaped review (severity, line range, security
  concerns, TODO scan, effort estimate). Our review today is 100%
  templated regex.

## What's clever

- **Hunk format with line numbers.** Each diff hunk is rendered as
  separate `__new hunk__` / `__old hunk__` sections with line numbers
  annotated next to the new lines. Lets the model emit
  `(file, start_line, end_line)` triples that map back to GitHub's
  PR-comment line indexing. (See `settings/pr_reviewer_prompts.toml`
  lines 7–47.)
- **Iterative compression on token overflow.** They render the
  "extended" diff first (extra context lines), measure tokens, and
  if it overflows the model budget they compress: drop extra lines,
  then drop unchanged hunks, then collapse files into an "Other
  modified files:" list. The output stays one prompt — they never
  re-request the model. (See `algo/pr_processing.py:38-127`.)
- **Pydantic schema in the prompt itself.** The system prompt declares
  the exact YAML output shape using Pydantic class definitions. Cuts
  malformed-YAML rates dramatically.
- **Optional toggles** (security review, TODO scan, effort estimate,
  contribution-cost) gated by jinja conditionals in the prompt
  template. One prompt, many use cases.

## What we should take

1. The hunk format with `__new hunk__` line numbers — it's the single
   biggest accuracy lever for inline-comment generation.
2. The token-budget-then-compress strategy, but **simplified**: drop
   extra-line context only. Skip the file-collapse fallback for v1;
   if the diff still overflows we just bail with `"diff too large"`.
3. The Pydantic-schema-in-prompt pattern. We can use a JSON schema
   instead of YAML since our LLM client expects JSON output.
4. The split between **summary**, **walkthrough**, **inline issues**
   so the PR comment has structure.

## What we should NOT take

- **Their settings system (dynaconf).** It's heavy and not how Retrace
  is configured. We have `LLMConfig` already.
- **Their git-provider abstraction.** We just need the diff text;
  callers already pull it via `gh pr diff`.
- **The multi-patch large-PR handling.** Too complex for v1; the bail
  path is fine until someone hits it.
- **The YAML-as-output format.** Our `LLMClient` already supports JSON
  mode for OpenAI-compatible endpoints — use that.

## Our improvement angle

- **Tighter scope:** one tool (`llm_review`) not seven. PR-Agent's
  surface is huge.
- **Falls back gracefully** to templated review when no LLM is
  configured — we already do that for replay analysis; same pattern.
- **PII redaction before sending diff to LLM.** PR-Agent leaves that
  to the user. We have `redact_sensitive_text` from PR #114 and
  should apply it.
- **Hard 32k token input cap** with explicit bail; PR-Agent's
  compression loop is impressive but the bail is honest for v1.
- **Caches the (`diff_sha256`, `model`) result** so a `gh pr` retry
  doesn't double-burn tokens.
- **Output flows through our existing `qa_incident` bridge** —
  PR-Agent's findings go straight to PR comments. Ours can also file
  qa_incidents (high-severity issues) so `retrace qa list` shows them.
