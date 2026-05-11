"""Tests for the LLM-driven PR review (`llm_pr_review.py`)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from retrace.llm_pr_review import (
    DEFAULT_MAX_INLINE_SUGGESTIONS,
    DEFAULT_TOTAL_TOKEN_CAP,
    InlineSuggestion,
    LLMReviewResult,
    _annotate_new_hunk_line_numbers,
    _cap_suggestions,
    _chunk_files,
    _estimate_tokens,
    _extract_added_lines,
    _filter_suggestions_against_diff,
    _split_diff_by_file,
    clear_cache,
    llm_review,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# NOTE: the apiKey value is intentionally a long realistic-shape token
# (>= 32 chars containing `-`) so it exercises the `_LONG_TOKEN_RE`
# branch of `redact_sensitive_text`. Shorter strings legitimately slip
# through that branch — that's a known redactor limitation tracked in
# roadmap follow-ups, not a bug in this test.
_TWO_FILE_DIFF = """\
diff --git a/server/routes/auth.ts b/server/routes/auth.ts
index 0000001..1111111 100644
--- a/server/routes/auth.ts
+++ b/server/routes/auth.ts
@@ -10,3 +10,6 @@ router.post('/api/login', async (req, res) => {
   const user = await users.find(req.body.email);
+  if (!user) {
+    return res.status(404).json({ error: 'not found' });
+  }
   res.json({ token: signJwt(user) });
 });
diff --git a/client/src/pages/Login.tsx b/client/src/pages/Login.tsx
index 2222222..3333333 100644
--- a/client/src/pages/Login.tsx
+++ b/client/src/pages/Login.tsx
@@ -5,2 +5,5 @@ export function Login() {
   const [email, setEmail] = useState('');
+  const [pw, setPw] = useState('');
+  // hardcoded for debug; remove before merging
+  const apiKey = 'sk-DEBUG-AbCdEf0123456789AbCdEf0123456789';
   return <form>...</form>;
 }
