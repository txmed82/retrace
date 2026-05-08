from pathlib import Path
import time

import pytest

from retrace.evidence import EvidenceItem, evidence_items_from_replay_issue
from retrace.storage import Storage


def test_append_and_list_failure_evidence_chronologically(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    failure_id = store.upsert_failure(
        _failure(
            project_id="proj_1",
            environment_id="env_1",
            source_external_id="manual-1",
        )
    )

    later = EvidenceItem(
        failure_id=failure_id,
        evidence_type="console_log",
        occurred_at_ms=200,
        source="manual",
        redaction_state="raw",
        payload={"message": "later"},
    )
    earlier = EvidenceItem(
        failure_id=failure_id,
        evidence_type="network_request",
        occurred_at_ms=100,
        source="manual",
        redaction_state="redacted",
        payload={"status": 500},
    )
    store.append_failure_evidence(later)
    store.append_failure_evidence(earlier)

    rows = store.list_failure_evidence(failure_id=failure_id)
    assert [row.evidence_type for row in rows] == ["network_request", "console_log"]
    assert rows[0].payload == {"status": 500}


def test_failure_evidence_preserves_append_order_for_same_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    failure_id = store.upsert_failure(
        _failure(
            project_id="proj_1",
            environment_id="env_1",
            source_external_id="manual-same-time",
        )
    )
    ids = iter(["ev_z", "ev_a"])
    monkeypatch.setattr(store, "_id", lambda prefix: next(ids))

    store.append_failure_evidence(
        EvidenceItem(
            failure_id=failure_id,
            evidence_type="console_log",
            occurred_at_ms=100,
            source="manual",
            redaction_state="raw",
            payload={"message": "first"},
        )
    )
    time.sleep(0.001)
    store.append_failure_evidence(
        EvidenceItem(
            failure_id=failure_id,
            evidence_type="console_log",
            occurred_at_ms=100,
            source="manual",
            redaction_state="raw",
            payload={"message": "second"},
        )
    )

    rows = store.list_failure_evidence(failure_id=failure_id)
    assert [row.id for row in rows] == ["ev_z", "ev_a"]
    assert [row.payload["message"] for row in rows] == ["first", "second"]


def test_sensitive_evidence_can_be_excluded_for_prompts(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    failure_id = store.upsert_failure(
        _failure(
            project_id="proj_1",
            environment_id="env_1",
            source_external_id="manual-2",
        )
    )
    store.append_failure_evidence(
        EvidenceItem(
            failure_id=failure_id,
            evidence_type="api_request_response",
            occurred_at_ms=100,
            source="manual",
            redaction_state="sensitive",
            payload={"authorization": "Bearer secret"},
        )
    )
    store.append_failure_evidence(
        EvidenceItem(
            failure_id=failure_id,
            evidence_type="ci_log_excerpt",
            occurred_at_ms=200,
            source="manual",
            redaction_state="redacted",
            payload={"line": "Assertion failed"},
        )
    )

    prompt_rows = store.list_failure_evidence(
        failure_id=failure_id,
        include_sensitive=False,
    )
    assert [row.evidence_type for row in prompt_rows] == ["ci_log_excerpt"]
    assert prompt_rows[0].safe_for_prompts is True


def test_prompt_safe_filter_excludes_unknown_redaction_states(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    failure_id = store.upsert_failure(
        _failure(
            project_id="proj_1",
            environment_id="env_1",
            source_external_id="manual-unknown-redaction",
        )
    )
    store.append_failure_evidence(
        EvidenceItem(
            failure_id=failure_id,
            evidence_type="console_log",
            occurred_at_ms=100,
            source="manual",
            redaction_state="raw",
            payload={"message": "safe"},
        )
    )
    with store._conn() as conn:
        conn.execute(
            """
            INSERT INTO failure_evidence
            (id, failure_id, evidence_type, occurred_at_ms, source,
             redaction_state, payload_json)
            VALUES ('ev_unknown', ?, 'console_log', 200, 'legacy', 'unknown', '{}')
            """,
            (failure_id,),
        )

    rows = store.list_failure_evidence(
        failure_id=failure_id,
        include_sensitive=False,
    )
    assert [row.redaction_state for row in rows] == ["raw"]
    all_rows = store.list_failure_evidence(failure_id=failure_id)
    assert [row.redaction_state for row in all_rows] == ["raw", "unknown"]
    assert [row.safe_for_prompts for row in all_rows] == [True, False]


def test_evidence_payload_must_be_json_serializable(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    failure_id = store.upsert_failure(
        _failure(
            project_id="proj_1",
            environment_id="env_1",
            source_external_id="manual-3",
        )
    )

    with pytest.raises(ValueError, match="JSON-serializable"):
        store.append_failure_evidence(
            EvidenceItem(
                failure_id=failure_id,
                evidence_type="custom",
                occurred_at_ms=1,
                source="manual",
                redaction_state="raw",
                payload={"bad": object()},
            )
        )


def test_replay_issue_backfills_typed_evidence(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    result = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="network-500",
        session_ids=["sess_1"],
        signal_summary={"network_5xx": 1},
        first_seen_ms=100,
        last_seen_ms=200,
        evidence={
            "signals": [
                {
                    "detector": "network_5xx",
                    "timestamp_ms": 200,
                    "url": "https://example.test/checkout",
                    "details": {"status": 500},
                }
            ],
            "events": [
                {
                    "type": 3,
                    "timestamp_ms": 150,
                    "source": 2,
                    "data_type": 2,
                }
            ],
        },
    )
    issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=result.public_id,
    )
    assert issue is not None

    evidence = store.list_failure_evidence(
        failure_id=str(issue["canonical_failure_id"]),
    )
    assert [row.evidence_type for row in evidence] == [
        "replay_event",
        "network_request",
    ]
    assert all(row.redaction_state == "redacted" for row in evidence)

    store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="network-500",
        session_ids=["sess_1"],
        signal_summary={"network_5xx": 1},
        first_seen_ms=100,
        last_seen_ms=200,
        evidence={
            "signals": [
                {
                    "detector": "network_5xx",
                    "timestamp_ms": 200,
                    "url": "https://example.test/checkout",
                    "details": {"status": 500},
                }
            ],
            "events": [
                {
                    "type": 3,
                    "timestamp_ms": 150,
                    "source": 2,
                    "data_type": 2,
                }
            ],
        },
    )
    assert len(store.list_failure_evidence(failure_id=str(issue["canonical_failure_id"]))) == 2


def test_replay_evidence_builder_falls_back_to_bundle() -> None:
    items = evidence_items_from_replay_issue(
        failure_id="flr_1",
        issue_public_id="bug_1",
        evidence={"custom": {"key": "value"}},
    )

    assert len(items) == 1
    assert items[0].evidence_type == "replay_evidence_bundle"
    assert items[0].safe_for_prompts is True


def test_replay_evidence_builder_preserves_canonical_issue_public_id() -> None:
    items = evidence_items_from_replay_issue(
        failure_id="flr_1",
        issue_public_id="bug_canonical",
        evidence={
            "signals": [
                {
                    "detector": "network_5xx",
                    "timestamp_ms": 1,
                    "issue_public_id": "bug_spoofed",
                }
            ],
            "events": [
                {
                    "type": 3,
                    "timestamp_ms": 2,
                    "issue_public_id": "bug_spoofed",
                }
            ],
        },
    )

    assert [item.payload["issue_public_id"] for item in items] == [
        "bug_canonical",
        "bug_canonical",
    ]


def _failure(*, project_id: str, environment_id: str, source_external_id: str):
    from retrace.failures import canonical_failure_from_monitor_incident

    return canonical_failure_from_monitor_incident(
        project_id=project_id,
        environment_id=environment_id,
        provider="manual",
        external_id=source_external_id,
        title="Manual failure",
    )
