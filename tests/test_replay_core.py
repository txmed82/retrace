from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from retrace.detectors.base import Signal
from retrace.replay_core import (
    ReplaySignalConfig,
    process_queued_replay_jobs,
    process_replay_sessions,
    summarize_replay_issue,
)
from retrace.issue_sinks import promote_replay_issue
from retrace.api_testing import load_api_spec
from retrace.replay_specs import (
    _safe_api_body,
    generate_api_spec_from_replay_issue,
    generate_spec_from_replay_issue,
)
from retrace.sinks.base import Cluster
from retrace.storage import Storage


def _workspace(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="production",
    )
    return store, workspace


def _navigation(url: str, ts: int = 0) -> dict[str, object]:
    return {"type": 4, "timestamp": ts, "data": {"href": url}}


def _click(node_id: int, ts: int = 100) -> dict[str, object]:
    return {
        "type": 3,
        "timestamp": ts,
        "data": {"source": 2, "type": 2, "id": node_id},
    }


def _console_error(message: str, ts: int = 1000) -> dict[str, object]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "retrace/console@1",
            "payload": {"level": "error", "payload": [message]},
        },
    }


def _sdk_click(target: dict[str, object], ts: int = 100) -> dict[str, object]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "retrace/click@1",
            "payload": {
                "button": 0,
                "target": target,
                "url": "https://app.example/checkout",
            },
        },
    }


def _sdk_input(target: dict[str, object], ts: int = 200) -> dict[str, object]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "retrace/input@1",
            "payload": {
                "target": target,
                "valueMasked": True,
                "url": "https://app.example/checkout",
            },
        },
    }


class FailingLLM:
    def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
        raise RuntimeError("offline")


class SuccessfulLLM:
    class Cfg:
        model = "qa-model"

    cfg = Cfg()

    def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
        return {
            "title": "Checkout total crashes",
            "severity": "high",
            "category": "functional_error",
            "what_happened": "The checkout page crashes after the user clicks pay.",
            "likely_cause": "The total value is missing before render.",
            "reproduction_steps": ["Open checkout", "Click pay"],
            "confidence": "high",
        }


def test_replay_core_aggregates_playback_batches_before_detection(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-batches",
        sequence=1,
        events=[_click(42), _console_error("TypeError: total is undefined")],
        flush_type="normal",
    )
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-batches",
        sequence=0,
        events=[_navigation("https://app.example/checkout")],
        flush_type="normal",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-batches"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert result.sessions_scanned == 1
    assert result.sessions_with_signals == 1
    assert result.signals_detected == 1
    signals = store.list_replay_signals(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-batches",
    )
    assert signals[0]["url"] == "https://app.example/checkout"
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert json.loads(issue["reproduction_steps_json"])[:2] == [
        "Open https://app.example/checkout",
        "Click element id 42",
    ]


def test_replay_core_persists_only_configured_signal_matches(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-config",
        sequence=0,
        events=[_navigation("https://app.example/cart"), _console_error("boom")],
        flush_type="normal",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-config"],
        config=ReplaySignalConfig.from_names(["rage_click"]),
    )

    assert result.signals_detected == 0
    assert result.signals_inserted == 0
    assert result.issues == []
    assert store.list_replay_signals(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-config",
    ) == []


def test_replay_core_uses_persisted_signal_definitions_by_default(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    for detector in [
        "dead_click",
        "blank_render",
        "network_4xx",
        "network_5xx",
        "error_toast",
        "session_abandon_on_error",
    ]:
        store.upsert_signal_definition(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            detector=detector,
            enabled=False,
        )
    store.upsert_signal_definition(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        detector="console_error",
        enabled=False,
    )
    store.upsert_signal_definition(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        detector="rage_click",
        enabled=True,
        thresholds={"min_matches": 1},
        prompt={"instruction": "Flag repeated rage clicks only."},
        custom_definition="Repeated clicks on the same target.",
    )
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-defs",
        sequence=0,
        events=[
            _navigation("https://app.example/cart"),
            _console_error("boom"),
            _click(1, ts=100),
            _click(1, ts=200),
            _click(1, ts=300),
        ],
        flush_type="normal",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-defs"],
    )

    assert result.signals_detected == 1
    signals = store.list_replay_signals(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-defs",
    )
    assert [row["detector"] for row in signals] == ["rage_click"]
    definitions = {
        definition.detector: definition
        for definition in store.list_signal_definitions(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
        )
    }
    assert definitions["console_error"].enabled is False
    assert definitions["rage_click"].thresholds == {"min_matches": 1}
    assert definitions["rage_click"].prompt == {
        "instruction": "Flag repeated rage clicks only."
    }
    assert definitions["rage_click"].custom_definition == "Repeated clicks on the same target."
    assert definitions["rage_click"].match_count == 1
    assert definitions["rage_click"].last_match_at is not None