"""


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


def _stub_client(*, response_payloads):
    """Build a stub `LLMClient` that returns the supplied payload(s) in
    order on consecutive `chat_json` calls."""
    client = MagicMock()
    client.cfg.model = "test-model"
    responses = list(response_payloads)
    call_log: list[tuple[str, str]] = []

    def _chat_json(*, system, user, temperature=0.2):
        call_log.append((system, user))
        return responses.pop(0)

    client.chat_json.side_effect = _chat_json
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client._calls = call_log
    return client


# ---------------------------------------------------------------------------
# Helpers (parsing, chunking, annotation)
# ---------------------------------------------------------------------------


def test_split_diff_by_file_two_files():
    files = _split_diff_by_file(_TWO_FILE_DIFF)
    assert [p for p, _ in files] == [
        "server/routes/auth.ts",
        "client/src/pages/Login.tsx",
    ]
    # Each chunk still contains the file header so chunks are
    # individually re-rooted.
    assert "diff --git a/server/routes/auth.ts" in files[0][1]
    assert "diff --git a/client/src/pages/Login.tsx" in files[1][1]


def test_annotate_line_numbers_marks_new_side_additions():
    annotated = _annotate_new_hunk_line_numbers(_TWO_FILE_DIFF)
    # The hunk header `@@ -10,3 +10,6 @@` puts new-side at line 10.
    # First line in the hunk is a context line at 10; the first added
    # line is the very next one at line 11.
    assert "   11: +  if (!user) {" in annotated
    # Unchanged context keeps a line number; the original `index` /
    # `@@` headers are not annotated.
    assert "@@ -10,3 +10,6 @@" in annotated


def test_chunk_files_packs_under_budget():
    files = _split_diff_by_file(_TWO_FILE_DIFF)
    chunks = _chunk_files(files, max_tokens_per_chunk=10_000)
    assert len(chunks) == 1  # both fit
    chunks = _chunk_files(files, max_tokens_per_chunk=20)  # tiny budget
    assert len(chunks) == 2  # one per file


def test_estimate_tokens_rough_4_chars():
    assert _estimate_tokens("") == 1  # min 1
    assert _estimate_tokens("a" * 400) == 100


# ---------------------------------------------------------------------------
# llm_review behaviour
# ---------------------------------------------------------------------------


def test_llm_review_returns_empty_when_no_client():
    result = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=None)
    assert result.is_empty
    assert result.model == ""


def test_llm_review_happy_path_returns_structured_result():
    client = _stub_client(
        response_payloads=[
            {
                "summary": "Adds 404 handling on login; introduces dead key.",
                "walkthrough": [
                    "server: returns 404 when user is missing",
                    "client: adds password state and a hardcoded apiKey",
                ],
                "inline_suggestions": [
                    {
                        "path": "client/src/pages/Login.tsx",
                        "line": 7,
                        "body": "Remove the hardcoded `apiKey` before merging.",
                        "suggested_code": "",
                    }
                ],
                "risk_notes": [
                    "Sensitive: hardcoded `apiKey` checked into the client bundle.",
                ],
            }
        ]
    )
    result = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client)

    assert not result.is_empty
    assert "404" in result.summary
    assert len(result.walkthrough) == 2
    assert len(result.inline_suggestions) == 1
    sug = result.inline_suggestions[0]
    assert isinstance(sug, InlineSuggestion)
    assert sug.path == "client/src/pages/Login.tsx"
    assert sug.line == 7
    assert "apiKey" in sug.body
    assert "hardcoded" in result.risk_notes[0].lower()
    assert result.model == "test-model"

    # Markdown shows up cleanly.
    md = result.to_markdown()
    assert "### Summary" in md
    assert "### Inline suggestions" in md
    assert "Login.tsx:7" in md


def test_llm_review_chunking_calls_llm_per_chunk():
    """A diff that exceeds `max_tokens_per_chunk` is split on file
    boundaries; each chunk gets its own request, and results are
    merged."""
    client = _stub_client(
        response_payloads=[
            {"summary": "first chunk summary", "walkthrough": ["server"]},
            {"summary": "second chunk summary", "walkthrough": ["client"]},
        ]
    )
    result = llm_review(
        diff_text=_TWO_FILE_DIFF,
        llm_client=client,
        max_tokens_per_chunk=20,  # forces 2 chunks
        total_token_cap=10_000,
    )
    assert client.chat_json.call_count == 2
    assert result.chunks == 2
    assert "first chunk summary" in result.summary
    assert "second chunk summary" in result.summary
    assert set(result.walkthrough) == {"server", "client"}


def test_llm_review_bails_when_total_token_cap_exceeded():
    """A diff over `total_token_cap` skips the LLM entirely and
    reports the skip."""
    big = "diff --git a/x b/x\n+" + ("X" * 500_000)
    client = _stub_client(response_payloads=[])
    result = llm_review(
        diff_text=big,
        llm_client=client,
        total_token_cap=1000,
    )
    assert result.diff_too_large
    assert "32000" in result.error or "1000" in result.error
    assert client.chat_json.call_count == 0


def test_llm_review_redacts_secrets_before_sending_to_llm():
    """The diff above includes `sk-DEBUG-not-a-real-key`. The redactor
    must mask it before the LLM ever sees it."""
    client = _stub_client(
        response_payloads=[{"summary": "ok", "walkthrough": []}]
    )
    llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client)

    sent_user = client._calls[0][1]
    assert "sk-DEBUG-AbCdEf0123456789AbCdEf0123456789" not in sent_user
    assert "<redacted>" in sent_user


def test_llm_review_caches_repeat_calls_for_same_diff_model():
    """Two identical calls = one LLM request."""
    client = _stub_client(
        response_payloads=[{"summary": "first", "walkthrough": []}]
    )
    r1 = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client)
    r2 = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client)
    assert r1.summary == "first"
    assert r2.summary == "first"
    assert client.chat_json.call_count == 1


def test_llm_review_drops_malformed_inline_suggestions():
    """Suggestions missing required fields are skipped, not crashed on.

    Uses a real `(path, line)` from `_TWO_FILE_DIFF` for the keeper —
    `server/routes/auth.ts:11` is the first `+` line of the auth hunk
    (`@@ -10,3 +10,6 @@` + 1 context line). The line-validity filter
    (item 1 of the P0.1 follow-up) is also exercised here: malformed
    rows are dropped at parse time, and the keeper must survive the
    diff-grounded filter too.
    """
    real_path = "server/routes/auth.ts"
    client = _stub_client(
        response_payloads=[
            {
                "summary": "x",
                "inline_suggestions": [
                    {"path": real_path, "line": 11, "body": "fine"},
                    {"path": "", "line": 11, "body": "no path"},   # dropped
                    {"path": real_path, "line": 0, "body": "bad line"},  # dropped
                    {"path": real_path, "line": 11, "body": ""},  # dropped
                    {"path": real_path, "line": "not-int", "body": "x"},  # dropped
                ],
            }
        ]
    )
    result = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client)
    assert len(result.inline_suggestions) == 1
    assert result.inline_suggestions[0].path == real_path
    assert result.inline_suggestions[0].line == 11


def test_llm_review_handles_chat_json_failure_per_chunk():
    """If chunk 2 raises, chunk 1's result is preserved and reported."""
    client = MagicMock()
    client.cfg.model = "test-model"
    client.chat_json.side_effect = [
        {"summary": "good", "walkthrough": []},
        Exception("LLM 500"),
    ]
    result = llm_review(
        diff_text=_TWO_FILE_DIFF,
        llm_client=client,
        max_tokens_per_chunk=20,  # 2 chunks
        total_token_cap=10_000,
    )
    # First chunk's content survives; the second's error is captured.
    assert result.summary == "good"
    assert result.chunks == 2


