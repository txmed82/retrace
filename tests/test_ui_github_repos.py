from __future__ import annotations

from pathlib import Path

from retrace.commands.ui import _connect_github_repo_payload, _github_repos_payload
from retrace.storage import Storage


def test_connect_github_repo_payload_stores_repo_with_local_path(
    tmp_path: Path,
) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    repo_dir = tmp_path / "checkout"
    repo_dir.mkdir()

    payload, status = _connect_github_repo_payload(
        store=store,
        repo_full_name="acme/widgets",
        default_branch="main",
        local_path=str(repo_dir),
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["repos"][0]["repo_full_name"] == "acme/widgets"
    assert payload["repos"][0]["local_path"] == str(repo_dir)

    listed = _github_repos_payload(store)
    assert listed["repos"][0]["default_branch"] == "main"


def test_connect_github_repo_payload_rejects_bad_repo_name(tmp_path: Path) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    payload, status = _connect_github_repo_payload(
        store=store,
        repo_full_name="widgets",
        default_branch="main",
        local_path="",
    )

    assert status == 400
    assert payload == {"ok": False, "error": "Repo must use owner/name format."}

    payload, status = _connect_github_repo_payload(
        store=store,
        repo_full_name="owner/name/extra",
        default_branch="main",
        local_path="",
    )

    assert status == 400
    assert payload == {"ok": False, "error": "Repo must use owner/name format."}

    payload, status = _connect_github_repo_payload(
        store=store,
        repo_full_name="owner//name",
        default_branch="main",
        local_path="",
    )

    assert status == 400
    assert payload == {"ok": False, "error": "Repo must use owner/name format."}


def test_connect_github_repo_payload_rejects_missing_local_path(
    tmp_path: Path,
) -> None:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    missing = tmp_path / "missing"

    payload, status = _connect_github_repo_payload(
        store=store,
        repo_full_name="acme/widgets",
        default_branch="main",
        local_path=str(missing),
    )

    assert status == 400
    assert payload["ok"] is False
    assert str(missing) in payload["error"]
