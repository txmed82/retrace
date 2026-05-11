"""Worktree-based fix-PR step.

The fix step must never disturb the user's main checkout, even on failure
paths. These tests stage a real local git repo, run propose_fix_for_incident
with `gh` disabled, and assert that:
  - the user's branch and working tree are untouched
  - a deterministic Retrace branch is created and committed to
  - the prompt is checked in under `.retrace/`
  - the worktree directory is cleaned up after the run
"""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path

import pytest

from retrace.auto_fix import _worktree_dir_for, propose_fix_for_incident
from retrace.qa_incidents import (
    Incident,
    IncidentEvidence,
    ReproductionStep,
    make_fingerprint,
    make_public_id,
    utc_now_iso,
)
from retrace.storage import Storage


def _git(args: list[str], cwd: Path, env: dict | None = None) -> str:
    full_env = os.environ.copy()
    # Avoid colliding with the user's git config in CI.
    full_env.setdefault("GIT_AUTHOR_NAME", "Retrace Test")
    full_env.setdefault("GIT_AUTHOR_EMAIL", "test@retrace.dev")
    full_env.setdefault("GIT_COMMITTER_NAME", "Retrace Test")
    full_env.setdefault("GIT_COMMITTER_EMAIL", "test@retrace.dev")
    if env:
        full_env.update(env)
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{r.stderr}\n{r.stdout}")
    return r.stdout


def _make_repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare 'origin' remote + a working clone pointing at it.

    Returns (working_repo, bare_origin).
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(["init", "--bare", "--initial-branch=main"], cwd=origin)

    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "--initial-branch=main"], cwd=work)
    _git(["remote", "add", "origin", str(origin)], cwd=work)
    (work / "README.md").write_text("hello\n")
    (work / "src.py").write_text("def login():\n    pass\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-m", "init"], cwd=work)
    _git(["push", "-u", "origin", "main"], cwd=work)
    return work, origin


def _make_incident(store: Storage) -> Incident:
    inc = Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id="local",
        environment_id="production",
        fingerprint=make_fingerprint(["worktree-test"]),
        title="Login fails on submit",
        summary="500 on /api/login under load",
        suspected_cause="POST /api/login swallows AbortError",
        severity="high",
        confidence="high",
        status="reproduced",
        primary_source_kind="replay",
        sources=[],
        reproduction=[ReproductionStep(0, "click", "submit")],
        expected_outcome="dashboard",
        actual_outcome="500",
        app_url="http://localhost:3000",
        evidence=IncidentEvidence(),
        affected_count=1,
        affected_users=1,
        first_seen_ms=0,
        last_seen_ms=0,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    store.upsert_qa_incident(inc.to_row())
    return inc


@pytest.fixture()
def repo_and_store(tmp_path: Path, monkeypatch):
    work, origin = _make_repo_pair(tmp_path)
    db = Storage(tmp_path / "retrace.db")
    db.init_schema()
    # Don't let the test pick up a real `gh` binary that might try to talk
    # to GitHub.com — force the missing-gh path.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # CI runners often have no global `git config user.name/email`. Export
    # the git identity env vars so every subprocess spawned during the
    # test (including the ones inside `auto_fix.propose_fix_for_incident`)
    # can commit without falling over on "empty ident name".
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Retrace Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@retrace.dev")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Retrace Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@retrace.dev")
    return work, origin, db


def test_propose_fix_leaves_user_checkout_untouched(repo_and_store):
    work, origin, store = repo_and_store
    inc = _make_incident(store)

    # The user has uncommitted edits in their working tree.
    (work / "in-flight.txt").write_text("user is mid-edit\n")
    assert _git(["status", "--porcelain"], cwd=work).strip() != ""

    outcome = propose_fix_for_incident(
        store=store,
        incident_id=inc.public_id,
        repo_full_name="local/test",
        repo_path=work,
        base_branch="main",
        prompts_out_dir=work.parent / "prompts",
        open_pr=True,
        draft=True,
    )

    # The user's branch and edits survive.
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=work).strip() == "main"
    assert (work / "in-flight.txt").exists()
    assert "in-flight.txt" in _git(["status", "--porcelain"], cwd=work)

    # The retrace branch was created and committed to (visible via reflog/log).
    branches = _git(["branch", "--list", "retrace/*"], cwd=work)
    assert outcome.branch and outcome.branch in branches

    # Worktree dir is cleaned up.
    wt = _worktree_dir_for(work, outcome.branch)
    assert not wt.exists(), f"worktree dir {wt} still exists after run"

    # The branch tip carries a `.retrace/<PUB>.fix.md` file.
    show = _git(
        ["show", "--stat", "--name-only", outcome.branch],
        cwd=work,
    )
    assert f".retrace/{inc.public_id}.fix.md" in show

    # No PR url because gh wasn't available, but the prompt is on disk too.
    assert outcome.pr_url == ""
    assert Path(outcome.prompt_path).exists()


def test_propose_fix_is_idempotent_on_repeat_runs(repo_and_store):
    work, origin, store = repo_and_store
    inc = _make_incident(store)

    first = propose_fix_for_incident(
        store=store,
        incident_id=inc.public_id,
        repo_full_name="local/test",
        repo_path=work,
        base_branch="main",
        prompts_out_dir=work.parent / "prompts",
        open_pr=True,
        draft=True,
    )

    # Second invocation must not blow up on "branch already exists".
    second = propose_fix_for_incident(
        store=store,
        incident_id=inc.public_id,
        repo_full_name="local/test",
        repo_path=work,
        base_branch="main",
        prompts_out_dir=work.parent / "prompts",
        open_pr=True,
        draft=True,
    )
    assert first.branch == second.branch
    assert second.status in {"pr_open", "applied", "prompt_ready"}
    assert not _worktree_dir_for(work, second.branch).exists()
