from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from retrace.api_testing import APITestSpec, create_api_spec
from retrace.storage import Storage
from retrace.tester import TesterSpec, create_spec


@dataclass(frozen=True)
class GeneratedReplaySpec:
    spec: TesterSpec
    issue_public_id: str
    replay_public_id: str
    confidence: str
    known_gaps: list[str]


@dataclass(frozen=True)
class GeneratedReplayAPISpec:
    spec: APITestSpec
    issue_public_id: str
    replay_public_id: str
    source_signal: dict[str, Any]


def generate_spec_from_replay_issue(
    *,
    store: Storage,
    specs_dir: Path,
    project_id: str,
    environment_id: str,
    issue_id: str,
    app_url: str = "",
) -> GeneratedReplaySpec:
    issue = store.get_replay_issue(
        project_id=project_id,
        environment_id=environment_id,
        issue_id=issue_id,
    )
    if issue is None:
        raise ValueError(f"Replay issue not found: {issue_id}")

    representative_session_id = str(issue["representative_session_id"] or "")
    sessions = store.list_replay_issue_sessions(str(issue["id"]))
    if not representative_session_id and sessions:
        representative_session_id = str(sessions[0]["session_id"])
    if not representative_session_id:
        raise ValueError(f"Replay issue has no linked session: {issue_id}")

    playback = store.get_replay_playback(
        project_id=project_id,
        environment_id=environment_id,
        session_id=representative_session_id,
    )
    if playback is None:
        raise ValueError(f"Replay session not found: {representative_session_id}")

    canonical_failure_id = str(issue["canonical_failure_id"] or "")
    base_url = app_url.strip() or _infer_base_url(playback.events) or "http://127.0.0.1:3000"
    exact_steps, gaps = _steps_from_events(playback.events, base_url=base_url)
    assertions = _assertions_from_issue(issue)
    generation_notes = _generation_notes(
        issue=issue,
        base_url=base_url,
        representative_session_id=representative_session_id,
        exact_steps=exact_steps,
        gaps=gaps,
    )
    prompt = (
        f"Replay-derived regression for {issue['public_id']}: "
        f"{str(issue['title'] or 'Replay issue')}"
    )
    spec = create_spec(
        specs_dir=specs_dir,
        name=f"{issue['public_id']} regression",
        prompt=prompt,
        app_url=base_url,
        start_command="",
        harness_command="",
        execution_engine="native",
        exact_steps=exact_steps,
        assertions=assertions,
        fixtures={
            "source": "replay_issue",
            "issue_id": str(issue["id"]),
            "issue_public_id": str(issue["public_id"]),
            "canonical_failure_id": canonical_failure_id,
            "replay_id": str(playback.session["id"]),
            "replay_public_id": str(playback.session["public_id"]),
            "session_id": representative_session_id,
            "generation": generation_notes,
        },
        data_extraction=[],
    )
    if canonical_failure_id:
        store.upsert_failure_test_link(
            failure_id=canonical_failure_id,
            issue_id=str(issue["id"]),
            issue_public_id=str(issue["public_id"]),
            spec_id=spec.spec_id,
            spec_name=spec.name,
            spec_path=str(specs_dir / f"{spec.spec_id}.json"),
            source="replay_issue",
        )
    return GeneratedReplaySpec(
        spec=spec,
        issue_public_id=str(issue["public_id"]),
        replay_public_id=str(playback.session["public_id"]),
        confidence=_generation_confidence(exact_steps, gaps),
        known_gaps=gaps,
    )


