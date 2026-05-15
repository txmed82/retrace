from __future__ import annotations

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import httpx

from retrace.llm.client import build_llm_http_request, extract_llm_text_content

from .models import (
    TesterAssertionResult,
)

logger = logging.getLogger(__name__)


def _assertion_result(
    *,
    assertion: dict[str, Any],
    ok: bool,
    expected: Any,
    actual: Any,
    message: str,
    confidence: float | None = None,
) -> TesterAssertionResult:
    assertion_type = str(
        assertion.get("type") or assertion.get("assertion_type") or "unknown"
    )
    selected_confidence = (
        confidence
        if confidence is not None
        else assertion.get("confidence")
        if assertion.get("confidence") is not None
        else 1.0
    )
    return TesterAssertionResult(
        assertion_id=str(
            assertion.get("id") or assertion.get("name") or uuid.uuid4().hex[:8]
        ),
        assertion_type=assertion_type,
        ok=ok,
        expected=expected,
        actual=actual,
        message=message,
        source=str(assertion.get("source") or "native"),
        confidence=_coerce_confidence(selected_confidence, default=1.0),
        consensus_group=str(assertion.get("consensus_group") or ""),
        model_votes=list(assertion.get("model_votes") or []),
    )


def _coerce_confidence(raw: Any, *, default: float = 1.0) -> float:
    try:
        value = default if raw is None else float(raw)
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(1.0, value))


def _bool_from_vote(vote: dict[str, Any]) -> bool | None:
    val = vote.get("ok")
    if val is None:
        return None
    return bool(val)


def _evaluate_consensus_assertion(
    assertion: dict[str, Any],
    *,
    consensus_group: str,
) -> TesterAssertionResult:
    votes = _collect_consensus_votes(assertion)
    if not votes:
        return _assertion_result(
            assertion=assertion,
            ok=False,
            expected="at least one vote",
            actual=0,
            message="No model votes collected for consensus assertion.",
        )
    ok_votes = [v for v in votes if _bool_from_vote(v) is True]
    fail_votes = [v for v in votes if _bool_from_vote(v) is False]
    ok = len(ok_votes) >= len(fail_votes)
    confidence = (
        max([_coerce_confidence(v.get("confidence"), default=0.5) for v in ok_votes])
        if ok and ok_votes
        else max([_coerce_confidence(v.get("confidence"), default=0.5) for v in fail_votes])
        if fail_votes
        else 0.5
    )
    message = (
        f"Consensus reached (OK={len(ok_votes)}, FAIL={len(fail_votes)})."
        if ok
        else f"Consensus failed (OK={len(ok_votes)}, FAIL={len(fail_votes)})."
    )
    return _assertion_result(
        assertion={**assertion, "consensus_group": consensus_group, "model_votes": votes},
        ok=ok,
        expected=f"majority OK in {consensus_group}",
        actual=f"{len(ok_votes)} OK / {len(fail_votes)} FAIL",
        message=message,
        confidence=confidence,
    )


def _evaluate_model_backed_consensus_assertion(
    assertion: dict[str, Any],
    *,
    response: Optional[httpx.Response],
) -> TesterAssertionResult:
    models = list(assertion.get("models") or [])
    if not models:
        return _assertion_result(
            assertion=assertion,
            ok=False,
            expected="at least one model",
            actual=None,
            message="No models configured for model-backed consensus assertion.",
        )
    prompt = str(assertion.get("prompt") or assertion.get("text") or "")
    if not prompt:
        return _assertion_result(
            assertion=assertion,
            ok=False,
            expected="non-empty prompt",
            actual=None,
            message="Prompt is required for model-backed consensus assertion.",
        )
    evidence = assertion.get("evidence")
    if not evidence and response:
        evidence = _response_assertion_evidence(
            response, capture_body=bool(assertion.get("capture_body_evidence"))
        )
    votes = _call_consensus_models(
        models=models,
        prompt=prompt,
        snapshot=evidence or {},
        provider=str(assertion.get("provider") or "openai"),
        base_url=str(assertion.get("base_url") or ""),
        api_key=assertion.get("api_key"),
        timeout=float(assertion.get("timeout") or 30.0),
        retry=bool(assertion.get("retry")),
    )
    return _evaluate_consensus_assertion(
        {**assertion, "model_votes": votes},
        consensus_group=f"models:{','.join(models)}",
    )


