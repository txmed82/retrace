from __future__ import annotations

from pathlib import Path

from retrace.storage import Storage


def test_repair_task_links_failure_and_multiple_evidence_items(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    created = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="checkout-dead-click",
        session_ids=["sess_1"],
        title="Checkout click does nothing",
        signal_summary={"dead_click": 1, "console_error": 1},
        first_seen_ms=100,
        last_seen_ms=120,
        evidence={
            "signals": [
                {"detector": "dead_click", "timestamp_ms": 100, "selector": "#pay"},
                {"detector": "console_error", "timestamp_ms": 120, "message": "boom"},
            ],
        },
    )
    issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=created.public_id,
    )
    assert issue is not None
    failure_id = str(issue["canonical_failure_id"])
    evidence = store.list_failure_evidence(
        failure_id=failure_id,
        include_sensitive=False,
    )
    assert len(evidence) == 2

    task_id = store.upsert_repair_task(
        failure_id=failure_id,
        title="Repair checkout click",
        source_type="replay_issue",
        source_external_id=created.public_id,
        likely_files=["src/checkout.tsx", "src/checkout.tsx"],
        prompt_artifacts=[
            {"artifact_type": "repair_prompt", "path": "reports/fix.codex.md"}
        ],
        validation_commands=["uv run pytest tests/test_checkout.py"],
        risk_notes="Payment flow regression risk.",
        evidence_ids=[item.id for item in evidence],
    )

    task = store.get_repair_task(task_id)
    assert task is not None
    assert task.public_id.startswith("rpr_")
    assert task.failure_id == failure_id
    assert task.source_external_id == created.public_id
    assert task.likely_files == ["src/checkout.tsx"]
    assert task.prompt_artifacts[0]["path"] == "reports/fix.codex.md"
    assert task.validation_commands == ["uv run pytest tests/test_checkout.py"]
    assert set(task.evidence_ids) == {item.id for item in evidence}

    failure = store.get_failure(
        project_id="proj_1",
        environment_id="env_1",
        failure_id=failure_id,
    )
    assert failure is not None
    assert failure.linked_repair_task_id == task_id

    store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="checkout-dead-click",
        session_ids=["sess_2"],
        title="Checkout click does nothing",
        signal_summary={"dead_click": 1},
        first_seen_ms=200,
        last_seen_ms=200,
    )
    refreshed = store.get_failure(
        project_id="proj_1",
        environment_id="env_1",
        failure_id=failure_id,
    )
    assert refreshed is not None
    assert refreshed.linked_repair_task_id == task_id