def generate_api_spec_from_replay_issue(
    *,
    store: Storage,
    specs_dir: Path,
    project_id: str,
    environment_id: str,
    issue_id: str,
    app_url: str = "",
) -> GeneratedReplayAPISpec:
    issue = store.get_replay_issue(
        project_id=project_id,
        environment_id=environment_id,
        issue_id=issue_id,
    )
    if issue is None:
        raise ValueError(f"Replay issue not found: {issue_id}")

    representative_session_id = str(issue["representative_session_id"] or "")
    sessions = store.list_replay_issue_sessions(str(issue["id"]))
    if not representative_session_id and sessions:
        representative_session_id = str(sessions[0]["session_id"])
    if not representative_session_id:
        raise ValueError(f"Replay issue has no linked session: {issue_id}")

    playback = store.get_replay_playback(
        project_id=project_id,
        environment_id=environment_id,
        session_id=representative_session_id,
    )
    if playback is None:
        raise ValueError(f"Replay session not found: {representative_session_id}")

    evidence = _json_obj(issue["evidence_json"])
    signal = _first_signal(_evidence_signals(evidence), {"network_4xx", "network_5xx"})
    if signal is None:
        raise ValueError(f"Replay issue has no failed network signal: {issue_id}")
    details = _json_obj(signal.get("details"))
    base_url = app_url.strip() or _infer_base_url(playback.events) or "http://127.0.0.1:3000"
    method = str(details.get("method") or details.get("request_method") or "GET").upper()
    raw_url = str(details.get("request_url") or details.get("url") or "").strip()
    if not raw_url:
        raise ValueError(f"Network signal has no request_url: {issue_id}")
    url, query = _api_url_and_query(raw_url, base_url)
    sanitized_source_url = _redacted_url(raw_url)
    headers, auth, header_notes = _safe_api_headers(details)
    body, body_notes = _safe_api_body(details)
    original_status = _status_int(details.get("status") or details.get("status_code"))
    expected_status = 200 if original_status >= 400 or original_status == 0 else original_status
    trigger_context = _api_trigger_context(
        evidence=evidence,
        signal_timestamp_ms=_status_int(signal.get("timestamp_ms")),
    )
    trace_ids = _api_trace_ids(details)
    sanitized_signal = _sanitized_network_signal(
        signal=signal,
        method=method,
        url=sanitized_source_url,
        status=original_status,
        headers=headers,
        body=body,
    )
    spec = create_api_spec(
        specs_dir=specs_dir,
        name=f"{issue['public_id']} API regression",
        method=method,
        url=url,
        query=query,
        headers=headers,
        body=body,
        auth=auth,
        expected_status=expected_status,
        json_assertions=[],
        schema_assertions=[],
        latency_ms=0,
        fixtures={
            "source": "replay_issue_api",
            "issue_id": str(issue["id"]),
            "issue_public_id": str(issue["public_id"]),
            "canonical_failure_id": str(issue["canonical_failure_id"] or ""),
            "replay_id": str(playback.session["id"]),
            "replay_public_id": str(playback.session["public_id"]),
            "session_id": representative_session_id,
            "source_network_signal": {
                "detector": signal.get("detector"),
                "method": method,
                "url": sanitized_source_url,
                "status": original_status,
            },
            "api_regression": {
                "original_status": original_status,
                "expected_status": expected_status,
                "trigger_context": trigger_context,
                "trace_ids": trace_ids,
                "assertion_strategy": (
                    "The real user replay saw this request fail; the regression "
                    "passes only when the same request no longer returns the "
                    "captured failure status."
                ),
            },
            "fixture_notes": [
                *header_notes,
                *body_notes,
                "Generated from a failed replay network call; confirm auth and test data before relying on CI.",
            ],
        },
    )
    canonical_failure_id = str(issue["canonical_failure_id"] or "")
    if canonical_failure_id:
        store.upsert_failure_test_link(
            failure_id=canonical_failure_id,
            issue_id=str(issue["id"]),
            issue_public_id=str(issue["public_id"]),
            spec_id=spec.spec_id,
            spec_name=spec.name,
            spec_path=str(specs_dir / f"{spec.spec_id}.json"),
            source="replay_issue_api",
        )
    return GeneratedReplayAPISpec(
        spec=spec,
        issue_public_id=str(issue["public_id"]),
        replay_public_id=str(playback.session["public_id"]),
        source_signal=sanitized_signal,
    )


