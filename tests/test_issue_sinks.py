from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from pytest_httpx import HTTPXMock

from retrace.cli import main
from retrace.issue_sink_clients import GitHubClient, IssueSinkError, LinearClient
from retrace.issue_sinks import promote_replay_issue, render_issue_markdown
from retrace.replay_core import ReplaySignalConfig, process_replay_sessions
from retrace.storage import Storage


def _navigation(url: str, ts: int = 0) -> dict[str, object]:
    return {"type": 4, "timestamp": ts, "data": {"href": url}}


def _console_error(message: str, ts: int = 1000) -> dict[str, object]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "retrace/console@1",
            "payload": {"level": "error", "payload": [message]},
        },
    }


def _seed_issue(tmp_path: Path) -> tuple[Storage, Any, str]:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(
        org_name="Acme", project_name="Web", environment_name="production"
    )
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
    return store, workspace, issue_public_id


def _mock_transport(handler):
    return httpx.MockTransport(handler)


# ---------- LinearClient ----------


def test_linear_client_create_issue_uses_graphql() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "uuid-1",
                            "identifier": "ENG-42",
                            "url": "https://linear.app/acme/issue/ENG-42",
                            "title": "Test",
                        },
                    }
                }
            },
        )

    with httpx.Client(transport=_mock_transport(handler)) as raw:
        client = LinearClient(api_key="lin_api_test", client=raw)
        result = client.create_issue(
            team_id="team-uuid",
            title="Test",
            description="body",
            labels=None,
        )

    assert result.external_id == "ENG-42"
    assert result.external_url == "https://linear.app/acme/issue/ENG-42"
    assert captured["headers"]["authorization"] == "lin_api_test"
    assert captured["json"]["variables"]["input"]["teamId"] == "team-uuid"


def test_linear_client_raises_on_graphql_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "boom"}]})

    with httpx.Client(transport=_mock_transport(handler)) as raw:
        client = LinearClient(api_key="lin_api_test", client=raw)
        with pytest.raises(IssueSinkError):
            client.create_issue(team_id="team", title="t", description="d")


def test_linear_client_resolves_team_id_by_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if "TeamByKey" in body["query"]:
            return httpx.Response(
                200,
                json={"data": {"teams": {"nodes": [{"id": "team-uuid", "key": "ENG"}]}}},
            )
        raise AssertionError("unexpected query")

    with httpx.Client(transport=_mock_transport(handler)) as raw:
        client = LinearClient(api_key="lin_api_test", client=raw)
        assert client.resolve_team_id("ENG") == "team-uuid"


# ---------- GitHubClient ----------


def test_github_client_create_issue_uses_rest() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            201,
            json={
                "number": 7,
                "html_url": "https://github.com/acme/web/issues/7",
                "state": "open",
            },
        )

    with httpx.Client(transport=_mock_transport(handler)) as raw:
        client = GitHubClient(api_key="ghp_test", client=raw)
        result = client.create_issue(
            repo="acme/web",
            title="Test",
            body="body",
            labels=["retrace"],
        )

    assert result.external_id == "acme/web#7"
    assert result.external_url == "https://github.com/acme/web/issues/7"
    assert captured["headers"]["authorization"] == "Bearer ghp_test"
    assert captured["headers"]["x-github-api-version"] == "2022-11-28"
    assert captured["url"].endswith("/repos/acme/web/issues")
    assert captured["json"]["labels"] == ["retrace"]


def test_github_client_rejects_bad_repo_format() -> None:
    with httpx.Client() as raw:
        client = GitHubClient(api_key="x", client=raw)
        with pytest.raises(ValueError):
            client.create_issue(repo="not-a-slug", title="t", body="b")


def test_github_client_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"message":"Bad credentials"}')

    with httpx.Client(transport=_mock_transport(handler)) as raw:
        client = GitHubClient(api_key="bad", client=raw)
        with pytest.raises(IssueSinkError):
            client.create_issue(repo="acme/web", title="t", body="b")


# ---------- promote_replay_issue end-to-end ----------