# ---------------------------------------------------------------------------
# Sanity: result shape + markdown render
# ---------------------------------------------------------------------------


def test_result_is_empty_when_truly_empty():
    assert LLMReviewResult().is_empty
    assert not LLMReviewResult(summary="x").is_empty
    # `diff_too_large` and `error` are user-visible outcomes — the
    # renderer must NOT skip them. (Regression for the renderer
    # gating bug that hid the "Skipped LLM review" notice.)
    assert not LLMReviewResult(diff_too_large=True).is_empty
    assert not LLMReviewResult(error="LLM 500").is_empty
    assert LLMReviewResult(error="   ").is_empty  # whitespace-only error doesn't count


def test_to_markdown_handles_diff_too_large_note():
    r = LLMReviewResult(diff_too_large=True, error="too big")
    md = r.to_markdown()
    assert "Skipped LLM review" in md
    assert str(DEFAULT_TOTAL_TOKEN_CAP) in md


def test_inline_suggestion_to_dict_round_trip():
    s = InlineSuggestion(path="a", line=1, body="b", suggested_code="c")
    assert s.to_dict() == {
        "path": "a",
        "line": 1,
        "body": "b",
        "suggested_code": "c",
    }


# ---------------------------------------------------------------------------
# P0.1 follow-up: line-validity filter, suggestion cap, self-critique, prior-review hint
# ---------------------------------------------------------------------------


def test_extract_added_lines_picks_up_added_and_context_lines():
    """Both `+` and ` ` count as new-side lines (GitHub lets you
    comment on context lines too); `-` lines do not."""
    valid = _extract_added_lines(_TWO_FILE_DIFF)
    # `server/routes/auth.ts` hunk: `@@ -10,3 +10,6 @@` then 1 ctx, 3 added, 1 ctx, 1 ctx.
    # Context line at 10; added at 11, 12, 13; trailing context at 14, 15.
    assert {10, 11, 12, 13, 14, 15} <= valid["server/routes/auth.ts"]
    # Removed lines should NOT appear (there are none in this fixture,
    # but the upper bound is what matters).
    assert 99 not in valid["server/routes/auth.ts"]
    # Login.tsx hunk: `@@ -5,2 +5,5 @@` — context 5, added 6/7/8, context 9.
    assert {5, 6, 7, 8, 9} <= valid["client/src/pages/Login.tsx"]


def test_filter_suggestions_drops_invented_lines_and_paths():
    valid = _extract_added_lines(_TWO_FILE_DIFF)
    suggestions = [
        InlineSuggestion(path="server/routes/auth.ts", line=11, body="real"),
        InlineSuggestion(path="server/routes/auth.ts", line=999, body="fake line"),
        InlineSuggestion(path="totally/made/up.py", line=1, body="fake path"),
        # Path prefix variants the model sometimes glues on — we accept.
        InlineSuggestion(path="b/server/routes/auth.ts", line=12, body="b-prefix"),
    ]
    kept = _filter_suggestions_against_diff(suggestions, valid)
    assert {s.body for s in kept} == {"real", "b-prefix"}
    # b/-prefix variant is re-rooted to the canonical path.
    paths = {s.path for s in kept}
    assert "server/routes/auth.ts" in paths
    assert "b/server/routes/auth.ts" not in paths