def _call_consensus_models(
    *,
    models: list[str],
    prompt: str,
    snapshot: dict[str, Any],
    provider: str,
    base_url: str,
    api_key: str | None,
    timeout: float,
    retry: bool = False,
) -> list[dict[str, Any]]:
    if not models:
        return []
    max_workers = min(4, len(models))
    votes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _call_assertion_model,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                prompt=prompt,
                snapshot=snapshot,
                timeout=timeout,
                retry=retry,
            ): model
            for model in models
        }
        for future in as_completed(futures):
            try:
                votes.append(future.result())
            except Exception as exc:
                votes.append(
                    {
                        "model": futures[future],
                        "ok": False,
                        "error": str(exc),
                        "retry": retry,
                    }
                )
    order = {model: idx for idx, model in enumerate(models)}
    return sorted(votes, key=lambda vote: order.get(str(vote.get("model")), 999))


def _call_assertion_model(
    *,
    provider: str,
    base_url: str,
    api_key: str | None,
    model: str,
    prompt: str,
    snapshot: dict[str, Any],
    timeout: float,
    retry: bool,
) -> dict[str, Any]:
    system = (
        "You are a strict UI test assertion judge. Return only JSON with keys "
        "ok (boolean), confidence (0-1), and reasoning (short string)."
    )
    user = (
        f"Assertion: {prompt}\n\n"
        f"Observed evidence JSON:\n{json.dumps(snapshot, indent=2, ensure_ascii=True)}"
    )
    url, headers, body = build_llm_http_request(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        system=system,
        user=user,
        temperature=0.0,
        response_json=True,
        max_tokens=256,
    )
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    raw_text = extract_llm_text_content(provider=provider, payload=payload)
    parsed = _parse_model_vote_json(raw_text)

    # Safely coerce "ok" field to boolean
    ok_val = parsed.get("ok")
    if isinstance(ok_val, bool):
        ok = ok_val
    elif isinstance(ok_val, str):
        normalized = ok_val.strip().lower()
        ok = normalized in {"true", "1", "yes"}
    elif isinstance(ok_val, (int, float)):
        ok = bool(ok_val)
    else:
        ok = False

    return {
        "model": model,
        "ok": ok,
        "confidence": _coerce_confidence(parsed.get("confidence"), default=0.5),
        "reasoning": str(parsed.get("reasoning") or ""),
        "retry": retry,
    }


def _parse_model_vote_json(content: str) -> dict[str, Any]:
    text = content.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(1))
    if not isinstance(parsed, dict):
        raise ValueError("model vote response must be a JSON object")
    return parsed


def _collect_consensus_votes(assertion: dict[str, Any]) -> list[dict[str, Any]]:
    votes = [
        vote
        for vote in list(assertion.get("model_votes") or assertion.get("votes") or [])
        if isinstance(vote, dict)
    ]
    parsed = [_bool_from_vote(vote) for vote in votes]
    has_failure = any(value is False for value in parsed)
    retry_votes = [
        vote for vote in list(assertion.get("retry_votes") or []) if isinstance(vote, dict)
    ]
    if has_failure and retry_votes:
        votes.extend(retry_votes)
    return votes