def test_promote_replay_issue_via_real_linear_client(tmp_path: Path) -> None:
    store, workspace, issue_public_id = _seed_issue(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        query = body["query"]
        if "TeamLabels" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "team": {
                            "labels": {
                                "nodes": [{"id": "label-1", "name": "retrace"}]
                            }
                        }
                    }
                },
            )
        assert "IssueCreate" in query
        assert body["variables"]["input"]["labelIds"] == ["label-1"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "uuid-1",
                            "identifier": "ENG-99",
                            "url": "https://linear.app/acme/issue/ENG-99",
                            "title": "x",
                        },
                    }
                }
            },
        )

    with httpx.Client(transport=_mock_transport(handler)) as raw:
        client = LinearClient(api_key="lin_api_test", client=raw)
        result = promote_replay_issue(
            store=store,
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            issue_id=issue_public_id,
            provider="linear",
            base_url="https://retrace.example",
            linear_client=client,
            linear_team_id="team-uuid",
            labels=["retrace"],
        )

    assert result.created is True
    assert result.external_id == "ENG-99"
    assert result.external_url == "https://linear.app/acme/issue/ENG-99"


def test_promote_replay_issue_via_real_github_client(tmp_path: Path) -> None:
    store, workspace, issue_public_id = _seed_issue(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "number": 12,
                "html_url": "https://github.com/acme/web/issues/12",
            },
        )

    with httpx.Client(transport=_mock_transport(handler)) as raw:
        client = GitHubClient(api_key="ghp_test", client=raw)
        result = promote_replay_issue(
            store=store,
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            issue_id=issue_public_id,
            provider="github",
            base_url="https://retrace.example",
            github_client=client,
            github_repo="acme/web",
        )

    assert result.created is True
    assert result.external_id == "acme/web#12"
    assert result.external_url == "https://github.com/acme/web/issues/12"


def test_promote_replay_issue_falls_back_to_stub_without_client(tmp_path: Path) -> None:
    store, workspace, issue_public_id = _seed_issue(tmp_path)
    result = promote_replay_issue(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=issue_public_id,
        provider="github",
        base_url="https://retrace.example",
    )
    assert result.created is True
    assert result.external_id.startswith("GH-bug_")
    assert result.external_url.startswith("github://issue/")


def test_promote_replay_issue_requires_team_for_real_linear(tmp_path: Path) -> None:
    store, workspace, issue_public_id = _seed_issue(tmp_path)
    with httpx.Client() as raw:
        client = LinearClient(api_key="lin_api_test", client=raw)
        with pytest.raises(IssueSinkError):
            promote_replay_issue(
                store=store,
                project_id=workspace.project_id,
                environment_id=workspace.environment_id,
                issue_id=issue_public_id,
                provider="linear",
                linear_client=client,
                linear_team_id="",
            )


# ---------- markdown rendering ----------


def test_render_issue_markdown_contains_key_fields() -> None:
    body = render_issue_markdown(
        {
            "summary": "Page crashes",
            "severity": "high",
            "affected_count": 3,
            "affected_users": 2,
            "likely_cause": "missing total",
            "reproduction_steps": ["Open page", "Click pay"],
            "replay_links": [
                {"role": "representative", "session_id": "abc", "url": "https://r/abc"}
            ],
            "source_public_id": "bug_xyz",
        }
    )
    assert "Page crashes" in body
    assert "**Severity:** high" in body
    assert "1. Open page" in body
    assert "https://r/abc" in body
    assert "bug_xyz" in body


def test_build_issue_sink_payload_skips_correlation_when_row_partial() -> None:
    """Legacy/partially-migrated rows missing some correlation columns must
    not crash issue promotion - the correlation block is all-or-nothing."""
    from retrace.issue_sinks import build_issue_sink_payload

    # Row dict with only one of the six correlation columns present.
    partial = {
        "id": "ri_1",
        "public_id": "bug_1",
        "title": "t",
        "severity": "high",
        "status": "new",
        "summary": "s",
        "likely_cause": "",
        "affected_count": 1,
        "affected_users": 1,
        "reproduction_steps_json": "[]",
        "evidence_json": "{}",
        "trace_ids_json": '["trace-only"]',
        # error_issue_ids_json / error_tracking_url / logs_url /
        # top_stack_frame / distinct_id intentionally absent.
    }
    payload = build_issue_sink_payload(
        issue=partial, sessions=[], provider="linear"
    )
    assert "correlation" not in payload


def test_render_issue_markdown_includes_correlation_block() -> None:
    body = render_issue_markdown(
        {
            "summary": "Checkout breaks",
            "severity": "high",
            "affected_count": 1,
            "affected_users": 1,
            "source_public_id": "bug_corr",
            "replay_links": [],
            "correlation": {
                "distinct_id": "user-9",
                "trace_ids": ["trace-abc"],
                "error_issue_ids": ["err-1"],
                "top_stack_frame": "renderCheckout (checkout.tsx:42)",
                "error_tracking_url": "https://posthog/example/error/err-1",
                "logs_url": "https://posthog/example/logs?trace=trace-abc",
            },
        }
    )
    assert "### Backend correlation" in body
    assert "`user-9`" in body  # distinct_id is one of the most useful lookup keys
    assert "`trace-abc`" in body
    assert "`err-1`" in body
    assert "(https://posthog/example/error/err-1)" in body
    assert "(https://posthog/example/logs?trace=trace-abc)" in body
    assert "renderCheckout (checkout.tsx:42)" in body


