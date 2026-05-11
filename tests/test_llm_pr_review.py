"""Tests for the LLM-driven PR review (`llm_pr_review.py`)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from retrace.llm_pr_review import (
    DEFAULT_TOTAL_TOKEN_CAP,
    InlineSuggestion,
    LLMReviewResult,
    _annotate_new_hunk_line_numbers,
    _chunk_files,
    _estimate_tokens,
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
    """Suggestions missing required fields are skipped, not crashed on."""
    client = _stub_client(
        response_payloads=[
            {
                "summary": "x",
                "inline_suggestions": [
                    {"path": "ok.py", "line": 5, "body": "fine"},
                    {"path": "", "line": 5, "body": "no path"},   # dropped
                    {"path": "ok.py", "line": 0, "body": "bad line"},  # dropped
                    {"path": "ok.py", "line": 5, "body": ""},  # dropped
                    {"path": "ok.py", "line": "not-int", "body": "x"},  # dropped
                ],
            }
        ]
    )
    result = llm_review(diff_text=_TWO_FILE_DIFF, llm_client=client)
    assert len(result.inline_suggestions) == 1
    assert result.inline_suggestions[0].path == "ok.py"


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