def test_replay_signal_definition_min_matches_filters_low_volume_matches(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.upsert_signal_definition(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        detector="console_error",
        enabled=True,
        thresholds={"min_matches": 2},
    )
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-threshold",
        sequence=0,
        events=[_navigation("https://app.example/cart"), _console_error("boom")],
        flush_type="normal",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-threshold"],
    )

    assert result.signals_detected == 0
    definitions = store.list_signal_definitions(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    assert {definition.detector: definition.match_count for definition in definitions}[
        "console_error"
    ] == 0


def test_replay_core_clusters_sessions_and_regresses_resolved_issue(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    for session_id, distinct_id in [("sess-a", "user-a"), ("sess-b", "user-b")]:
        store.insert_replay_batch(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id=session_id,
            sequence=0,
            events=[
                _navigation("https://app.example/checkout"),
                _console_error("TypeError: total is undefined"),
            ],
            flush_type="final",
            distinct_id=distinct_id,
        )

    first = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-a"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    assert len(first.issues) == 1
    assert first.issues[0].inserted is True
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["status"] == "new"
    assert issue["representative_session_id"] == "sess-a"
    assert issue["affected_users"] == 1
    assert store.resolve_replay_issue(first.issues[0].issue_id) is True

    second = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-a", "sess-b"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert len(second.issues) == 1
    assert second.issues[0].inserted is False
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["id"] == first.issues[0].issue_id
    assert issue["status"] == "regressed"
    assert issue["affected_count"] == 2
    assert issue["affected_users"] == 2
    assert issue["representative_session_id"] == "sess-a"
    assert json.loads(issue["signal_summary_json"]) == {"console_error": 2}
    sessions = store.list_replay_issue_sessions(first.issues[0].issue_id)
    assert [(row["session_id"], row["role"]) for row in sessions] == [
        ("sess-a", "representative"),
        ("sess-b", "supporting"),
    ]


def test_replay_core_counts_anonymous_sessions_with_identified_users(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    for session_id, distinct_id in [("sess-known", "user-1"), ("sess-anon", "")]:
        store.insert_replay_batch(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id=session_id,
            sequence=0,
            events=[
                _navigation("https://app.example/checkout"),
                _console_error("TypeError: total is undefined"),
            ],
            flush_type="final",
            distinct_id=distinct_id,
        )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-known", "sess-anon"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert len(result.issues) == 1
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["affected_count"] == 2
    assert issue["affected_users"] == 2


def test_replay_core_preserves_representative_session_order(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    for session_id in ["sess-z", "sess-a"]:
        store.insert_replay_batch(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id=session_id,
            sequence=0,
            events=[
                _navigation("https://app.example/checkout"),
                _console_error("Error: checkout failed"),
            ],
            flush_type="final",
        )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-z", "sess-a"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["representative_session_id"] == "sess-z"
    sessions = store.list_replay_issue_sessions(result.issues[0].issue_id)
    assert [(row["session_id"], row["role"]) for row in sessions] == [
        ("sess-z", "representative"),
        ("sess-a", "supporting"),
    ]


def test_replay_issue_lifecycle_tracks_ongoing_unresolved_and_ticket_created(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    for session_id in ["sess-a", "sess-b", "sess-c"]:
        store.insert_replay_batch(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id=session_id,
            sequence=0,
            events=[
                _navigation("https://app.example/checkout"),
                _console_error("Error: checkout failed"),
            ],
            flush_type="final",
            distinct_id="same-user",
        )

    first = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-a"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue_id = first.issues[0].issue_id

    second = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-a", "sess-b"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    assert second.issues[0].inserted is False
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["status"] == "ongoing"
    assert issue["affected_count"] == 2
    assert issue["affected_users"] == 1

    assert store.resolve_replay_issue(issue_id) is True
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["status"] == "resolved"
    assert issue["resolved_at"]
    assert store.mark_replay_issue_unresolved(issue_id) is True
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["status"] == "unresolved"
    assert issue["resolved_at"] is None
    process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-a", "sess-b", "sess-c"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["status"] == "ongoing"

    assert (
        store.mark_replay_issue_ticket_created(
            issue_id,
            external_ticket_id="RET-999",
            external_ticket_url="https://linear.app/cerebral-labs/issue/RET-999",
        )
        is True
    )
    process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-a", "sess-b", "sess-c"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["status"] == "ticket_created"
    assert issue["external_ticket_state"] == "created"
    assert issue["external_ticket_id"] == "RET-999"
    assert issue["external_ticket_url"].endswith("/RET-999")


def test_replay_issue_ignored_fingerprint_stays_ignored_on_reprocess(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    events = [
        _navigation("https://app.example/checkout"),
        _console_error("Error: checkout failed"),
    ]
    for session_id in ["sess-ignore-a", "sess-ignore-b"]:
        store.insert_replay_batch(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            session_id=session_id,
            sequence=0,
            events=events,
            flush_type="final",
        )

    first = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-ignore-a"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    assert store.ignore_replay_issue(first.issues[0].issue_id) is True

    second = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-ignore-a", "sess-ignore-b"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]

    assert second.issues[0].current_status == "ignored"
    assert second.issues[0].inserted is False
    assert issue["status"] == "ignored"
    assert issue["affected_count"] == 2


def test_replay_core_persists_ai_analysis_metadata_and_evidence(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-ai",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _click(9),
            _console_error("TypeError: total is undefined"),
        ],
        flush_type="final",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-ai"],
        config=ReplaySignalConfig.from_names(["console_error"]),
        llm_client=SuccessfulLLM(),  # type: ignore[arg-type]
    )

    assert len(result.issues) == 1
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["title"] == "Checkout total crashes"
    assert issue["analysis_status"] == "ai"
    assert issue["analysis_model"] == "qa-model"
    assert issue["analysis_prompt_version"] == "replay-analysis-v1"
    assert issue["analysis_created_at"]
    assert issue["analysis_error"] == ""
    evidence = json.loads(issue["evidence_json"])
    assert evidence["representative_session_id"] == "sess-ai"
    assert evidence["signal_summary"] == {"console_error": 1}
    assert evidence["signals"][0]["details"]["message"] == "TypeError: total is undefined"
    assert evidence["events"][0]["href"] == "https://app.example/checkout"
    click_evidence = evidence["events"][1]
    assert click_evidence["type"] == 3
    assert click_evidence["data_type"] == 2


def test_generate_spec_from_replay_issue_preserves_public_ids(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-spec",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _click(9),
            _console_error("TypeError: total is undefined"),
        ],
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-spec"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue_public_id = processed.issues[0].public_id

    generated = generate_spec_from_replay_issue(
        store=store,
        specs_dir=tmp_path / "specs",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=issue_public_id,
    )

    assert generated.issue_public_id == issue_public_id
    assert generated.replay_public_id.startswith("rpl_")
    assert generated.confidence == "low"
    assert generated.known_gaps
    assert generated.spec.execution_engine == "native"
    assert generated.spec.app_url == "https://app.example"
    assert generated.spec.exact_steps[0]["url"] == "https://app.example/checkout"
    assert generated.spec.exact_steps[1]["action"] == "click"
    assert generated.spec.fixtures["issue_public_id"] == issue_public_id
    assert generated.spec.fixtures["canonical_failure_id"]
    quality = generated.spec.fixtures["generation"]["quality"]
    assert quality["status"] == "needs_locator"
    assert quality["confidence"] == "low"
    assert quality["requires_human_edit"] is True
    assert quality["rrweb_fallback_step_count"] == 1
    assert quality["blocking_gaps"] == [
        "click-1 needs a durable locator for rrweb node 9"
    ]
    assert (tmp_path / "specs" / f"{generated.spec.spec_id}.json").exists()
    links = store.list_failure_test_links(
        failure_id=str(generated.spec.fixtures["canonical_failure_id"])
    )
    assert len(links) == 1
    assert links[0].spec_id == generated.spec.spec_id
    assert links[0].issue_public_id == issue_public_id
    assert links[0].coverage_state == "covered_unverified"


def test_generate_spec_prefers_sdk_target_selectors_over_rrweb_ids(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-sdk-spec",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _click(9, ts=100),
            _sdk_click(
                {
                    "tagName": "button",
                    "testIdAttrName": "data-test",
                    "testIdValue": "pay-button",
                    "text": "Pay now",
                },
                ts=101,
            ),
            _sdk_input(
                {
                    "tagName": "input",
                    "name": "email",
                    "ariaLabel": "Email address",
                },
                ts=200,
            ),
            _sdk_click(
                {
                    "tagName": "button",
                    "testIdAttrName": "data-qa",
                    "testIdValue": "coupon-toggle",
                },
                ts=300,
            ),
            _console_error("TypeError: total is undefined", ts=1000),
        ],
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-sdk-spec"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    generated = generate_spec_from_replay_issue(
        store=store,
        specs_dir=tmp_path / "specs",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=processed.issues[0].public_id,
    )

    click_target = generated.spec.exact_steps[1]["target"]
    assert generated.spec.exact_steps[1]["action"] == "click"
    assert click_target["selector"] == '[data-test="pay-button"]'
    assert click_target["selector_candidates"][0] == {
        "selector": '[data-test="pay-button"]',
        "strategy": "test_id",
        "score": 100,
        "rationale": "data-test is the most stable captured test selector.",
    }
    assert click_target["selector_candidates"][1]["selector"] == (
        'role=button[name="Pay now"]'
    )
    assert click_target["selector_rationale"] == (
        "data-test is the most stable captured test selector."
    )
    input_step = generated.spec.exact_steps[2]
    assert input_step["text"] == "[redacted-replay-input]"
    assert input_step["target"]["selector"] == 'role=textbox[name="Email address"]'
    assert input_step["target"]["selector_candidates"][0]["strategy"] == "role_name"
    assert "value" not in input_step
    assert generated.spec.exact_steps[3]["target"]["selector"] == '[data-qa="coupon-toggle"]'
    assert all("rrweb node" not in gap for gap in generated.known_gaps)
    assert generated.known_gaps == ["input-1 needs safe test data"]
    assert (
        generated.spec.fixtures["generation"]["unsupported_step_warnings"] == []
    )
    quality = generated.spec.fixtures["generation"]["quality"]
    assert quality["status"] == "needs_test_data"
    assert quality["confidence"] == "medium"
    assert quality["requires_human_edit"] is True
    assert quality["selector_backed_step_count"] == 3
    assert quality["rrweb_fallback_step_count"] == 0
    assert quality["editable_gaps"] == ["input-1 needs safe test data"]
    assert generated.confidence == "medium"


def test_generate_spec_ranks_semantic_selectors_before_stable_ids(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-semantic-selector",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _sdk_click(
                {
                    "tagName": "button",
                    "id": "pay_123",
                    "role": "button",
                    "text": "Pay now",
                    "className": "Button_root__abc primary",
                },
                ts=100,
            ),
            _console_error("TypeError: total is undefined", ts=1000),
        ],
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-semantic-selector"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    generated = generate_spec_from_replay_issue(
        store=store,
        specs_dir=tmp_path / "specs",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=processed.issues[0].public_id,
    )

    target = generated.spec.exact_steps[1]["target"]
    assert target["selector"] == 'role=button[name="Pay now"]'
    assert [candidate["strategy"] for candidate in target["selector_candidates"]] == [
        "role_name",
        "id",
        "text",
        "class",
    ]
    assert target["selector_candidates"][-1]["rationale"] == (
        "Class names are brittle and are only used as a last resort."
    )


def test_generate_spec_keeps_rrweb_fallbacks_not_near_sdk_events(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-mixed-spec",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _click(9, ts=100),
            _sdk_click(
                {
                    "tagName": "button",
                    "testIdAttrName": "data-testid",
                    "testIdValue": "pay-button",
                },
                ts=120,
            ),
            _click(77, ts=1000),
            _console_error("TypeError: total is undefined", ts=1200),
        ],
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-mixed-spec"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    generated = generate_spec_from_replay_issue(
        store=store,
        specs_dir=tmp_path / "specs",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=processed.issues[0].public_id,
    )

    assert generated.spec.exact_steps[1]["target"]["selector"] == '[data-testid="pay-button"]'
    assert generated.spec.exact_steps[2]["target"] == {"rrweb_id": 77}
    assert generated.known_gaps == [
        "click-2 needs a durable locator for rrweb node 77"
    ]
    assert generated.confidence == "low"
    assert generated.spec.fixtures["generation"]["quality"]["status"] == "needs_locator"


def test_generate_spec_adds_signal_assertions_and_generation_notes(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-signal-assertions",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _sdk_click(
                {
                    "tagName": "button",
                    "testIdAttrName": "data-testid",
                    "testIdValue": "checkout-pay",
                    "text": "Pay now",
                },
                ts=100,
            ),
        ],
        flush_type="final",
    )
    created = store.upsert_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        fingerprint="checkout-network-toast-blank",
        session_ids=["sess-signal-assertions"],
        signal_summary={"network_5xx": 1, "blank_render": 1, "error_toast": 1},
        first_seen_ms=100,
        last_seen_ms=500,
        title="Checkout fails after payment",
        evidence={
            "signals": [
                {
                    "detector": "network_5xx",
                    "timestamp_ms": 300,
                    "details": {
                        "method": "post",
                        "request_url": "/api/checkout",
                        "status": 500,
                    },
                }
            ]
        },
    )

    generated = generate_spec_from_replay_issue(
        store=store,
        specs_dir=tmp_path / "specs",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=created.public_id,
    )

    assertions_by_id = {
        assertion["id"]: assertion for assertion in generated.spec.assertions
    }
    assert assertions_by_id["network-failure-cleared"]["evidence"] == {
        "detector": "network_5xx",
        "method": "POST",
        "url": "/api/checkout",
        "status": 500,
    }
    assert assertions_by_id["network-error-ui-absent"]["type"] == "selector_count"
    assert assertions_by_id["network-error-ui-absent"]["expected"] == 0
    assert '[role="alert"],' not in assertions_by_id["network-error-ui-absent"]["selector"]
    assert "error" in assertions_by_id["network-error-ui-absent"]["selector"]
    assert assertions_by_id["page-not-blank"]["type"] == "selector_visible"
    assert assertions_by_id["visible-content-present"]["type"] == "text_matches"
    assert assertions_by_id["error-toast-absent"]["type"] == "selector_count"
    generation = generated.spec.fixtures["generation"]
    assert generation["human_readable_steps"] == [
        "Open https://app.example/checkout",
        'Click [data-testid="checkout-pay"]',
    ]
    assert "network-failure-cleared" in generation["human_readable_assertions"][1]
    assert "Run the app at https://app.example." in generation["preconditions"]
    assert any("redacted" in note for note in generation["fixture_notes"])
    assert generation["unsupported_step_warnings"] == []
    assert generation["quality"]["status"] == "runnable"
    assert generation["quality"]["requires_human_edit"] is False


