from __future__ import annotations

from pathlib import Path

from retrace.fix_suggestions import (
    generate_fix_suggestions,
    parsed_finding_from_replay_issue,
    replay_issue_report_key,
)
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
    other = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="profile-dead-click",
        session_ids=["sess_other"],
        title="Profile click does nothing",
        signal_summary={"dead_click": 1},
        first_seen_ms=300,
        last_seen_ms=300,
        evidence={
            "signals": [
                {"detector": "dead_click", "timestamp_ms": 300, "selector": "#profile"}
            ],
        },
    )
    other_issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=other.public_id,
    )
    assert other_issue is not None
    other_evidence = store.list_failure_evidence(
        failure_id=str(other_issue["canonical_failure_id"]),
        include_sensitive=False,
    )
    assert len(other_evidence) == 1

    task_id = store.upsert_repair_task(
        failure_id=failure_id,
        title="Repair checkout click",
        source_type="replay_issue",
        source_external_id=created.public_id,
        status="not-a-real-status",
        likely_files=["src/checkout.tsx", "src/checkout.tsx"],
        prompt_artifacts=[
            {"artifact_type": "repair_prompt", "path": "reports/fix.codex.md"}
        ],
        validation_commands=["uv run pytest tests/test_checkout.py"],
        risk_notes="Payment flow regression risk.",
        evidence_ids=[item.id for item in evidence] + ["ev_missing", other_evidence[0].id],
    )

    task = store.get_repair_task(task_id)
    assert task is not None
    assert task.public_id.startswith("rpr_")
    assert task.project_id == "proj_1"
    assert task.environment_id == "env_1"
    assert task.failure_id == failure_id
    assert task.source_external_id == created.public_id
    assert task.status == "open"
    assert task.likely_files == ["src/checkout.tsx"]
    assert task.prompt_artifacts[0]["path"] == "reports/fix.codex.md"
    assert task.validation_commands == ["uv run pytest tests/test_checkout.py"]
    assert set(task.evidence_ids) == {item.id for item in evidence}

    store.upsert_repair_task(
        failure_id=failure_id,
        title="Repair checkout click",
        source_type="replay_issue",
        source_external_id=created.public_id,
        evidence_ids=[evidence[0].id],
    )
    refreshed_task = store.get_repair_task(task_id)
    assert refreshed_task is not None
    assert refreshed_task.evidence_ids == [evidence[0].id]

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


def test_repair_task_failure_does_not_block_prompt_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    created = store.upsert_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        fingerprint="checkout-error",
        session_ids=["sess_1"],
        title="Checkout error",
        signal_summary={"console_error": 1},
        first_seen_ms=100,
        last_seen_ms=100,
    )
    issue = store.get_replay_issue(
        project_id="proj_1",
        environment_id="env_1",
        issue_id=created.public_id,
    )
    assert issue is not None
    store.upsert_github_repo(
        repo_full_name="acme/widgets",
        default_branch="main",
        local_path=str(tmp_path),
    )
    repo = store.get_github_repo("acme/widgets")
    assert repo is not None

    def fail_repair_task(**_kwargs):
        raise RuntimeError("repair task unavailable")

    monkeypatch.setattr(store, "upsert_repair_task", fail_repair_task)

    result = generate_fix_suggestions(
        store=store,
        repo=repo,
        repo_path=tmp_path,
        out_dir=tmp_path / "fix-prompts",
        report_key=replay_issue_report_key(created.public_id),
        source_label=f"replay issue {created.public_id}",
        artifact_stem="replay-checkout",
        findings=[parsed_finding_from_replay_issue(issue)],
        project_id="proj_1",
        environment_id="env_1",
    )

    assert result.generated == 1
    assert result.artifacts[0].repair_task_id == ""
    assert (result.out_dir / result.artifacts[0].artifact_json).exists()
    assert (result.out_dir / result.artifacts[0].prompt_files["codex"]).exists()