def test_filter_passes_through_when_diff_has_no_structure():
    """A diff we couldn't parse should not punish the model — we accept
    whatever it returned rather than dropping everything."""
    kept = _filter_suggestions_against_diff(
        [InlineSuggestion(path="x", line=1, body="ok")],
        valid_lines={},
    )
    assert len(kept) == 1


def test_cap_suggestions_prefers_actionable_with_code():
    s1 = InlineSuggestion(path="a", line=1, body="prose only")
    s2 = InlineSuggestion(path="a", line=2, body="with code", suggested_code="return None")
    s3 = InlineSuggestion(path="a", line=3, body="prose only")
    s4 = InlineSuggestion(path="a", line=4, body="more code", suggested_code="x = 1")
    capped = _cap_suggestions([s1, s2, s3, s4], limit=2)
    # Both winners must have suggested_code; ties broken by original order.
    assert [s.line for s in capped] == [2, 4]


def test_cap_suggestions_keeps_order_when_under_limit():
    s1 = InlineSuggestion(path="a", line=1, body="x")
    s2 = InlineSuggestion(path="a", line=2, body="y")
    assert _cap_suggestions([s1, s2], limit=5) == [s1, s2]


def test_llm_review_drops_hallucinated_line_numbers_end_to_end():
    """LLM returns 4 suggestions, only 1 lands on a real new-side line."""
    real_path = "server/routes/auth.ts"
    client = _stub_client(
        response_payloads=[
            {
                "summary": "x",
                "inline_suggestions": [
                    {"path": real_path, "line": 11, "body": "real"},
                    {"path": real_path, "line": 9999, "body": "invented line"},
                    {"path": "made-up.py", "line": 1, "body": "invented path"},
                    {"path": real_path, "line": 12, "body": "also real"},
                ],
            }
        ]
    )
    result = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client)
    bodies = {s.body for s in result.inline_suggestions}
    assert bodies == {"real", "also real"}


def test_llm_review_caps_inline_suggestions_at_default():
    """Default cap kicks in on overflow with no self-critique."""
    real = "server/routes/auth.ts"
    # 5 valid suggestions, default cap 3
    client = _stub_client(
        response_payloads=[
            {
                "summary": "x",
                "inline_suggestions": [
                    {"path": real, "line": 11, "body": "a"},
                    {"path": real, "line": 12, "body": "b", "suggested_code": "fix()"},
                    {"path": real, "line": 13, "body": "c"},
                    {"path": real, "line": 14, "body": "d", "suggested_code": "other()"},
                    {"path": real, "line": 15, "body": "e"},
                ],
            }
        ]
    )
    result = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client)
    assert len(result.inline_suggestions) == DEFAULT_MAX_INLINE_SUGGESTIONS
    # The two with suggested_code must be in the kept set.
    kept_bodies = {s.body for s in result.inline_suggestions}
    assert "b" in kept_bodies
    assert "d" in kept_bodies


def test_llm_review_caps_risk_notes():
    risks = [f"risk #{i}" for i in range(10)]
    client = _stub_client(
        response_payloads=[
            {"summary": "x", "inline_suggestions": [], "risk_notes": risks}
        ]
    )
    result = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client, max_risk_notes=4)
    assert len(result.risk_notes) == 4
    # Order preserved.
    assert result.risk_notes == risks[:4]


def test_llm_review_self_critique_off_by_default_no_extra_call():
    """With critique off, only one LLM call regardless of overflow."""
    real = "server/routes/auth.ts"
    payload = {
        "summary": "x",
        "inline_suggestions": [
            {"path": real, "line": 11 + i, "body": f"s{i}"} for i in range(4)
        ],
    }
    client = _stub_client(response_payloads=[payload])
    llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client)
    assert client.chat_json.call_count == 1


