"""Sanity tests for the composite actions under `.github/actions/`.

These are the inputs/outputs documented in `docs/github-actions.md`.
If we ever rename or drop one, the doc and downstream workflows break —
this test pins the contract so the rename is loud.

The tests are intentionally cheap: parse the YAML, assert structural
invariants. We do NOT spin up a runner / call GitHub Actions APIs;
that would be a separate end-to-end against a scratch repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


ACTIONS_DIR = Path(__file__).resolve().parents[1] / ".github" / "actions"


def _load_action(name: str) -> dict:
    path = ACTIONS_DIR / name / "action.yml"
    assert path.is_file(), f"composite action missing: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# Pinned contracts: input name -> "required" flag we promise to callers.
# Renaming or flipping these is a docs-affecting change.
_PR_REVIEW_INPUTS = {
    "pr-number": False,
    "repo": False,
    "post-comment": False,
    "run-affected-tests": False,
    "use-llm": False,
    "llm-self-critique": False,
    "python-version": False,
    "retrace-ref": False,
    "llm-provider": False,
    "llm-base-url": False,
    "llm-model": False,
    "llm-api-key": False,
    "working-directory": False,
}

_SOURCE_MAP_INPUTS = {
    "api-base-url": True,
    "service-token": True,
    "environment-id": True,
    "source-map-dir": True,
    "sha": False,
    "artifact-prefix": False,
    "branch": False,
    "author": False,
    "record-deploy": False,
    "fail-on-upload-error": False,
}

_QA_AUTO_INPUTS = {
    "repo": False,
    "incident-id": False,
    "project-id": False,
    "environment-id": False,
    "base-branch": False,
    "app-url": False,
    "execution-engine": False,
    "apply-with": False,
    "draft": False,
    "no-pr": False,
    "python-version": False,
    "retrace-ref": False,
    "working-directory": False,
}


# Outputs each action promises.
_OUTPUTS = {
    "pr-review": {"comment-url"},
    "source-map-upload": {"uploaded-count", "skipped-count", "deploy-public-id"},
    "qa-auto": {"incident-id", "pr-url"},
}


@pytest.mark.parametrize(
    "name,expected_inputs",
    [
        ("pr-review", _PR_REVIEW_INPUTS),
        ("source-map-upload", _SOURCE_MAP_INPUTS),
        ("qa-auto", _QA_AUTO_INPUTS),
    ],
)
def test_action_inputs_match_documented_contract(name, expected_inputs):
    action = _load_action(name)
    actual = action.get("inputs") or {}

    # Every expected input must be declared.
    missing = set(expected_inputs) - set(actual)
    assert not missing, f"{name}: inputs missing from action.yml: {sorted(missing)}"

    # No surprise inputs — would have to update the docs.
    extra = set(actual) - set(expected_inputs)
    assert not extra, f"{name}: undocumented inputs in action.yml: {sorted(extra)}"

    # Required flag matches the contract.
    for key, want_required in expected_inputs.items():
        got = bool(actual[key].get("required"))
        assert got == want_required, (
            f"{name}.{key}: required={got} (expected {want_required})"
        )


@pytest.mark.parametrize(
    "name,expected_outputs",
    list(_OUTPUTS.items()),
)
def test_action_outputs_match_documented_contract(name, expected_outputs):
    action = _load_action(name)
    actual = set((action.get("outputs") or {}).keys())
    assert actual == expected_outputs, (
        f"{name}: outputs mismatch (got {sorted(actual)}, want {sorted(expected_outputs)})"
    )


@pytest.mark.parametrize(
    "name", ["pr-review", "source-map-upload", "qa-auto"],
)
def test_action_runs_composite_with_steps(name):
    """All three are composite shell-step actions — no Docker, no Node.
    Keeps cold start fast and avoids needing a registry."""
    action = _load_action(name)
    runs = action.get("runs") or {}
    assert runs.get("using") == "composite", f"{name}: runs.using must be 'composite'"
    steps = runs.get("steps") or []
    assert steps, f"{name}: runs.steps must not be empty"
    for step in steps:
        # Every shell step declares its shell explicitly. Steps that
        # invoke other actions via `uses:` are allowed to omit `shell`.
        if "uses" not in step:
            assert "shell" in step, f"{name}: shell step missing `shell:` key"
            assert step["shell"] == "bash", (
                f"{name}: shell={step['shell']!r} — keep all steps on bash for portability"
            )


def test_pr_review_uses_setup_python():
    """pr-review needs Python in the runner; assert setup-python is wired."""
    action = _load_action("pr-review")
    steps = action["runs"]["steps"]
    uses = [s.get("uses") for s in steps if isinstance(s.get("uses"), str)]
    assert any(u.startswith("actions/setup-python@") for u in uses), (
        "pr-review must use actions/setup-python"
    )


def test_source_map_upload_has_no_python_dependency():
    """Source-map upload is intentionally dep-free (curl + jq only).
    If someone adds `setup-python` here, they're paying ~10s of CI time
    for no reason — flag it loudly."""
    action = _load_action("source-map-upload")
    steps = action["runs"]["steps"]
    uses = [s.get("uses") for s in steps if isinstance(s.get("uses"), str)]
    assert not any("setup-python" in u for u in uses), (
        "source-map-upload should stay Python-free — curl + jq is enough"
    )


def test_pr_review_on_mode_requires_both_base_url_and_api_key():
    """Regression for CodeRabbit Minor on PR #129: the validation step
    must reject `use-llm=on` when EITHER `llm-base-url` OR `llm-api-key`
    is empty — the error message claims both are required, so the
    check should match the message.
    """
    action = _load_action("pr-review")
    steps = action["runs"]["steps"]
    validate = next(s for s in steps if s.get("name") == "Validate inputs")
    body = validate["run"]
    # The on-mode guard must mention both inputs.
    assert "inputs.llm-base-url" in body
    assert "inputs.llm-api-key" in body
    # And the guard must error on either-empty, not just one.
    assert "[ -z \"${{ inputs.llm-base-url }}\" ]" in body
    assert "[ -z \"${{ inputs.llm-api-key }}\" ]" in body


def test_source_map_upload_normalizes_trailing_slash():
    """Regression for CodeRabbit Minor on PR #129: a caller passing
    `source-map-dir: dist/` (with trailing slash) must not produce
    `artifact_url` values with double slashes after the prefix
    strip."""
    action = _load_action("source-map-upload")
    upload_step = next(
        s for s in action["runs"]["steps"] if s.get("id") == "upload"
    )
    body = upload_step["run"]
    # The script must normalize the directory before using it.
    assert 'src_dir="${{ inputs.source-map-dir }}"' in body
    assert 'src_dir="${src_dir%/}"' in body
    # And the normalized value must be what's stripped + searched.
    assert "${map_path#${src_dir}/}" in body
    assert 'find "${src_dir}"' in body


def test_qa_auto_passes_through_optional_id():
    """The script body must reference the `incident-id` input; otherwise
    the --id passthrough is broken."""
    action = _load_action("qa-auto")
    run_step = next(
        s for s in action["runs"]["steps"]
        if s.get("id") == "run" or "retrace qa auto" in (s.get("run") or "")
    )
    body = run_step["run"]
    assert "${{ inputs.incident-id }}" in body
    assert "--id" in body
