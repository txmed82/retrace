"""LLM-powered PR review.

The companion to `pr_review.py` (the templated review): given a unified
diff + a `PRReviewAnalysis`, ask an LLM to produce a structured summary,
walkthrough, inline suggestions, and risk notes.

Design follows the takeaways from `docs/study-notes/pr-agent.md`:

  * Diff is rendered with `__new hunk__` / `__old hunk__` sections and
    line numbers annotated next to new lines, so the model can emit
    `(file, start_line, end_line)` triples that map back to GitHub's
    PR-comment line indexing.
  * The system prompt declares a JSON schema for the output (Retrace's
    `LLMClient.chat_json` already runs in JSON mode for
    OpenAI-compatible endpoints).
  * Diffs over `max_tokens_per_chunk` are split on file boundaries;
    each chunk is reviewed separately and merged. A hard 32k input
    token cap bails with an explicit reason.
  * The diff is run through `redact_sensitive_text` before it hits the
    LLM — we don't want passwords or bearer tokens in someone's
    OpenAI logs.
  * Results are cached by `(sha256(diff), model)` so a retry doesn't
    double-burn tokens.

This module deliberately stays a single function (`llm_review`). Wiring
into the CLI happens in `commands/review.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Optional

from retrace.llm.client import LLMClient
from retrace.qa_incidents import redact_sensitive_text


log = logging.getLogger(__name__)


PROMPT_VERSION = "llm_pr_review/v2"

# Conservative defaults that fit in even small context windows; users
# with a 200k-token model can override via `llm_review(...)` kwargs.
DEFAULT_CHUNK_TOKEN_BUDGET = 6_000
DEFAULT_TOTAL_TOKEN_CAP = 32_000

# Final inline-suggestion cap applied after merge + line-validity filter.
# Three is the same number the system prompt asks for — we enforce it
# here because the model often misses the cap on multi-file PRs.
DEFAULT_MAX_INLINE_SUGGESTIONS = 3
DEFAULT_MAX_RISK_NOTES = 5


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InlineSuggestion:
    path: str
    line: int
    body: str
    suggested_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LLMReviewResult:
    summary: str = ""
    walkthrough: list[str] = field(default_factory=list)
    inline_suggestions: list[InlineSuggestion] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    chunks: int = 0
    diff_too_large: bool = False
    error: str = ""
    # P3.5 cost-visibility fields. Estimated chars/4 from the
    # redacted prompt + response text — directionally correct, not
    # audit-grade. See `retrace.llm_pricing` for the price table.
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    @property
    def is_empty(self) -> bool:
        """`True` when there's literally nothing to show.

        Note: a `diff_too_large` skip or a captured `error` is still
        worth surfacing in the PR comment (the user needs to know we
        looked but stopped), so those count as non-empty.
        """
        return not (
            self.summary
            or self.walkthrough
            or self.inline_suggestions
            or self.risk_notes
            or self.diff_too_large
            or bool(self.error.strip())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "walkthrough": list(self.walkthrough),
            "inline_suggestions": [s.to_dict() for s in self.inline_suggestions],
            "risk_notes": list(self.risk_notes),
            "model": self.model,
            "prompt_version": self.prompt_version,
            "chunks": self.chunks,
            "diff_too_large": self.diff_too_large,
            "error": self.error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
        }

    def to_markdown(self) -> str:
        """Render as the "Retrace LLM review" section of a PR comment."""
        lines: list[str] = []
        if self.summary:
            lines.append("### Summary")
            lines.append("")
            lines.append(self.summary.strip())
            lines.append("")
        if self.walkthrough:
            lines.append("### Walkthrough")
            for item in self.walkthrough:
                lines.append(f"- {item}")
            lines.append("")
        if self.inline_suggestions:
            lines.append("### Inline suggestions")
            for sug in self.inline_suggestions:
                lines.append(f"- `{sug.path}:{sug.line}` — {sug.body}")
                if sug.suggested_code:
                    lines.append("")
                    lines.append("  ```suggestion")
                    for sub in sug.suggested_code.splitlines():
                        lines.append(f"  {sub}")
                    lines.append("  ```")
            lines.append("")
        if self.risk_notes:
            lines.append("### Risk notes")
            for r in self.risk_notes:
                lines.append(f"- {r}")
            lines.append("")
        if self.diff_too_large:
            lines.append(
                f"> _Skipped LLM review: diff exceeded the {DEFAULT_TOTAL_TOKEN_CAP}-token cap._"
            )
        return "\n".join(lines).rstrip() + ("\n" if lines else "")


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate — 4 chars per token is the OpenAI rule of
    thumb. Good enough for budget gating; we don't need a real
    tokenizer here."""
    return max(1, len(text or "") // 4)


# ---------------------------------------------------------------------------
# Diff parsing + chunking
# ---------------------------------------------------------------------------


_FILE_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+) b/(?P<b>.+)$", re.MULTILINE)