def _evaluate_native_assertion(
    assertion: dict[str, Any],
    *,
    response: Optional[httpx.Response],
) -> TesterAssertionResult:
    kind = str(assertion.get("type") or assertion.get("assertion_type") or "").lower()
    if kind in {"model_consensus", "consensus", "ai_consensus"}:
        consensus_assertion = dict(assertion)
        response_evidence = _response_assertion_evidence(
            response,
            capture_body=bool(assertion.get("capture_body_evidence")),
        )
        existing_evidence = consensus_assertion.get("evidence")
        if isinstance(existing_evidence, dict):
            consensus_assertion["evidence"] = {
                **response_evidence,
                **existing_evidence,
            }
        else:
            consensus_assertion["evidence"] = response_evidence
        return _evaluate_model_backed_consensus_assertion(
            consensus_assertion,
            response=response,
        )
    if response is None:
        return _assertion_result(
            assertion=assertion,
            ok=False,
            expected=assertion.get("expected"),
            actual=None,
            message="No response is available for assertion.",
        )

    if kind in {"status", "status_code", "assert_status"}:
        expected = int(assertion.get("expected", assertion.get("status", 200)))
        actual = response.status_code
        return _assertion_result(
            assertion=assertion,
            ok=actual == expected,
            expected=expected,
            actual=actual,
            message=f"Expected status {expected}, got {actual}.",
        )

    if kind in {"text_contains", "body_contains", "assert_text", "contains"}:
        expected = str(assertion.get("expected", assertion.get("text", "")))
        actual = response.text
        return _assertion_result(
            assertion=assertion,
            ok=expected in actual,
            expected=expected,
            actual={"contains": expected in actual, "body_length": len(actual)},
            message=f"Expected response body to contain {expected!r}.",
        )

    if kind in {"header_present", "assert_header"}:
        expected = str(assertion.get("expected", assertion.get("header", "")))
        actual = dict(response.headers)
        return _assertion_result(
            assertion=assertion,
            ok=expected.lower() in {k.lower() for k in response.headers.keys()},
            expected=expected,
            actual=actual,
            message=f"Expected header {expected!r} to be present.",
        )

    return _assertion_result(
        assertion=assertion,
        ok=False,
        expected=assertion.get("expected"),
        actual={"unsupported_type": kind},
        message=f"Unsupported native assertion type: {kind or 'unknown'}.",
    )


def _response_assertion_evidence(
    response: Optional[httpx.Response],
    *,
    capture_body: bool = False,
) -> dict[str, Any]:
    if response is None:
        return {"kind": "http_response", "available": False}
    text = response.text
    evidence = {
        "kind": "http_response",
        "available": True,
        "url": str(response.url),
        "status_code": response.status_code,
        "headers": _redacted_response_headers(dict(response.headers)),
        "body_capture": bool(capture_body),
        "body_length": len(text),
    }
    if capture_body:
        evidence["body_excerpt"] = text[:2000]
    return evidence


def _redacted_response_headers(headers: dict[str, str]) -> dict[str, str]:
    sensitive = {
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-csrf-token",
        "x-xsrf-token",
    }
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in sensitive:
            redacted[key] = "[redacted]"
        else:
            redacted[key] = value
    return redacted


def _classify_failure(
    *,
    harness_log_path: Path,
    error: str,
    assertion_results: list[dict[str, Any]],
    exit_code: int,
) -> str:
    text = ""
    try:
        if harness_log_path.exists():
            text = harness_log_path.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        text = ""
    failed_assertions = [
        item for item in assertion_results if not bool(item.get("ok", False))
    ]
    merged = "\n".join(
        [
            text,
            str(error or ""),
            _assertion_text_for_classification(failed_assertions),
        ]
    ).lower()
    if any(
        k in merged
        for k in [
            "app did not become reachable",
            "connection refused",
            "econnrefused",
            "net::err_connection_refused",
            "failed to connect",
            "could not connect",
            "server unavailable",
        ]
    ):
        return "environment_failure"
    if any(k in merged for k in ["timeout", "timed out", "deadline exceeded"]):
        return "timeout"
    if any(
        k in merged
        for k in ["invalid username or password", "login failed", "unauthorized", "401"]
    ):
        return "auth_failure"
    if _failed_selector_assertion(failed_assertions):
        return "selector_drift"
    if failed_assertions or exit_code != 0:
        return "app_bug"
    return "unknown"


def _assertion_text_for_classification(items: list[dict[str, Any]]) -> str:
    parts = []
    for item in items:
        parts.append(str(item.get("message") or ""))
        parts.append(str(item.get("expected") or ""))
        parts.append(str(item.get("actual") or ""))
    return "\n".join(parts)


def _failed_selector_assertion(items: list[dict[str, Any]]) -> bool:
    for item in items:
        msg = str(item.get("message") or "").lower()
        if "selector" in msg or "not found" in msg or "could not find" in msg:
            return True
    return False


def _flake_reason_from_classification(failure_classification: str) -> str:
    if failure_classification == "timeout":
        return "Execution timed out intermittently."
    if failure_classification == "environment_failure":
        return "Intermittent environment or network connection failure."
    if failure_classification == "selector_drift":
        return "Selector failed to match intermittently (potential race or dynamic UI)."
    return "Intermittent test failure."
