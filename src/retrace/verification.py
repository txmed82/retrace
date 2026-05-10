from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from retrace.api_testing import (
    api_runs_dir_for_data_dir,
    api_specs_dir_for_data_dir,
    load_api_spec,
    run_api_spec,
)
from retrace.tester import (
    load_spec,
    run_spec,
    runs_dir_for_data_dir,
    specs_dir_for_data_dir,
)


@dataclass(frozen=True)
class VerificationTestRef:
    kind: str
    spec_id: str
    coverage_link_id: str = ""
    spec_name: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationPlan:
    failure_id: str
    failure_public_id: str
    repair_task_id: str = ""
    tests: list[VerificationTestRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_id": self.failure_id,
            "failure_public_id": self.failure_public_id,
            "repair_task_id": self.repair_task_id,
            "tests": [item.to_dict() for item in self.tests],
        }


@dataclass(frozen=True)
class VerificationRunItem:
    kind: str
    spec_id: str
    coverage_link_id: str
    ok: bool
    status: str
    run_id: str = ""
    error: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationResult:
    status: str
    failure_id: str
    failure_public_id: str
    repair_task_id: str = ""
    tests: list[VerificationRunItem] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "failure_id": self.failure_id,
            "failure_public_id": self.failure_public_id,
            "repair_task_id": self.repair_task_id,
            "tests": [item.to_dict() for item in self.tests],
            "error": self.error,
        }


def plan_repair_verification(
    *,
    store: Any,
    data_dir: Path,
    repair_task_id: str = "",
    failure_id: str = "",
) -> VerificationPlan:
    failure, repair_task = _failure_and_task(
        store=store,
        repair_task_id=repair_task_id,
        failure_id=failure_id,
    )
    refs: list[VerificationTestRef] = []
    for link in store.list_all_failure_test_links(failure_id=failure.id):
        kind = _linked_test_kind(data_dir=data_dir, link=link)
        if not kind:
            continue
        refs.append(
            VerificationTestRef(
                kind=kind,
                spec_id=link.spec_id,
                coverage_link_id=link.id,
                spec_name=link.spec_name,
                source=link.source,
            )
        )
    return VerificationPlan(
        failure_id=failure.id,
        failure_public_id=failure.public_id,
        repair_task_id=repair_task.id if repair_task is not None else "",
        tests=refs,
    )


def run_repair_verification(
    *,
    store: Any,
    data_dir: Path,
    cwd: Path,
    repair_task_id: str = "",
    failure_id: str = "",
    dry_run: bool = False,
) -> VerificationResult:
    plan = plan_repair_verification(
        store=store,
        data_dir=data_dir,
        repair_task_id=repair_task_id,
        failure_id=failure_id,
    )
    if dry_run:
        return VerificationResult(
            status="planned" if plan.tests else "blocked",
            failure_id=plan.failure_id,
            failure_public_id=plan.failure_public_id,
            repair_task_id=plan.repair_task_id,
            tests=[
                VerificationRunItem(
                    kind=item.kind,
                    spec_id=item.spec_id,
                    coverage_link_id=item.coverage_link_id,
                    ok=False,
                    status="planned",
                )
                for item in plan.tests
            ],
            error="" if plan.tests else "No linked Retrace specs found.",
        )
    if not plan.tests:
        _record_verification(
            store=store,
            plan=plan,
            status="blocked",
            tests=[],
            error="No linked Retrace specs found.",
        )
        return VerificationResult(
            status="blocked",
            failure_id=plan.failure_id,
            failure_public_id=plan.failure_public_id,
            repair_task_id=plan.repair_task_id,
            error="No linked Retrace specs found.",
        )

    runs: list[VerificationRunItem] = []
    for test in plan.tests:
        runs.append(
            _run_one_test(
                store=store,
                data_dir=data_dir,
                cwd=cwd,
                test=test,
            )
        )
    if any(item.status == "blocked" for item in runs):
        status = "blocked"
        error = "One or more linked specs could not run."
    elif any(not item.ok for item in runs):
        status = "failed"
        error = "One or more linked specs failed."
    else:
        status = "passed"
        error = ""
    _record_verification(
        store=store,
        plan=plan,
        status=status,
        tests=runs,
        error=error,
    )
    return VerificationResult(
        status=status,
        failure_id=plan.failure_id,
        failure_public_id=plan.failure_public_id,
        repair_task_id=plan.repair_task_id,
        tests=runs,
        error=error,
    )