def test_generate_api_spec_from_failed_replay_network_call(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-api-spec",
        sequence=0,
        events=[_navigation("https://app.example/checkout")],
        flush_type="final",
    )
    created = store.upsert_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        fingerprint="checkout-api-500",
        session_ids=["sess-api-spec"],
        signal_summary={"network_5xx": 1},
        first_seen_ms=100,
        last_seen_ms=500,
        title="Checkout API failed",
        evidence={
            "events": [
                {
                    "type": 4,
                    "timestamp_ms": 250,
                    "href": "https://app.example/checkout?token=secret-token&step=pay",
                }
            ],
            "signals": [
                {
                    "detector": "network_5xx",
                    "timestamp_ms": 300,
                    "details": {
                        "method": "POST",
                        "request_url": "/api/checkout?cart=abc&token=secret-token",
                        "status": 500,
                        "request_headers": {
                            "content-type": "application/json",
                            "authorization": "Bearer secret-token",
                            "x-api-key": "secret-key",
                        },
                        "request_body": json.dumps(
                            {"cart_id": "abc", "token": "secret-token"}
                        ),
                    },
                }
            ]
        },
    )

    generated = generate_api_spec_from_replay_issue(
        store=store,
        specs_dir=tmp_path / "api-specs",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=created.public_id,
    )

    spec = generated.spec
    persisted = (tmp_path / "api-specs" / f"{spec.spec_id}.json").read_text()
    loaded = load_api_spec(tmp_path / "api-specs", spec.spec_id)
    assert loaded.method == "POST"
    assert loaded.url == "https://app.example/api/checkout"
    assert loaded.query == {"cart": "abc", "token": "[redacted-api-input]"}
    assert loaded.expected_status == 500
    assert loaded.headers == {"content-type": "application/json"}
    assert loaded.auth == {
        "type": "headers",
        "headers_env": "RETRACE_API_AUTH_HEADERS",
    }
    assert json.loads(loaded.body) == {
        "cart_id": "abc",
        "token": "[redacted-api-input]",
    }
    assert loaded.fixtures["source_network_signal"]["url"] == (
        "/api/checkout?cart=abc&token=%5Bredacted-api-input%5D"
    )
    assert loaded.fixtures["api_regression"]["original_status"] == 500
    assert loaded.fixtures["api_regression"]["forbidden_status"] == 500
    assert loaded.fixtures["api_regression"]["status_assertion"] == "not_equal"
    assert loaded.fixtures["api_regression"]["trigger_context"][0]["href"] == (
        "https://app.example/checkout?token=%5Bredacted-api-input%5D&step=pay"
    )
    assert "assertion_strategy" in loaded.fixtures["api_regression"]
    assert generated.source_signal["details"]["request_url"] == (
        "/api/checkout?cart=abc&token=%5Bredacted-api-input%5D"
    )
    assert "secret-token" not in persisted
    assert "secret-key" not in persisted
    assert "secret-token" not in json.dumps(generated.source_signal)
    assert "secret-key" not in json.dumps(generated.source_signal)
    assert loaded.fixtures["source"] == "replay_issue_api"
    assert loaded.fixtures["issue_public_id"] == created.public_id
    assert loaded.fixtures["canonical_failure_id"]
    links = store.list_failure_test_links(
        failure_id=str(loaded.fixtures["canonical_failure_id"])
    )
    assert len(links) == 1
    assert links[0].spec_id == spec.spec_id
    assert links[0].source == "replay_issue_api"


