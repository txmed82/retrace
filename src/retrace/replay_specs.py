from __future__ import annotations

import json
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
    click_count = 0
    input_count = 0
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
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
        elif event.get("type") == 3 and data.get("source") == 2 and data.get("type") == 2:
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
    if not steps:
        steps.append({"id": "home", "action": "get", "url": base_url})
        gaps.append("Replay did not include convertible navigation, click, or input events.")
    return steps[:20], list(dict.fromkeys(gaps))


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