def _split_diff_by_file(diff_text: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, file_diff_text) pairs.

    File boundaries are `diff --git a/X b/Y` markers (canonical git
    output). When the file header is missing (some tools), we fall
    back to splitting on `+++ b/` lines.
    """
    matches = list(_FILE_HEADER_RE.finditer(diff_text))
    if not matches:
        # Fallback: single chunk
        return [("", diff_text)] if diff_text.strip() else []
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        chunk = diff_text[start:end]
        # Prefer the `b/` (post-image) path
        path = m.group("b")
        out.append((path, chunk))
    return out


def _extract_added_lines(diff_text: str) -> dict[str, set[int]]:
    """Map `path -> set of new-side line numbers actually present` in the
    diff.

    Used to filter out hallucinated inline suggestions: any `(path,
    line)` not in this map points at code the model is making up.

    Both `+` (added) and ` ` (context) new-side lines are valid targets
    for an inline comment — GitHub's PR-comment API accepts a comment
    on any line that appears on the new side of the diff. `-` (removed)
    lines don't have a new-side line number, so they don't count.
    """
    by_path: dict[str, set[int]] = {}
    current_path = ""
    current_new_line = 0
    in_hunk = False
    for raw in diff_text.splitlines():
        m_file = _FILE_HEADER_RE.match(raw)
        if m_file:
            current_path = m_file.group("b")
            by_path.setdefault(current_path, set())
            in_hunk = False
            continue
        if raw.startswith("+++ b/"):
            # Some tools emit `+++ b/X` without the `diff --git` header.
            current_path = raw[len("+++ b/") :].strip()
            by_path.setdefault(current_path, set())
            in_hunk = False
            continue
        if raw.startswith("@@"):
            in_hunk = True
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            current_new_line = int(m.group(1)) if m else 0
            continue
        if not in_hunk or not current_path:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            by_path[current_path].add(current_new_line)
            current_new_line += 1
        elif raw.startswith(" "):
            by_path[current_path].add(current_new_line)
            current_new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            # removed line — no new-side number
            continue
        else:
            continue
    return by_path


def _filter_suggestions_against_diff(
    suggestions: list[InlineSuggestion],
    valid_lines: dict[str, set[int]],
) -> list[InlineSuggestion]:
    """Drop suggestions whose `(path, line)` doesn't exist in the diff.

    This is the cheap defense against the model inventing line numbers.
    It does NOT validate that the body refers to the right code — that's
    the self-critique pass's job.
    """
    if not valid_lines:
        # No diff structure to validate against — accept what we got.
        return list(suggestions)
    out: list[InlineSuggestion] = []
    for sug in suggestions:
        # Be lenient about leading `./` or `a/`/`b/` prefixes the model
        # sometimes glues on.
        candidates = {sug.path, sug.path.lstrip("./"), sug.path.removeprefix("a/"),
                      sug.path.removeprefix("b/")}
        match_path = next((p for p in candidates if p in valid_lines), None)
        if match_path is None:
            log.debug("dropped suggestion: path %r not in diff", sug.path)
            continue
        if sug.line not in valid_lines[match_path]:
            log.debug(
                "dropped suggestion: %s:%d not on new side of diff",
                sug.path,
                sug.line,
            )
            continue
        # Re-root to the canonical path so the renderer is consistent.
        if match_path != sug.path:
            sug = replace(sug, path=match_path)
        out.append(sug)
    return out


def _cap_suggestions(
    suggestions: list[InlineSuggestion],
    *,
    limit: int,
) -> list[InlineSuggestion]:
    """Keep at most `limit` suggestions, preferring ones with an
    `suggested_code` block (more actionable for the author).

    Sort is stable on (has_code DESC, original order ASC) so the model's
    own ordering wins ties.
    """
    if limit <= 0 or len(suggestions) <= limit:
        return suggestions
    indexed = list(enumerate(suggestions))
    indexed.sort(key=lambda pair: (0 if pair[1].suggested_code.strip() else 1, pair[0]))
    return [s for _, s in indexed[:limit]]


def _annotate_new_hunk_line_numbers(file_diff: str) -> str:
    """Rewrite each hunk header so the new-side line numbers appear
    next to additions, matching PR-Agent's `__new hunk__` format.

    Input form:

        @@ -10,5 +20,7 @@ ctx
         unchanged
        -removed
        +added

    Output form keeps the original diff for grep-friendliness AND
    appends a `__new hunk__ (line N)` annotation per `+` line — the
    LLM can quote `start_line` / `end_line` accurately.
    """
    out_lines: list[str] = []
    current_new_line = 0
    in_hunk = False
    for raw in file_diff.splitlines():
        if raw.startswith("@@"):
            in_hunk = True
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                current_new_line = int(m.group(1))
            out_lines.append(raw)
            continue
        if not in_hunk:
            out_lines.append(raw)
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            out_lines.append(f"{current_new_line:>5}: {raw}")
            current_new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            # removed lines don't advance the new-side counter
            out_lines.append(f"     : {raw}")
        elif raw.startswith(" "):
            out_lines.append(f"{current_new_line:>5}: {raw}")
            current_new_line += 1
        else:
            out_lines.append(raw)
    return "\n".join(out_lines)


def _chunk_files(
    files: list[tuple[str, str]],
    *,
    max_tokens_per_chunk: int,
) -> list[list[tuple[str, str]]]:
    """Greedy pack files into chunks under `max_tokens_per_chunk`.

    Files that are themselves over budget go in their own chunk
    (oversize is the LLM's problem). We never split a single file.
    """
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_tokens = 0
    for path, body in files:
        annotated = _annotate_new_hunk_line_numbers(body)
        tokens = _estimate_tokens(annotated)
        if current and current_tokens + tokens > max_tokens_per_chunk:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append((path, annotated))
        current_tokens += tokens
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """\
You are Retrace's PR reviewer. You read unified-diff hunks and produce
constructive, terse feedback for the PR author.

Focus only on changes introduced by this PR (lines starting with `+`).
Do not flag stylistic preferences or repeat what the diff obviously
says. When uncertain, prefer not flagging over guessing.

The diff is annotated with line numbers on the new side of each hunk,
in the format `<line>: <diff_line>`. Use those numbers when emitting
inline suggestions.

Return a single JSON object matching this schema (omit keys when you
have nothing to say for that section):

{
  "summary": "2-4 sentence summary of what the PR does and the
              shape of the change.",
  "walkthrough": [
    "One bullet per meaningfully changed file or area of behaviour."
  ],
  "inline_suggestions": [
    {
      "path": "src/foo/bar.py",
      "line": 42,
      "body": "Concrete, actionable note. One or two sentences.",
      "suggested_code": "optional replacement line(s); leave empty if
                         not a simple textual replacement"
    }
  ],
  "risk_notes": [
    "High-severity concerns: security, data loss, correctness in a
     stated scenario. Be specific."
  ]
}

Rules:
- Never invent file paths or line numbers — only use what appears in
  the diff above.
- Prefer 0-3 inline suggestions over a long noisy list.
- If the diff introduces no real concerns, return only `summary` and
  `walkthrough`. An empty `inline_suggestions` + `risk_notes` is fine.
- Don't quote secrets, env-var values, or PII from the diff.
- JSON must be syntactically valid. No prose outside the JSON object.
"""


def _build_user_message(
    chunk_files: list[tuple[str, str]],
    *,
    analysis_hint: str = "",
) -> str:
    parts: list[str] = []
    if analysis_hint:
        parts.append("Retrace already detected the following context:")
        parts.append(analysis_hint)
        parts.append("")
    parts.append("Diff:")
    for path, body in chunk_files:
        parts.append("---")
        parts.append(f"File: {path or '<unknown>'}")
        parts.append(body.rstrip())
    parts.append("---")
    return "\n".join(parts)


def _analysis_hint(analysis: Any, *, prior_review_summary: str = "") -> str:
    """Tiny context summary so the LLM knows what Retrace already saw.

    `prior_review_summary` (item 4) is folded in verbatim when present —
    it carries notes from prior LLM reviews on files touched in this
    PR, so we don't keep re-flagging the same issue across reviews.
    """
    lines: list[str] = []
    if analysis is not None:
        if getattr(analysis, "affected_flows", None):
            flows = ", ".join(
                getattr(f, "name", "") for f in analysis.affected_flows[:5]
            )
            if flows:
                lines.append(f"Affected flows (top 5): {flows}")
        if getattr(analysis, "prior_failures", None):
            priors = ", ".join(
                getattr(p, "public_id", "") for p in analysis.prior_failures[:5]
            )
            if priors:
                lines.append(f"Prior failures touching the diff: {priors}")
        if getattr(analysis, "missing_tests", None):
            misses = ", ".join(
                getattr(m, "flow", "") for m in analysis.missing_tests[:5]
            )
            if misses:
                lines.append(f"Flows lacking coverage: {misses}")
    prior_review_summary = (prior_review_summary or "").strip()
    if prior_review_summary:
        lines.append("Prior Retrace review notes on these files:")
        lines.append(prior_review_summary)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------


def _coerce_result(raw: dict[str, Any], *, model: str) -> LLMReviewResult:
    summary = str(raw.get("summary") or "").strip()

    walkthrough_raw = raw.get("walkthrough") or []
    walkthrough = [str(item).strip() for item in walkthrough_raw if str(item).strip()]

    inline_raw = raw.get("inline_suggestions") or []
    inline: list[InlineSuggestion] = []
    for item in inline_raw:
        if not isinstance(item, dict):
            continue
        try:
            line_val = int(item.get("line") or 0)
        except (TypeError, ValueError):
            continue
        path = str(item.get("path") or "").strip()
        body = str(item.get("body") or "").strip()
        if not path or not body or line_val <= 0:
            continue
        inline.append(
            InlineSuggestion(
                path=path,
                line=line_val,
                body=body,
                suggested_code=str(item.get("suggested_code") or "").strip(),
            )
        )

    risks_raw = raw.get("risk_notes") or []
    risks = [str(item).strip() for item in risks_raw if str(item).strip()]

    return LLMReviewResult(
        summary=summary,
        walkthrough=walkthrough,
        inline_suggestions=inline,
        risk_notes=risks,
        model=model,
    )


def _merge_results(results: list[LLMReviewResult], *, model: str) -> LLMReviewResult:
    if not results:
        return LLMReviewResult(model=model)
    if len(results) == 1:
        return results[0]
    summary = " ".join(r.summary for r in results if r.summary).strip()
    walkthrough: list[str] = []
    inline: list[InlineSuggestion] = []
    risks: list[str] = []
    for r in results:
        walkthrough.extend(r.walkthrough)
        inline.extend(r.inline_suggestions)
        risks.extend(r.risk_notes)
    return LLMReviewResult(
        summary=summary,
        walkthrough=walkthrough,
        inline_suggestions=inline,
        risk_notes=risks,
        model=model,
        chunks=len(results),
    )


# ---------------------------------------------------------------------------
# Optional self-critique pass
# ---------------------------------------------------------------------------


_SELF_CRITIQUE_SYSTEM = """\
You are Retrace's PR-review critic. You are given the inline suggestions
and risk notes another model produced for this PR. Your job is to rank
them and drop noise.

Return a single JSON object:

{
  "kept_suggestion_indices": [0, 3, 5],     // up to 3 indices, ordered most-important first
  "kept_risk_indices":       [1, 0],        // up to 5 indices, ordered most-important first
  "rationale": "one short line, optional"
}

Rules:
- Drop anything that's stylistic-only, obvious from the diff, or
  speculative (e.g. "consider adding tests" with no specific scenario).
- Prefer items that name a concrete failure mode (security, data loss,
  null deref, regression against named flow).
- Indices refer to the lists in the user message, 0-based.
- Return only the JSON object — no prose.
"""


def _self_critique(
    *,
    client: LLMClient,
    suggestions: list[InlineSuggestion],
    risk_notes: list[str],
    max_suggestions: int,
    max_risks: int,
) -> tuple[list[InlineSuggestion], list[str]]:
    """Ask the LLM to rank/dedupe.

    Falls back to the input on any error — the critique pass must never
    degrade the result (worst case is "same as before").
    """
    if not suggestions and not risk_notes:
        return suggestions, risk_notes
    user_parts: list[str] = []
    if suggestions:
        user_parts.append("Inline suggestions (index — path:line — body):")
        for i, s in enumerate(suggestions):
            body = s.body.replace("\n", " ").strip()
            user_parts.append(f"  {i} — {s.path}:{s.line} — {body[:240]}")
        user_parts.append("")
    if risk_notes:
        user_parts.append("Risk notes (index — body):")
        for i, r in enumerate(risk_notes):
            user_parts.append(f"  {i} — {r[:240]}")
        user_parts.append("")
    user_parts.append(
        f"Keep at most {max_suggestions} suggestion(s) and {max_risks} risk note(s)."
    )
    user_msg = "\n".join(user_parts)

    try:
        raw = client.chat_json(
            system=_SELF_CRITIQUE_SYSTEM,
            user=user_msg,
            temperature=0.1,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("self-critique pass failed; using uncritiqued result: %s", exc)
        return suggestions, risk_notes
    if not isinstance(raw, dict):
        return suggestions, risk_notes

    def _pick(idx_list: Any, source: list, cap: int) -> list:
        if not isinstance(idx_list, list):
            return source[:cap]
        seen: set[int] = set()
        out: list = []
        for v in idx_list:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if i < 0 or i >= len(source) or i in seen:
                continue
            seen.add(i)
            out.append(source[i])
            if len(out) >= cap:
                break
        return out

    kept_suggestions = _pick(raw.get("kept_suggestion_indices"), suggestions, max_suggestions)
    kept_risks = _pick(raw.get("kept_risk_indices"), risk_notes, max_risks)
    return kept_suggestions, kept_risks


# ---------------------------------------------------------------------------
# In-memory cache (sha256(diff) + model -> result). Process-local.
# ---------------------------------------------------------------------------


_CACHE: dict[str, LLMReviewResult] = {}


def _cache_key(diff_text: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(diff_text.encode("utf-8"))
    return h.hexdigest()


def clear_cache() -> None:
    """Test helper / explicit reset."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def llm_review(
    *,
    diff_text: str,
    analysis: Any = None,
    llm_client: Optional[LLMClient] = None,
    max_tokens_per_chunk: int = DEFAULT_CHUNK_TOKEN_BUDGET,
    total_token_cap: int = DEFAULT_TOTAL_TOKEN_CAP,
    max_inline_suggestions: int = DEFAULT_MAX_INLINE_SUGGESTIONS,
    max_risk_notes: int = DEFAULT_MAX_RISK_NOTES,
    enable_self_critique: bool = False,
    prior_review_summary: str = "",
) -> LLMReviewResult:
    """Run an LLM review over `diff_text`.

    Returns an empty `LLMReviewResult` when no LLM client is provided,
    so callers can opt-in without an LLM key gracefully.

    Quality guardrails applied to the merged result before returning:

      1. **Line-validity filter** — inline suggestions whose
         `(path, line)` doesn't appear on the new side of the diff are
         dropped. Defends against hallucinated line numbers.
      2. **Suggestion cap** — at most `max_inline_suggestions` and
         `max_risk_notes` survive. Suggestions with a concrete
         `suggested_code` block win ties.
      3. **Optional self-critique** — when `enable_self_critique=True`
         and we have more findings than the cap, one extra LLM call
         ranks/dedupes the candidates before the deterministic cap.
      4. **Prior-review summary** — `prior_review_summary` is folded
         into the analysis hint so the model knows what Retrace
         already flagged on these files in earlier PRs.
    """
    if llm_client is None:
        return LLMReviewResult()

    if not diff_text or not diff_text.strip():
        return LLMReviewResult(model=llm_client.cfg.model)

    # Cheap size check first — `redact_sensitive_text` has regexes that
    # are O(n) per pattern over the full input, so running them on a
    # 500k-char diff just to bail is wasteful. Token-cap-bail uses the
    # raw size and skips redaction.
    raw_tokens = _estimate_tokens(diff_text)
    if raw_tokens > total_token_cap:
        result = LLMReviewResult(
            model=llm_client.cfg.model,
            diff_too_large=True,
            error=(
                f"Diff is ~{raw_tokens} tokens, over the {total_token_cap}-token "
                "cap. Skipping LLM review for this PR."
            ),
        )
        # Cache the bail too so a retry doesn't pay the size check again.
        _CACHE[_cache_key(diff_text[:1024], llm_client.cfg.model)] = result
        return result

    # PII redaction BEFORE the diff leaves the host. PR-Agent leaves
    # this to the user; we don't.
    safe_diff = redact_sensitive_text(diff_text, max_len=10 * total_token_cap)

    cache_key = _cache_key(
        safe_diff
        + f"|crit={int(enable_self_critique)}|cap={max_inline_suggestions},{max_risk_notes}"
        + f"|prior={prior_review_summary}",
        llm_client.cfg.model,
    )
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    total_tokens = _estimate_tokens(safe_diff)
    if total_tokens > total_token_cap:
        # Honest bail rather than PR-Agent-style aggressive compression.
        result = LLMReviewResult(
            model=llm_client.cfg.model,
            diff_too_large=True,
            error=(
                f"Diff is ~{total_tokens} tokens, over the {total_token_cap}-token "
                "cap. Skipping LLM review for this PR."
            ),
        )
        _CACHE[cache_key] = result
        return result

    files = _split_diff_by_file(safe_diff)
    if not files:
        return LLMReviewResult(model=llm_client.cfg.model)

    chunks = _chunk_files(files, max_tokens_per_chunk=max_tokens_per_chunk)
    hint = _analysis_hint(analysis, prior_review_summary=prior_review_summary)

    chunk_results: list[LLMReviewResult] = []
    for chunk_files_list in chunks:
        user_msg = _build_user_message(chunk_files_list, analysis_hint=hint)
        try:
            raw = llm_client.chat_json(
                system=_SYSTEM_PROMPT,
                user=user_msg,
                temperature=0.2,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("llm_pr_review: chunk request failed: %s", exc)
            chunk_results.append(
                LLMReviewResult(model=llm_client.cfg.model, error=str(exc))
            )
            continue
        if not isinstance(raw, dict):
            log.warning("llm_pr_review: chunk returned non-dict: %r", raw)
            continue
        chunk_results.append(_coerce_result(raw, model=llm_client.cfg.model))

    merged = _merge_results(chunk_results, model=llm_client.cfg.model)

    # P3.5 — estimate token usage and dollar cost from the prompt
    # text we sent and the response text we got back. chars/4 is
    # directionally correct; real provider `usage` blocks would
    # require deeper plumbing into the LLM client surface (other
    # callers don't need it). See `retrace.llm_pricing`.
    from retrace.llm_pricing import (
        estimate_cost_usd as _estimate_cost_usd,
        estimate_tokens_from_text as _estimate_tokens_from_text,
    )

    prompt_tokens = (
        _estimate_tokens_from_text(_SYSTEM_PROMPT)
        + _estimate_tokens_from_text(safe_diff)
        + _estimate_tokens_from_text(hint)
    )
    response_text = " ".join(
        [merged.summary]
        + list(merged.walkthrough)
        + [s.body + " " + s.suggested_code for s in merged.inline_suggestions]
        + list(merged.risk_notes)
    )
    response_tokens = _estimate_tokens_from_text(response_text)
    estimated_cost = _estimate_cost_usd(
        model=merged.model,
        input_tokens=prompt_tokens,
        output_tokens=response_tokens,
    )
    merged = replace(
        merged,
        input_tokens=prompt_tokens,
        output_tokens=response_tokens,
        estimated_cost_usd=estimated_cost,
    )

    # (1) Line-validity filter against the redacted diff (the same lines
    # the LLM saw).
    valid_lines = _extract_added_lines(safe_diff)
    filtered_suggestions = _filter_suggestions_against_diff(
        merged.inline_suggestions, valid_lines
    )

    # (3) Optional self-critique — only run if there's actually overflow,
    # otherwise we're paying a second LLM call for nothing.
    risk_notes_after = list(merged.risk_notes)
    suggestions_after = filtered_suggestions
    overflow = (
        len(suggestions_after) > max_inline_suggestions
        or len(risk_notes_after) > max_risk_notes
    )
    if enable_self_critique and overflow:
        suggestions_after, risk_notes_after = _self_critique(
            client=llm_client,
            suggestions=suggestions_after,
            risk_notes=risk_notes_after,
            max_suggestions=max_inline_suggestions,
            max_risks=max_risk_notes,
        )

    # (2) Deterministic cap — applied even after self-critique as a
    # belt-and-braces guarantee.
    suggestions_after = _cap_suggestions(suggestions_after, limit=max_inline_suggestions)
    risk_notes_after = risk_notes_after[:max_risk_notes]

    merged = replace(
        merged,
        inline_suggestions=suggestions_after,
        risk_notes=risk_notes_after,
    )

    _CACHE[cache_key] = merged
    return merged


def llm_review_to_pr_comment(result: LLMReviewResult) -> str:
    """Convenience for callers that want a paste-ready comment block."""
    return result.to_markdown()


# Surface a JSON-serialisable form for tests / `--json` output.
def llm_review_to_json(result: LLMReviewResult) -> str:
    return json.dumps(result.to_dict(), indent=2)