def test_generate_api_spec_treats_user_visible_4xx_as_regression(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-api-400",
        sequence=0,
        events=[_navigation("https://app.example/settings")],
        flush_type="final",
    )
    created = store.upsert_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        fingerprint="settings-api-400",
        session_ids=["sess-api-400"],
        signal_summary={"network_4xx": 1},
        first_seen_ms=100,
        last_seen_ms=500,
        title="Settings API failed",
        evidence={
            "signals": [
                {
                    "detector": "network_4xx",
                    "timestamp_ms": 300,
                    "details": {
                        "method": "PATCH",
                        "request_url": "/api/settings",
                        "status": 400,
                    },
                }
            ]
        },
    )

    generated = generate_api_spec_from_replay_issue(
        store=store,
        specs_dir=tmp_path / "api-specs",
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=created.public_id,
    )

    assert generated.spec.expected_status == 400
    assert generated.spec.fixtures["api_regression"]["original_status"] == 400
    assert generated.spec.fixtures["api_regression"]["status_assertion"] == "not_equal"


def test_replay_api_body_redaction_handles_form_encoded_strings() -> None:
    body, notes = _safe_api_body(
        {"request_body": "grant_type=password&username=dev&password=secret"}
    )

    assert body == "grant_type=password&username=dev&password=%5Bredacted-api-input%5D"
    assert notes
    assert "secret" not in body


