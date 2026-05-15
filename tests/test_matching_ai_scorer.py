from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from retrace.matching.scorer import CodeCandidate
from retrace.matching.ai_scorer import rerank_candidates_with_ai


def test_rerank_candidates_with_ai():
    # Setup
    llm = MagicMock()
    candidates = [
        CodeCandidate(file_path="src/auth.py", score=10.0, rationale="keywords"),
        CodeCandidate(file_path="src/ui/Button.tsx", score=8.0, rationale="keywords"),
        CodeCandidate(file_path="src/utils.py", score=5.0, rationale="keywords"),
    ]
    
    # Mock LLM to prioritize the UI component
    llm.chat_json.return_value = {
        "ranked_files": ["src/ui/Button.tsx", "src/auth.py"]
    }
    
    reranked = rerank_candidates_with_ai(
        llm=llm,
        candidates=candidates,
        title="Button is broken",
        category="ui",
        evidence_text="Clicking the button does nothing",
        top_n=2
    )
    
    assert len(reranked) == 2
    assert reranked[0].file_path == "src/ui/Button.tsx"
    assert reranked[1].file_path == "src/auth.py"
    
    # Verify LLM was called with correct context
    args, kwargs = llm.chat_json.call_args
    assert "Button is broken" in kwargs["user"]
    assert "src/ui/Button.tsx" in kwargs["user"]


def test_rerank_candidates_fallback_on_error():
    llm = MagicMock()
    llm.chat_json.side_effect = Exception("LLM Down")
    
    candidates = [
        CodeCandidate(file_path="src/auth.py", score=10.0, rationale="keywords"),
    ]
    
    reranked = rerank_candidates_with_ai(
        llm=llm,
        candidates=candidates,
        title="Bug",
        category="error",
        evidence_text="error",
        top_n=1
    )
    
    # Should fall back to original ranking
    assert len(reranked) == 1
    assert reranked[0].file_path == "src/auth.py"