def _infer_base_url(events: list[dict[str, Any]]) -> str:
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        href = data.get("href")
        if isinstance(href, str) and href.startswith(("http://", "https://")):
            parsed = urlparse(href)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _steps_from_events(
    events: list[dict[str, Any]],
    *,
    base_url: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    steps: list[dict[str, Any]] = []
    gaps: list[str] = []
    seen_navs: set[str] = set()
    sdk_interactions = _sdk_interaction_timestamps(events)
    click_count = 0
    input_count = 0
    unknown_count = 0
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            unknown_count += 1
            steps.append(
                {
                    "id": f"unknown-{unknown_count}",
                    "action": "unknown",
                    "meta": {"rrweb_type": event.get("type"), "data": event.get("data")},
                    "source": "replay",
                }
            )
            gaps.append(
                f"unknown-{unknown_count}: rrweb event type {event.get('type')} with non-dict data"
            )
            continue
        if event.get("type") == 4 and isinstance(data.get("href"), str):
            href = str(data["href"])
            if href not in seen_navs:
                steps.append(
                    {
                        "id": f"nav-{len(seen_navs) + 1}",
                        "action": "get",
                        "url": href,
                        "source": "replay",
                    }
                )
                seen_navs.add(href)
        elif event.get("type") == 6 and _custom_plugin(data) == "retrace/click@1":
            click_count += 1
            payload = _custom_payload(data)
            target = _step_target_from_capture(payload.get("target"))
            step = {
                "id": f"click-{click_count}",
                "action": "click",
                "target": target,
                "source": "retrace_browser_sdk",
            }
            steps.append(step)
            if not target.get("selector"):
                gaps.append(
                    f"click-{click_count} needs a durable locator; SDK target metadata was incomplete"
                )
        elif event.get("type") == 6 and _custom_plugin(data) == "retrace/input@1":
            input_count += 1
            payload = _custom_payload(data)
            target = _step_target_from_capture(payload.get("target"))
            step = {
                "id": f"input-{input_count}",
                "action": "type",
                "target": target,
                "text": "[redacted-replay-input]",
                "source": "retrace_browser_sdk",
            }
            steps.append(step)
            if not target.get("selector"):
                gaps.append(
                    f"input-{input_count} needs a durable locator and safe test data"
                )
            else:
                gaps.append(f"input-{input_count} needs safe test data")
        elif event.get("type") == 6 and _custom_plugin(data).startswith("retrace/"):
            continue
        elif event.get("type") == 3 and data.get("source") == 2 and data.get("type") == 2:
            if _has_nearby_sdk_interaction(sdk_interactions, "click", event.get("timestamp")):
                continue
            click_count += 1
            steps.append(
                {
                    "id": f"click-{click_count}",
                    "action": "click",
                    "target": {"rrweb_id": data.get("id")},
                    "source": "replay",
                }
            )
            gaps.append(
                f"click-{click_count} needs a durable locator for rrweb node {data.get('id', 'unknown')}"
            )
        elif event.get("type") == 3 and data.get("source") == 5:
            if _has_nearby_sdk_interaction(sdk_interactions, "input", event.get("timestamp")):
                continue
            input_count += 1
            steps.append(
                {
                    "id": f"input-{input_count}",
                    "action": "type",
                    "target": {"rrweb_id": data.get("id")},
                    "text": "[redacted-replay-input]",
                    "source": "replay",
                }
            )
            gaps.append(
                f"input-{input_count} needs a durable locator and safe test data"
            )
        else:
            unknown_count += 1
            steps.append(
                {
                    "id": f"unknown-{unknown_count}",
                    "action": "unknown",
                    "meta": {"rrweb_type": event.get("type"), "data": data},
                    "source": "replay",
                }
            )
            gaps.append(
                f"unknown-{unknown_count}: unsupported rrweb event type {event.get('type')}, source {data.get('source')}, id {data.get('id', 'N/A')}"
            )
    if not steps:
        steps.append({"id": "home", "action": "get", "url": base_url})
        gaps.append("Replay did not include convertible navigation, click, or input events.")
    return steps[:20], list(dict.fromkeys(gaps))


def _sdk_interaction_timestamps(events: list[dict[str, Any]]) -> dict[str, list[int]]:
    matches: dict[str, list[int]] = {"click": [], "input": []}
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event.get("type") != 6:
            continue
        plugin = _custom_plugin(data)
        action = {"retrace/click@1": "click", "retrace/input@1": "input"}.get(plugin)
        if not action:
            continue
        timestamp = _event_timestamp_ms(event.get("timestamp"))
        if timestamp is not None:
            matches[action].append(timestamp)
    return matches


def _has_nearby_sdk_interaction(
    sdk_interactions: dict[str, list[int]],
    action: str,
    raw_timestamp: Any,
) -> bool:
    timestamp = _event_timestamp_ms(raw_timestamp)
    if timestamp is None:
        return False
    return any(
        abs(timestamp - sdk_timestamp) <= 250
        for sdk_timestamp in sdk_interactions.get(action, [])
    )


def _event_timestamp_ms(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _custom_plugin(data: dict[str, Any]) -> str:
    return str(data.get("plugin") or "")


def _custom_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else {}


def _step_target_from_capture(raw_target: Any) -> dict[str, Any]:
    if not isinstance(raw_target, dict):
        return {}
    target = {k: v for k, v in raw_target.items() if v not in (None, "")}
    candidates = _selector_candidates_from_captured_target(target)
    if candidates:
        target["selector"] = candidates[0]["selector"]
        target["selector_candidates"] = candidates
        target["selector_rationale"] = candidates[0]["rationale"]
    return target


def _selector_from_captured_target(target: dict[str, Any]) -> str:
    candidates = _selector_candidates_from_captured_target(target)
    return str(candidates[0]["selector"]) if candidates else ""


def _selector_candidates_from_captured_target(
    target: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add(strategy: str, selector: str, rationale: str, score: int) -> None:
        if selector and all(item["selector"] != selector for item in candidates):
            candidates.append(
                {
                    "selector": selector,
                    "strategy": strategy,
                    "score": score,
                    "rationale": rationale,
                }
            )

    tag = _clean_css_identifier(str(target.get("tagName") or ""))
    test_id = _first_text(target, "testIdValue", "testId", "test_id", "dataTestId")
    if test_id:
        attr = _safe_test_id_attr(_first_text(target, "testIdAttrName"))
        add(
            "test_id",
            f'[{attr}="{_css_attr_escape(test_id)}"]',
            f"{attr} is the most stable captured test selector.",
            100,
        )
    role = _first_text(target, "role") or _implicit_role(tag)
    accessible_name = _accessible_name(target)
    if role and accessible_name:
        add(
            "role_name",
            f'role={_playwright_text_escape(role)}[name="{_playwright_text_escape(accessible_name)}"]',
            "Accessible role and name usually survive markup refactors.",
            90,
        )
    label = _first_text(target, "labelText", "label")
    if label:
        add(
            "label",
            f'label="{_playwright_text_escape(label)}"',
            "Associated form label is a durable user-facing locator.",
            88,
        )
    aria_label = _first_text(target, "ariaLabel", "aria_label")
    if aria_label:
        prefix = f"{tag}" if tag else ""
        add(
            "aria_label",
            f'{prefix}[aria-label="{_css_attr_escape(aria_label)}"]',
            "ARIA label is explicit accessibility metadata.",
            86,
        )
    name = _first_text(target, "name")
    if name:
        prefix = f"{tag}" if tag else ""
        add(
            "name",
            f'{prefix}[name="{_css_attr_escape(name)}"]',
            "Name attribute is stable for form controls.",
            82,
        )
    element_id = _first_text(target, "id")
    if element_id:
        selector = (
            f"#{element_id}"
            if _CSS_IDENT_RE.fullmatch(element_id)
            else f'[id="{_css_attr_escape(element_id)}"]'
        )
        add("id", selector, "Element ID is useful when no semantic locator wins.", 70)
    text = _first_text(target, "text")
    if _is_constrained_text(text, tag):
        add(
            "text",
            f'text="{_playwright_text_escape(text)}"',
            "Short visible text can work, but may change with copy updates.",
            60,
        )
    class_name = _first_text(target, "className", "class")
    classes = [
        token
        for token in re.split(r"\s+", class_name)
        if token and _CSS_IDENT_RE.fullmatch(token)
    ]
    if tag and classes:
        add(
            "class",
            f"{tag}{''.join(f'.{token}' for token in classes[:2])}",
            "Class names are brittle and are only used as a last resort.",
            20,
        )
    return sorted(candidates, key=lambda item: -int(item["score"]))


def _implicit_role(tag: str) -> str:
    return {
        "a": "link",
        "button": "button",
        "input": "textbox",
        "select": "combobox",
        "textarea": "textbox",
    }.get(tag, "")


def _accessible_name(target: dict[str, Any]) -> str:
    return _first_text(target, "accessibleName", "ariaLabel", "labelText", "text")


def _is_constrained_text(text: str, tag: str) -> bool:
    if not text or len(text) > 80:
        return False
    return tag in {"a", "button"} or not tag


def _first_text(target: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _clean_css_identifier(value: str) -> str:
    clean = value.strip().lower()
    return clean if _CSS_IDENT_RE.fullmatch(clean) else ""


def _safe_test_id_attr(value: str) -> str:
    if value in {"data-testid", "data-test", "data-qa"}:
        return value
    return "data-testid"


def _css_attr_escape(value: str) -> str:
    return _selector_string_escape(value)


def _playwright_text_escape(value: str) -> str:
    return _selector_string_escape(value)


def _selector_string_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


_CSS_IDENT_RE = re.compile(r"-?[_a-zA-Z]+[_a-zA-Z0-9-]*")


def _assertions_from_issue(issue: Any) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = [
        {
            "id": "page-loads",
            "type": "status_code",
            "expected": 200,
            "source": "replay_generator",
        }
    ]
    signal_summary = _json_obj(issue["signal_summary_json"])
    evidence = _json_obj(issue["evidence_json"])
    signals = _evidence_signals(evidence)
    network_signal = _first_signal(signals, {"network_4xx", "network_5xx"})
    if network_signal is not None:
        details = _json_obj(network_signal.get("details"))
        status = details.get("status") or details.get("status_code") or "4xx/5xx"
        method = str(details.get("method") or details.get("request_method") or "REQUEST")
        request_url = str(
            details.get("request_url") or details.get("url") or "failed request"
        )
        assertions.append(
            {
                "id": "network-failure-cleared",
                "type": "model_consensus",
                "prompt": (
                    "After replaying the steps, verify the failed network request "
                    f"does not recur: {method.upper()} {request_url} returned {status}."
                ),
                "expected": "No matching failed request or visible network error remains.",
                "consensus_group": str(issue["public_id"]),
                "source": "replay_generator",
                "evidence": {
                    "detector": network_signal.get("detector"),
                    "method": method.upper(),
                    "url": request_url,
                    "status": status,
                },
            }
        )
        assertions.append(_error_ui_absent_assertion("network-error-ui-absent"))
    if "blank_render" in signal_summary:
        assertions.append(
            {
                "id": "page-not-blank",
                "type": "selector_visible",
                "selector": "body",
                "expected": "body visible",
                "timeout_ms": 3000,
                "source": "replay_generator",
            }
        )
        assertions.append(
            {
                "id": "visible-content-present",
                "type": "text_matches",
                "selector": "body",
                "expected": r"\S",
                "source": "replay_generator",
            }
        )
    if "error_toast" in signal_summary:
        assertions.append(_error_ui_absent_assertion("error-toast-absent"))
    if signal_summary:
        assertions.append(
            {
                "id": "issue-not-reproduced",
                "type": "model_consensus",
                "prompt": (
                    "The replay-derived failure should no longer be visible. "
                    f"Original issue: {issue['title']}. Signals: "
                    f"{json.dumps(signal_summary, sort_keys=True)}"
                ),
                "consensus_group": str(issue["public_id"]),
                "source": "replay_generator",
            }
        )
    return assertions


def _error_ui_absent_assertion(assertion_id: str) -> dict[str, Any]:
    return {
        "id": assertion_id,
        "type": "selector_count",
        "selector": (
            '[role="alert"][data-error]:visible, '
            '[role="alert"][aria-label*="error" i]:visible, '
            '[data-testid*="error" i]:visible, '
            '[data-test*="error" i]:visible, '
            '[data-qa*="error" i]:visible, '
            '.Toastify__toast--error:visible, '
            '[class*="toast" i][class*="error" i]:visible'
        ),
        "expected": 0,
        "source": "replay_generator",
    }


def _evidence_signals(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    raw = evidence.get("signals")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _first_signal(
    signals: list[dict[str, Any]],
    detectors: set[str],
) -> dict[str, Any] | None:
    for signal in signals:
        if str(signal.get("detector") or "") in detectors:
            return signal
    return None


def _generation_notes(
    *,
    issue: Any,
    base_url: str,
    representative_session_id: str,
    exact_steps: list[dict[str, Any]],
    gaps: list[str],
) -> dict[str, Any]:
    unsupported_step_warnings = [
        gap
        for gap in gaps
        if gap.startswith("unknown-") or "unsupported rrweb event" in gap
    ]
    return {
        "human_readable_steps": _human_readable_steps(exact_steps),
        "preconditions": [
            f"Run the app at {base_url}.",
            f"Replay source session: {representative_session_id}.",
            f"Source issue: {issue['public_id']}.",
        ],
        "fixture_notes": [
            "Replay input values are redacted; replace placeholders with safe test data.",
            "Selectors are generated from SDK metadata when available.",
        ],
        "unsupported_step_warnings": unsupported_step_warnings,
    }


def _human_readable_steps(exact_steps: list[dict[str, Any]]) -> list[str]:
    readable: list[str] = []
    for step in exact_steps:
        action = str(step.get("action") or "")
        if action == "get":
            readable.append(f"Open {step.get('url')}")
        elif action == "click":
            readable.append(f"Click {_target_label(step)}")
        elif action == "type":
            readable.append(f"Type redacted input into {_target_label(step)}")
        elif action == "unknown":
            readable.append(f"Review unsupported replay step {step.get('id')}")
    return readable


def _target_label(step: dict[str, Any]) -> str:
    target = step.get("target")
    if not isinstance(target, dict):
        return "unknown target"
    return str(
        target.get("selector")
        or target.get("selector_rationale")
        or target.get("rrweb_id")
        or "unknown target"
    )


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-csrf-token",
    "x-xsrf-token",
}
_SENSITIVE_BODY_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "client_secret",
    "jwt",
    "password",
    "refresh_token",
    "secret",
    "token",
}
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "jwt",
    "password",
    "secret",
    "token",
}


def _api_url_and_query(raw_url: str, base_url: str) -> tuple[str, dict[str, Any]]:
    absolute = raw_url if raw_url.startswith(("http://", "https://")) else urljoin(base_url, raw_url)
    parsed = urlparse(absolute)
    raw_query = {
        key: values[-1] if len(values) == 1 else values
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
    }
    query = _redact_query(raw_query)
    clean_url = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", parsed.fragment)
    )
    return clean_url, query


def _redacted_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    raw_query = {
        key: values[-1] if len(values) == 1 else values
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
    }
    query = urlencode(_redact_query(raw_query), doseq=True)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment)
    )


