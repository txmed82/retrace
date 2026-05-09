from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ValidationCommand:
    command: str
    reason: str
    source: str = "repo_inspection"


def infer_validation_commands(
    *,
    repo_path: Path | None,
    linked_tests: list[dict[str, Any]] | None = None,
    likely_files: list[str] | None = None,
    failure_metadata: dict[str, Any] | None = None,
) -> list[ValidationCommand]:
    commands: list[ValidationCommand] = []
    for link in linked_tests or []:
        spec_id = _safe_spec_id(link.get("spec_id"))
        if not spec_id:
            continue
        spec_path = str(link.get("spec_path") or "")
        if _looks_like_api_spec(spec_path, spec_id):
            commands.append(
                ValidationCommand(
                    command=f"retrace tester api-run {spec_id}",
                    reason=f"Runs linked API spec {spec_id}.",
                    source="linked_test",
                )
            )
        else:
            commands.append(
                ValidationCommand(
                    command=f"retrace tester run {spec_id}",
                    reason=f"Runs linked UI spec {spec_id}.",
                    source="linked_test",
                )
            )
    metadata = failure_metadata or {}
    metadata_spec_id = _safe_spec_id(metadata.get("spec_id"))
    if metadata_spec_id:
        commands.append(
            ValidationCommand(
                command=f"retrace tester api-run {metadata_spec_id}",
                reason=f"Re-runs failing API/test spec {metadata_spec_id}.",
                source="failure_metadata",
            )
        )
    if repo_path is not None and repo_path.exists():
        commands.extend(_targeted_repo_commands(repo_path, likely_files or []))
        commands.extend(_package_manager_commands(repo_path))
    return _dedupe_safe_commands(commands)


def _targeted_repo_commands(
    repo_path: Path,
    likely_files: list[str],
) -> list[ValidationCommand]:
    commands: list[ValidationCommand] = []
    for rel in likely_files:
        path = repo_path / rel
        if rel.startswith("tests/") and path.suffix == ".py" and path.exists():
            commands.append(
                ValidationCommand(
                    command=f"uv run pytest {shlex.quote(rel)}",
                    reason=f"Runs the directly linked Python test {rel}.",
                    source="likely_file",
                )
            )
            continue
        candidate = repo_path / "tests" / f"test_{Path(rel).stem}.py"
        if candidate.exists():
            test_rel = candidate.relative_to(repo_path).as_posix()
            commands.append(
                ValidationCommand(
                    command=f"uv run pytest {shlex.quote(test_rel)}",
                    reason=f"Runs the nearest Python regression test for {rel}.",
                    source="likely_file",
                )
            )
    return commands


def _package_manager_commands(repo_path: Path) -> list[ValidationCommand]:
    commands: list[ValidationCommand] = []
    if (repo_path / "pyproject.toml").exists() and (repo_path / "tests").exists():
        commands.append(
            ValidationCommand(
                command="uv run pytest",
                reason="Runs the repository Python test suite from pyproject.toml.",
                source="repo_files",
            )
        )
    package_json = repo_path / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else {}
        if isinstance(scripts, dict) and scripts.get("test"):
            commands.append(
                ValidationCommand(
                    command=_js_test_command(repo_path),
                    reason="Runs the package.json test script.",
                    source="repo_files",
                )
            )
    return commands


def _js_test_command(repo_path: Path) -> str:
    if (repo_path / "pnpm-lock.yaml").exists():
        return "pnpm test"
    if (repo_path / "yarn.lock").exists():
        return "yarn test"
    return "npm test"


def _dedupe_safe_commands(commands: list[ValidationCommand]) -> list[ValidationCommand]:
    out: list[ValidationCommand] = []
    seen: set[str] = set()
    for item in commands:
        command = item.command.strip()
        if not command or command in seen or not _is_safe_validation_command(command):
            continue
        seen.add(command)
        out.append(item)
    return out


def _is_safe_validation_command(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    if parts[:3] == ["uv", "run", "pytest"]:
        return True
    if parts[0] == "pytest":
        return True
    if parts[:3] == ["python", "-m", "pytest"]:
        return True
    if parts[:2] in (["npm", "test"], ["pnpm", "test"], ["yarn", "test"]):
        return True
    if parts[:3] in (
        ["retrace", "tester", "run"],
        ["retrace", "tester", "api-run"],
    ):
        return len(parts) == 4 and bool(_safe_spec_id(parts[3]))
    return False


def _looks_like_api_spec(spec_path: str, spec_id: str) -> bool:
    text = f"{spec_path} {spec_id}".lower()
    return "api" in text or spec_path.endswith(".json")


def _safe_spec_id(value: object) -> str:
    spec_id = str(value or "").strip()
    return spec_id if re.fullmatch(r"[A-Za-z0-9_-]+", spec_id) else ""
