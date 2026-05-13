"""P3.3 scenario 3 — LLM PR review on a fixture diff.

This one is in-process — the LLM client is a mock and the diff
path is pure, so there's no server to spin up. We're verifying
that the full `llm_review` pipeline (diff parsing, redaction,
chunking, prompt build, response coercion, line-validity filter,
suggestion cap, cost estimate) produces a well-formed result on a
known diff.

The other two e2e scenarios cover the HTTP wiring; this one
covers the long arm of `llm_pr_review.py` that the unit suite
otherwise only hits piecemeal.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from retrace.llm_pr_review import (
    LLMReviewResult,
    PROMPT_VERSION,
    llm_review,
)


_FIXTURE_DIFF = """diff --git a/server/auth.py b/server/auth.py
index 0000000..1111111 100644
--- a/server/auth.py
+++ b/server/auth.py
@@ -10,4 +10,7 @@ def authenticate(token: str) -> User | None:
     row = db.find_user(token)
+    if row is None:
+        log.warning("auth: no user for token=%s", token[:6])
+        return None
     return User.from_row(row)
"""


def _stub_llm_response() -> dict:
    """The shape `chat_json` returns when wired to a real provider."""
    return {
        "summary": "Adds explicit None handling in authenticate().",
        "walkthrough": ["server/auth.py: explicit None branch + log line"],
        "inline_suggestions": [
            {
                "path": "server/auth.py",
                "line": 11,
                "body": "Logging the token prefix is OK but consider a hashed identifier instead.",
                "suggested_code": "log.warning(\"auth: no user for hash=%s\", _hash(token))",
            }
        ],
        "risk_notes": ["No new failure mode introduced."],
    }


def test_llm_review_produces_expected_shape():
    """Pipes a known diff through the full `llm_review` pipeline
    against a mocked LLM client. The result must include the
    summary, the walkthrough, at least one validated suggestion,
    risk notes, the prompt version pinning, and the P3.5 cost
    estimate fields."""
    fake = MagicMock()
    fake.cfg = MagicMock()
    fake.cfg.model = "gpt-4o-mini"
    fake.chat_json.return_value = _stub_llm_response()

    result = llm_review(diff_text=_FIXTURE_DIFF, llm_client=fake)

    assert isinstance(result, LLMReviewResult)
    assert result.model == "gpt-4o-mini"
    assert result.prompt_version == PROMPT_VERSION
    assert result.summary
    # At least one inline suggestion survived the line-validity filter
    # (the stub points at line 11, which is on the new side of the
    # diff — that's the whole point of the filter).
    assert len(result.inline_suggestions) >= 1
    assert any("auth.py" in s.path for s in result.inline_suggestions)
    # Cost-visibility fields (P3.5) are populated, not zero.
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.estimated_cost_usd > 0


def test_llm_review_returns_empty_when_no_client():
    """The fallback path — no LLM configured. Must produce an
    empty result instead of raising. Catches the embarrassing
    regression where someone removes the early-return guard and
    suddenly every retrace install without an LLM key starts
    crashing during PR review."""
    result = llm_review(diff_text=_FIXTURE_DIFF, llm_client=None)
    assert isinstance(result, LLMReviewResult)
    assert result.is_empty
    assert result.model == ""


def test_llm_review_serializes_to_json():
    """Downstream tooling parses `result.to_dict()` — pin the
    shape so we don't break the contract by renaming a field."""
    fake = MagicMock()
    fake.cfg.model = "gpt-4o-mini"
    fake.chat_json.return_value = _stub_llm_response()
    result = llm_review(diff_text=_FIXTURE_DIFF, llm_client=fake)
    payload = result.to_dict()
    expected_keys = {
        "summary",
        "walkthrough",
        "inline_suggestions",
        "risk_notes",
        "model",
        "prompt_version",
        "chunks",
        "diff_too_large",
        "error",
        "input_tokens",
        "output_tokens",
        "estimated_cost_usd",
    }
    assert set(payload.keys()) == expected_keys
    # Survives a JSON round-trip.
    assert json.loads(json.dumps(payload)) == payload