def test_llm_review_self_critique_runs_on_overflow_and_uses_indices():
    """With critique on AND overflow, an extra call ranks suggestions."""
    real = "server/routes/auth.ts"
    review_payload = {
        "summary": "x",
        "inline_suggestions": [
            {"path": real, "line": 11, "body": "noisy"},
            {"path": real, "line": 12, "body": "important", "suggested_code": "fix()"},
            {"path": real, "line": 13, "body": "also noisy"},
            {"path": real, "line": 14, "body": "second important"},
        ],
        "risk_notes": ["minor", "MAJOR", "tiny"],
    }
    # Critic keeps the 2nd suggestion (index 1) and the 'MAJOR' risk (index 1)
    critique_payload = {
        "kept_suggestion_indices": [1, 3],
        "kept_risk_indices": [1],
    }
    client = _stub_client(response_payloads=[review_payload, critique_payload])
    result = llm_review(
        diff_text=_TWO_FILE_DIFF,
        llm_client=client,
        enable_self_critique=True,
        max_inline_suggestions=2,
        max_risk_notes=1,
    )
    assert client.chat_json.call_count == 2
    bodies = [s.body for s in result.inline_suggestions]
    assert bodies == ["important", "second important"]
    assert result.risk_notes == ["MAJOR"]


def test_llm_review_self_critique_skipped_when_no_overflow():
    """No overflow → no critique call even with flag on."""
    real = "server/routes/auth.ts"
    payload = {
        "summary": "x",
        "inline_suggestions": [
            {"path": real, "line": 11, "body": "one"},
        ],
        "risk_notes": ["just one"],
    }
    client = _stub_client(response_payloads=[payload])
    result = llm_review(
        diff_text=_TWO_FILE_DIFF,
        llm_client=client,
        enable_self_critique=True,
        max_inline_suggestions=3,
        max_risk_notes=3,
    )
    assert client.chat_json.call_count == 1
    assert len(result.inline_suggestions) == 1


def test_llm_review_self_critique_failure_falls_back_to_uncritiqued():
    """If the critique LLM call raises, we keep the deterministic cap result."""
    from unittest.mock import MagicMock

    real = "server/routes/auth.ts"
    review_payload = {
        "summary": "x",
        "inline_suggestions": [
            {"path": real, "line": 11, "body": "a"},
            {"path": real, "line": 12, "body": "b"},
            {"path": real, "line": 13, "body": "c"},
            {"path": real, "line": 14, "body": "d"},
        ],
    }
    client = MagicMock()
    client.cfg.model = "test-model"
    client.chat_json.side_effect = [review_payload, Exception("critic 500")]
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    result = llm_review(
        diff_text=_TWO_FILE_DIFF,
        llm_client=client,
        enable_self_critique=True,
        max_inline_suggestions=2,
    )
    # Critic raised → fallback uses _cap_suggestions deterministically.
    assert len(result.inline_suggestions) == 2


def test_llm_review_prior_review_summary_appears_in_prompt():
    """`prior_review_summary` is folded into the analysis hint passed to the model."""
    client = _stub_client(
        response_payloads=[{"summary": "ok", "walkthrough": []}]
    )
    llm_review(
        diff_text=_TWO_FILE_DIFF,
        llm_client=client,
        prior_review_summary="- auth bypass risk flagged on /api/login  (PR #42)",
    )
    sent_user = client._calls[0][1]
    assert "Prior Retrace review notes on these files" in sent_user
    assert "auth bypass risk" in sent_user
    assert "PR #42" in sent_user


def test_llm_review_caches_separately_on_critique_and_cap_settings():
    """Two reviews with the same diff but different cap settings must
    NOT share a cache entry — otherwise turning the cap up would still
    return yesterday's truncated result."""
    real = "server/routes/auth.ts"
    payload = {
        "summary": "x",
        "inline_suggestions": [
            {"path": real, "line": 11 + i, "body": f"s{i}"} for i in range(5)
        ],
    }
    # Two payloads queued: each call should consume one (no cache hit).
    client = _stub_client(response_payloads=[dict(payload), dict(payload)])
    r1 = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client, max_inline_suggestions=2)
    r2 = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client, max_inline_suggestions=4)
    assert client.chat_json.call_count == 2
    assert len(r1.inline_suggestions) == 2
    assert len(r2.inline_suggestions) == 4