def test_promote_replay_issue_dedupes_external_ticket(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-sink",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _console_error("TypeError: total is undefined"),
        ],
        flush_type="final",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-sink"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue_public_id = processed.issues[0].public_id

    first = promote_replay_issue(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=issue_public_id,
        provider="github",
        base_url="https://retrace.example",
    )
    second = promote_replay_issue(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=issue_public_id,
        provider="github",
        base_url="https://retrace.example",
    )

    assert first.created is True
    assert first.external_id.startswith("GH-bug_")
    assert first.payload["source_public_id"] == issue_public_id
    assert first.payload["replay_links"][0]["url"].startswith("https://retrace.example")
    assert second.created is False
    assert second.external_id == first.external_id


def test_replay_core_windows_evidence_around_representative_signal(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    events = [_navigation("https://app.example/checkout", ts=0)]
    events.extend(_click(node_id, ts=node_id * 100) for node_id in range(1, 70))
    events.append(_console_error("TypeError: total is undefined", ts=6500))
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-late",
        sequence=0,
        events=events,
        flush_type="final",
    )

    process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-late"],
        config=ReplaySignalConfig.from_names(["console_error"]),
        llm_client=SuccessfulLLM(),  # type: ignore[arg-type]
    )

    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    evidence = json.loads(issue["evidence_json"])
    evidence_node_ids = {
        event["id"] for event in evidence["events"] if "id" in event
    }
    assert 65 in evidence_node_ids
    assert 1 not in evidence_node_ids
    assert any(
        event.get("plugin") == "retrace/console@1" for event in evidence["events"]
    )