def _redact_query(query: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            "[redacted-api-input]"
            if str(key).lower() in _SENSITIVE_QUERY_KEYS
            else value
        )
        for key, value in query.items()
    }


def _safe_api_headers(details: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any], list[str]]:
    raw = (
        details.get("request_headers")
        or details.get("headers")
        or details.get("requestHeaders")
        or {}
    )
    if not isinstance(raw, dict):
        return {}, {}, []
    headers: dict[str, str] = {}
    sensitive: list[str] = []
    for key, value in raw.items():
        key_s = str(key)
        if key_s.lower() in _SENSITIVE_HEADER_NAMES:
            sensitive.append(key_s)
            continue
        headers[key_s] = str(value)
    if not sensitive:
        return headers, {}, []
    return (
        headers,
        {"type": "headers", "headers_env": "RETRACE_API_AUTH_HEADERS"},
        [
            "Sensitive request headers were not persisted; set "
            "RETRACE_API_AUTH_HEADERS to a JSON object before running.",
        ],
    )


def _safe_api_body(details: dict[str, Any]) -> tuple[Any, list[str]]:
    body = (
        details.get("request_body")
        if "request_body" in details
        else details.get("body", details.get("requestBody"))
    )
    if body in (None, ""):
        return None, []
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return body, []
    if isinstance(body, str):
        try:
            parsed_body = json.loads(body)
        except json.JSONDecodeError:
            parsed_form = parse_qs(body, keep_blank_values=True)
            if parsed_form:
                redacted_form = {
                    key: (
                        ["[redacted-api-input]"] * len(values)
                        if str(key).lower() in _SENSITIVE_BODY_KEYS
                        else values
                    )
                    for key, values in parsed_form.items()
                }
                if redacted_form != parsed_form:
                    return urlencode(redacted_form, doseq=True), [
                        "Sensitive form-encoded body values were replaced with redacted placeholders."
                    ]
            return body, []
        redacted_body = _redact_api_body(parsed_body)
        notes = []
        if redacted_body != parsed_body:
            notes.append(
                "Sensitive request body values were replaced with redacted placeholders."
            )
        return json.dumps(redacted_body, sort_keys=True), notes
    redacted = _redact_api_body(body)
    notes = []
    if redacted != body:
        notes.append("Sensitive request body values were replaced with redacted placeholders.")
    return redacted, notes