def _failure_and_task(
    *,
    store: Any,
    repair_task_id: str,
    failure_id: str,
) -> tuple[Any, Any | None]:
    repair_task = None
    if repair_task_id.strip():
        repair_task = store.get_repair_task(repair_task_id.strip())
        if repair_task is None:
            raise ValueError(f"unknown repair_task_id: {repair_task_id}")
        failure_id = repair_task.failure_id
    failure = store.get_failure_by_id(failure_id.strip())
    if failure is None:
        raise ValueError(f"unknown failure_id: {failure_id}")
    if repair_task is None and failure.linked_repair_task_id:
        repair_task = store.get_repair_task(failure.linked_repair_task_id)
    return failure, repair_task


def _linked_test_kind(*, data_dir: Path, link: Any) -> str:
    if (api_specs_dir_for_data_dir(data_dir) / f"{link.spec_id}.json").exists():
        return "api"
    if (specs_dir_for_data_dir(data_dir) / f"{link.spec_id}.json").exists():
        return "ui"
    path = str(link.spec_path or "").lower()
    if "/api-tests/" in path or "api-tests" in path:
        return "api"
    if "/ui-tests/" in path or "ui-tests" in path:
        return "ui"
    return ""


def _run_one_test(
    *,
    store: Any,
    data_dir: Path,
    cwd: Path,
    test: VerificationTestRef,
) -> VerificationRunItem:
    try:
        if test.kind == "api":
            spec = load_api_spec(api_specs_dir_for_data_dir(data_dir), test.spec_id)
            result = run_api_spec(
                spec=spec,
                runs_dir=api_runs_dir_for_data_dir(data_dir),
            )
        else:
            spec = load_spec(specs_dir_for_data_dir(data_dir), test.spec_id)
            result = run_spec(
                spec=spec,
                runs_dir=runs_dir_for_data_dir(data_dir),
                cwd=cwd,
            )
        if test.coverage_link_id:
            store.update_failure_test_link_run(
                spec_id=result.spec_id,
                run_result=result,
                link_id=test.coverage_link_id,
            )
        return VerificationRunItem(
            kind=test.kind,
            spec_id=result.spec_id,
            coverage_link_id=test.coverage_link_id,
            ok=bool(result.ok),
            status=str(result.status),
            run_id=str(result.run_id),
            error=str(getattr(result, "error", "") or ""),
            artifacts=list(getattr(result, "artifacts", []) or []),
        )
    except Exception as exc:
        return VerificationRunItem(
            kind=test.kind,
            spec_id=test.spec_id,
            coverage_link_id=test.coverage_link_id,
            ok=False,
            status="blocked",
            error=str(exc),
        )


def _record_verification(
    *,
    store: Any,
    plan: VerificationPlan,
    status: str,
    tests: list[VerificationRunItem],
    error: str,
) -> None:
    metadata = {
        "last_verification": {
            "status": status,
            "tests": [item.to_dict() for item in tests],
            "error": error,
        }
    }
    if status == "passed":
        store.update_failure_status(
            failure_id=plan.failure_id,
            status="resolved",
            metadata=metadata,
        )
        if plan.repair_task_id:
            store.update_repair_task_status(
                repair_task_id=plan.repair_task_id,
                status="resolved",
                metadata=metadata,
            )
    elif status == "failed":
        store.update_failure_status(
            failure_id=plan.failure_id,
            status="regressed",
            metadata=metadata,
        )
        if plan.repair_task_id:
            store.update_repair_task_status(
                repair_task_id=plan.repair_task_id,
                status="ready_for_validation",
                metadata=metadata,
            )
    else:
        failure = store.get_failure_by_id(plan.failure_id)
        store.update_failure_status(
            failure_id=plan.failure_id,
            status=str(getattr(failure, "status", "") or "triaged"),
            metadata=metadata,
        )
    if status == "blocked" and plan.repair_task_id:
        store.update_repair_task_status(
            repair_task_id=plan.repair_task_id,
            status="blocked",
            metadata=metadata,
        )