def test_replay_core_uses_deterministic_fallback_when_llm_fails(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-fallback",
        sequence=0,
        events=[
            _navigation("https://app.example/cart"),
            _click(7),
            _console_error("ReferenceError: cart is not defined", ts=500),
        ],
        flush_type="final",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-fallback"],
        config=ReplaySignalConfig.from_names(["console_error"]),
        llm_client=FailingLLM(),  # type: ignore[arg-type]
    )

    assert len(result.issues) == 1
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert issue["title"] == "ReferenceError: cart is not defined on replay"
    assert issue["analysis_status"] == "fallback"
    assert issue["analysis_prompt_version"] == "replay-analysis-v1"
    assert issue["analysis_error"] == "offline"
    assert "console_error across 1 replay session(s)" in issue["summary"]
    assert "Reason codes: console_error.error_level." in issue["summary"]
    assert issue["likely_cause"].startswith("Generated from replay signals")
    evidence = json.loads(issue["evidence_json"])
    assert evidence["signals"][0]["confidence"] == "medium"
    assert evidence["signals"][0]["reason_codes"] == ["console_error.error_level"]
    assert json.loads(issue["reproduction_steps_json"]) == [
        "Open https://app.example/cart",
        "Click element id 7",
    ]


def test_replay_summary_severity_uses_all_cluster_signals() -> None:
    cluster = Cluster(
        fingerprint="mixed",
        session_ids=["low", "high"],
        signal_summary={"dead_click": 1, "network_5xx": 1},
        primary_url="https://example.com",
        first_seen_ms=10,
        last_seen_ms=20,
    )
    finding = summarize_replay_issue(
        cluster=cluster,
        events_by_session={
            "low": [_navigation("https://example.com")],
            "high": [_navigation("https://example.com/api")],
        },
        signals_by_session={
            "low": [
                Signal(
                    session_id="low",
                    detector="dead_click",
                    timestamp_ms=10,
                    url="https://example.com",
                    reason_codes=("dead_click.no_followup_dom_or_network",),
                )
            ],
            "high": [
                Signal(
                    session_id="high",
                    detector="network_5xx",
                    timestamp_ms=20,
                    url="https://example.com/api",
                    details={"request_url": "/api/save", "status": 500},
                    confidence="high",
                    reason_codes=("network_5xx.status_5xx",),
                )
            ],
        },
    )

    assert finding.severity == "high"
    assert finding.confidence == "high"
    assert "dead_click.no_followup_dom_or_network" in finding.what_happened
    assert "network_5xx.status_5xx" in finding.what_happened