def _sanitized_network_signal(
    *,
    signal: dict[str, Any],
    method: str,
    url: str,
    status: int,
    headers: dict[str, str],
    body: Any,
) -> dict[str, Any]:
    return {
        "detector": signal.get("detector"),
        "timestamp_ms": signal.get("timestamp_ms"),
        "details": {
            "method": method,
            "request_url": url,
            "status": status,
            "request_headers": headers,
            "request_body": body,
        },
    }


def _redact_api_body(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted-api-input]"
                if str(key).lower() in _SENSITIVE_BODY_KEYS
                else _redact_api_body(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_api_body(item) for item in value]
    return value


def _api_trigger_context(
    *, evidence: dict[str, Any], signal_timestamp_ms: int
) -> list[dict[str, Any]]:
    events = evidence.get("events") if isinstance(evidence.get("events"), list) else []
    out: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        timestamp_ms = _status_int(event.get("timestamp_ms"))
        if signal_timestamp_ms and abs(timestamp_ms - signal_timestamp_ms) > 10_000:
            continue
        event_type = event.get("type")
        if event_type == 4:
            out.append(
                {
                    "kind": "navigation",
                    "timestamp_ms": timestamp_ms,
                    "href": str(event.get("href") or ""),
                }
            )
        elif event_type == 3 and event.get("source") == 2:
            out.append(
                {
                    "kind": "click",
                    "timestamp_ms": timestamp_ms,
                    "rrweb_id": event.get("id"),
                }
            )
        elif event_type == 3 and event.get("source") == 5:
            out.append(
                {
                    "kind": "input",
                    "timestamp_ms": timestamp_ms,
                    "rrweb_id": event.get("id"),
                }
            )
    return out[-8:]


def _api_trace_ids(details: dict[str, Any]) -> list[str]:
    trace = details.get("trace") if isinstance(details.get("trace"), dict) else {}
    values = [
        trace.get("traceId"),
        trace.get("trace_id"),
        trace.get("requestTraceId"),
        trace.get("responseTraceId"),
    ]
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _status_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _generation_confidence(steps: list[dict[str, Any]], gaps: list[str]) -> str:
    if not steps:
        return "low"
    if gaps:
        return "medium"
    return "high"
