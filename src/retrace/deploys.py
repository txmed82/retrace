from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Any

from retrace.storage import DeployMarkerRow, FailureRow, Storage


@dataclass(frozen=True)
class DeployCorrelationResult:
    failure_id: str
    deploy_id: str
    deploy_sha: str
    changed_files: list[str]


def record_deploy(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    sha: str,
    branch: str = "",
    author: str = "",
    deployed_at_ms: int = 0,
    changed_files: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeployMarkerRow:
    deploy_id = store.record_deploy_marker(
        project_id=project_id,
        environment_id=environment_id,
        sha=sha,
        branch=branch,
        author=author,
        deployed_at_ms=deployed_at_ms or int(time() * 1000),
        changed_files=changed_files or [],
        metadata=metadata or {},
    )
    deploy = store.get_deploy_marker(deploy_id)
    assert deploy is not None
    return deploy


def correlate_failure_to_deploy(
    *,
    store: Storage,
    failure_id: str,
) -> DeployCorrelationResult | None:
    failure = store.get_failure_by_id(failure_id)
    if failure is None:
        raise ValueError(f"unknown failure_id: {failure_id}")
    at_ms = _failure_time_ms(failure)
    if at_ms <= 0:
        return None
    deploy = store.nearest_deploy_marker(
        project_id=failure.project_id,
        environment_id=failure.environment_id,
        at_ms=at_ms,
    )
    if deploy is None:
        return None
    store.update_failure_deploy(failure_id=failure.id, deploy_sha=deploy.sha)
    return DeployCorrelationResult(
        failure_id=failure.id,
        deploy_id=deploy.id,
        deploy_sha=deploy.sha,
        changed_files=deploy.changed_files,
    )


def correlate_recent_failures_to_deploys(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    limit: int = 100,
) -> list[DeployCorrelationResult]:
    out: list[DeployCorrelationResult] = []
    for failure in store.list_failures(
        project_id=project_id,
        environment_id=environment_id,
        limit=limit,
    ):
        result = correlate_failure_to_deploy(store=store, failure_id=failure.id)
        if result is not None:
            out.append(result)
    return out


def changed_files_for_failure(*, store: Storage, failure: FailureRow) -> list[str]:
    if not failure.related_deploy_sha:
        return []
    for deploy in store.list_deploy_markers(
        project_id=failure.project_id,
        environment_id=failure.environment_id,
        limit=100,
    ):
        if deploy.sha == failure.related_deploy_sha:
            return deploy.changed_files
    return []


def _failure_time_ms(failure: FailureRow) -> int:
    for value in (failure.first_seen_ms, failure.last_seen_ms):
        if int(value or 0) > 0:
            return int(value)
    return 0