def test_replay_core_processes_queued_finalize_jobs(tmp_path: Path) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-job",
        sequence=0,
        events=[
            _navigation("https://app.example/settings"),
            _console_error("Error: settings failed"),
        ],
        flush_type="final",
    )

    result = process_queued_replay_jobs(
        store=store,
        project_id=workspace.project_id,
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert result.jobs_seen == 1
    assert result.jobs_processed == 1
    assert result.jobs_failed == 0
    assert result.sessions_processed == 1
    assert result.issues_created_or_updated == 1
    jobs = store.list_processing_jobs(kind="replay.finalize", status="succeeded")
    assert len(jobs) == 1
    assert jobs[0]["last_error"] == ""
    issues = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )
    assert issues[0]["title"] == "Error: settings failed on replay"


def test_replay_job_project_filter_is_applied_before_limit(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    first = store.ensure_workspace(
        org_name="First",
        project_name="Web",
        environment_name="production",
    )
    second = store.ensure_workspace(
        org_name="Second",
        project_name="Web",
        environment_name="production",
    )
    store.insert_replay_batch(
        project_id=first.project_id,
        environment_id=first.environment_id,
        session_id="sess-first",
        sequence=0,
        events=[_navigation("https://first.example"), _console_error("Error: first")],
        flush_type="final",
    )
    store.insert_replay_batch(
        project_id=second.project_id,
        environment_id=second.environment_id,
        session_id="sess-second",
        sequence=0,
        events=[_navigation("https://second.example"), _console_error("Error: second")],
        flush_type="final",
    )

    result = process_queued_replay_jobs(
        store=store,
        project_id=second.project_id,
        limit=1,
        config=ReplaySignalConfig.from_names(["console_error"]),
    )

    assert result.jobs_seen == 1
    assert result.jobs_processed == 1
    first_jobs = store.list_processing_jobs(
        kind="replay.finalize",
        status="queued",
        project_id=first.project_id,
    )
    second_jobs = store.list_processing_jobs(
        kind="replay.finalize",
        status="succeeded",
        project_id=second.project_id,
    )
    assert len(first_jobs) == 1
    assert len(second_jobs) == 1
    second_issues = store.list_replay_issues(
        project_id=second.project_id,
        environment_id=second.environment_id,
    )
    assert second_issues[0]["title"] == "Error: second on replay"


class _FakeReplayEnricher:
    """Minimal stand-in for CorrelationEnricher used in unit tests.

    It does not extend the real class — replay_core only depends on the
    `enrich(finding, signals)` shape, so mirroring the duck-type keeps the
    test free from PostHog HTTP plumbing.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Signal]]] = []

    def enrich(self, finding, signals):
        from dataclasses import replace

        self.calls.append((finding.session_id, list(signals)))
        return replace(
            finding,
            distinct_id="user-42",
            error_issue_ids=["err-1", "err-2"],
            trace_ids=["trace-abc"],
            top_stack_frame="renderCheckout (checkout.tsx:42)",
            error_tracking_url="https://posthog/example/error/err-1",
            logs_url="https://posthog/example/logs?trace=trace-abc",
        )


def test_replay_processing_persists_correlation_when_enricher_provided(
    tmp_path: Path,
) -> None:
    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-correlation",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _console_error("TypeError: total is undefined"),
        ],
        flush_type="final",
    )

    enricher = _FakeReplayEnricher()
    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-correlation"],
        config=ReplaySignalConfig.from_names(["console_error"]),
        enricher=enricher,
    )

    assert result.sessions_with_signals == 1
    assert enricher.calls and enricher.calls[0][0] == "sess-correlation"
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert json.loads(issue["trace_ids_json"]) == ["trace-abc"]
    assert json.loads(issue["error_issue_ids_json"]) == ["err-1", "err-2"]
    assert issue["distinct_id"] == "user-42"
    assert issue["top_stack_frame"] == "renderCheckout (checkout.tsx:42)"
    assert (
        issue["error_tracking_url"]
        == "https://posthog/example/error/err-1"
    )
    assert issue["logs_url"] == "https://posthog/example/logs?trace=trace-abc"


def test_replay_processing_swallows_enricher_exceptions(tmp_path: Path) -> None:
    """A misbehaving enricher must never break replay processing."""

    class ExplodingEnricher:
        def enrich(self, finding, signals):
            raise RuntimeError("query api down")

    store, workspace = _workspace(tmp_path)
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-explode",
        sequence=0,
        events=[
            _navigation("https://app.example/cart"),
            _console_error("boom"),
        ],
        flush_type="final",
    )

    result = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-explode"],
        config=ReplaySignalConfig.from_names(["console_error"]),
        enricher=ExplodingEnricher(),
    )

    assert result.sessions_with_signals == 1
    issue = store.list_replay_issues(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )[0]
    assert json.loads(issue["trace_ids_json"]) == []
    assert issue["error_tracking_url"] == ""
