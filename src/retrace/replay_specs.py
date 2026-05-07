from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from retrace.storage import Storage
from retrace.tester import TesterSpec, create_spec


@dataclass(frozen=True)
class GeneratedReplaySpec:
    spec: TesterSpec
    issue_public_id: str
    replay_public_id: str
    confidence: str
    known_gaps: list[str]


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

    base_url = app_url.strip() or _infer_base_url(playback.events) or "http://127.0.0.1:3000"
    exact_steps, gaps = _steps_from_events(playback.events, base_url=base_url)
    assertions = _assertions_from_issue(issue)
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
            "replay_id": str(playback.session["id"]),
            "replay_public_id": str(playback.session["public_id"]),
            "session_id": representative_session_id,
        },
        data_extraction=[],
    )
    return GeneratedReplaySpec(
        spec=spec,
        issue_public_id=str(issue["public_id"]),
        replay_public_id=str(playback.session["public_id"]),
        confidence=_generation_confidence(exact_steps, gaps),
        known_gaps=gaps,
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
    selector = _selector_from_captured_target(target)
    if selector:
        target["selector"] = selector
    return target


def _selector_from_captured_target(target: dict[str, Any]) -> str:
    tag = _clean_css_identifier(str(target.get("tagName") or ""))
    test_id = _first_text(target, "testIdValue", "testId", "test_id", "dataTestId")
    if test_id:
        attr = _safe_test_id_attr(_first_text(target, "testIdAttrName"))
        return f'[{attr}="{_css_attr_escape(test_id)}"]'
    element_id = _first_text(target, "id")
    if element_id:
        if _CSS_IDENT_RE.fullmatch(element_id):
            return f"#{element_id}"
        return f'[id="{_css_attr_escape(element_id)}"]'
    name = _first_text(target, "name")
    if name:
        prefix = f"{tag}" if tag else ""
        return f'{prefix}[name="{_css_attr_escape(name)}"]'
    aria_label = _first_text(target, "ariaLabel", "aria_label")
    if aria_label:
        prefix = f"{tag}" if tag else ""
        return f'{prefix}[aria-label="{_css_attr_escape(aria_label)}"]'
    role = _first_text(target, "role")
    if role:
        return f'[role="{_css_attr_escape(role)}"]'
    text = _first_text(target, "text")
    if text and (tag in {"a", "button"} or not tag):
        return f'text="{_playwright_text_escape(text)}"'
    class_name = _first_text(target, "className", "class")
    classes = [
        token
        for token in re.split(r"\s+", class_name)
        if token and _CSS_IDENT_RE.fullmatch(token)
    ]
    if tag and classes:
        return f"{tag}{''.join(f'.{token}' for token in classes[:2])}"
    return ""


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


def _json_obj(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _generation_confidence(steps: list[dict[str, Any]], gaps: list[str]) -> str:
    if not steps:
        return "low"
    if gaps:
        return "medium"
    return "high"