# ---------- CLI promote-issue ----------


_PROMOTE_CONFIG_YAML = """posthog:
  host: https://us.i.posthog.com
  project_id: "42"
  api_key: phx_test
llm:
  base_url: http://localhost:8080/v1
  model: llama
run:
  data_dir: ./data
linear:
  api_key: lin_api_test
"""


def _seed_issue_in(tmp_path: Path) -> str:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    store = Storage(data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-cli",
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
        session_ids=["sess-cli"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    return processed.issues[0].public_id


_SYNC_CONFIG_YAML = """posthog:
  host: https://us.i.posthog.com
  project_id: "42"
  api_key: phx_test
llm:
  base_url: http://localhost:8080/v1
  model: llama
run:
  data_dir: ./data
linear:
  api_key: lin_api_test
github_sink:
  api_key: ghp_test
"""


def _seed_ticketed_issue(tmp_path: Path, *, ticket_id: str) -> str:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    store = Storage(data_dir / "retrace.db")
    store.init_schema()
    workspace = store.ensure_workspace(project_name="Default")
    store.insert_replay_batch(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_id="sess-tx",
        sequence=0,
        events=[
            _navigation("https://app.example/checkout"),
            _console_error("TypeError: total is undefined"),
        ],
        flush_type="incremental",
    )
    processed = process_replay_sessions(
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        session_ids=["sess-tx"],
        config=ReplaySignalConfig.from_names(["console_error"]),
    )
    issue_row = store.get_replay_issue(
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
        issue_id=processed.issues[0].public_id,
    )
    assert issue_row is not None
    store.mark_replay_issue_ticket_created(
        str(issue_row["id"]),
        external_ticket_id=ticket_id,
        external_ticket_url=f"https://upstream/{ticket_id}",
    )
    return str(issue_row["public_id"])


def test_cli_sync_tickets_marks_resolved_when_github_closed(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "config.yaml").write_text(_SYNC_CONFIG_YAML)
    (tmp_path / ".env").write_text("")
    public_id = _seed_ticketed_issue(tmp_path, ticket_id="acme/web#7")

    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/repos/acme/web/issues/7",
        json={"state": "closed", "state_reason": "completed", "html_url": "x"},
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["api", "sync-tickets"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert public_id in out["resolved"]
    assert out["dry_run"] is False


def test_cli_sync_tickets_dry_run_does_not_transition(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "config.yaml").write_text(_SYNC_CONFIG_YAML)
    (tmp_path / ".env").write_text("")
    public_id = _seed_ticketed_issue(tmp_path, ticket_id="acme/web#9")

    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/repos/acme/web/issues/9",
        json={"state": "closed", "state_reason": "completed", "html_url": "x"},
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["api", "sync-tickets", "--dry-run"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["resolved"] == []
    assert any(p["public_id"] == public_id for p in out["plan"])


def test_cli_sync_tickets_skips_unknown_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "config.yaml").write_text(_SYNC_CONFIG_YAML)
    (tmp_path / ".env").write_text("")
    public_id = _seed_ticketed_issue(tmp_path, ticket_id="weirdformat")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["api", "sync-tickets"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["resolved"] == []
    assert any(
        s["public_id"] == public_id and "unknown_provider" in s["reason"]
        for s in out["skipped"]
    )


def test_cli_promote_issue_unknown_team_key_returns_friendly_error(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "config.yaml").write_text(_PROMOTE_CONFIG_YAML)
    (tmp_path / ".env").write_text("")
    issue_public_id = _seed_issue_in(tmp_path)

    # Linear returns no teams matching the bogus key — resolve_team_id raises IssueSinkError.
    httpx_mock.add_response(
        method="POST",
        url="https://api.linear.app/graphql",
        json={"data": {"teams": {"nodes": []}}},
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "api",
            "promote-issue",
            "--provider",
            "linear",
            "--team-key",
            "BOGUS",
            issue_public_id,
        ],
    )

    assert result.exit_code != 0
    output = result.output + (str(result.exception) if result.exception else "")
    assert "Linear team not found" in output
    # The friendly ClickException output should not contain a Python traceback.
    assert "Traceback" not in result.output
