from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from retrace.auto_fix import propose_fix_for_incident
from retrace.auto_repro import reproduce_incident
from retrace.commands.api import _build_enricher
from retrace.config import load_config
from retrace.qa_incidents import Incident
from retrace.replay_core import process_queued_replay_jobs
from retrace.storage import Storage
from retrace.tester import (
    create_spec,
    list_specs,
    load_spec,
    run_spec,
    runs_dir_for_data_dir,
    specs_dir_for_data_dir,
)


def _server_info() -> dict[str, Any]:
    return {
        "name": "retrace-mcp",
        "version": "0.1.0",
    }


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "retrace.list_findings",
            "description": "List parsed findings from retrace.db",
            "inputSchema": {"type": "object", "properties": {"config": {"type": "string"}}},
        },
        {
            "name": "retrace.list_tester_specs",
            "description": "List saved UI tester specs",
            "inputSchema": {"type": "object", "properties": {"config": {"type": "string"}}},
        },
        {
            "name": "retrace.list_replay_sessions",
            "description": "List first-party replay sessions from retrace.db",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "config": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "retrace.list_replay_issues",
            "description": "List replay-backed issues from retrace.db",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "config": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "retrace.process_queued_replays",
            "description": "Process queued final replay batches into signals and issues",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "config": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "retrace.create_tester_spec",
            "description": "Create a tester spec (describe or explore_suite)",
            "inputSchema": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "config": {"type": "string"},
                    "name": {"type": "string"},
                    "mode": {"type": "string"},
                    "prompt": {"type": "string"},
                    "app_url": {"type": "string"},
                    "start_command": {"type": "string"},
                    "harness_command": {"type": "string"},
                    "auth_required": {"type": "boolean"},
                    "auth_mode": {"type": "string"},
                    "auth_login_url": {"type": "string"},
                    "auth_username": {"type": "string"},
                    "execution_engine": {"type": "string"},
                    "exact_steps": {"type": "array"},
                    "assertions": {"type": "array"},
                },
            },
        },
        {
            "name": "retrace.run_tester_spec",
            "description": "Run a tester spec and return flake-aware result",
            "inputSchema": {
                "type": "object",
                "required": ["spec_id"],
                "properties": {
                    "config": {"type": "string"},
                    "spec_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "app_url": {"type": "string"},
                    "start_command": {"type": "string"},
                    "retries": {"type": "integer"},
                },
            },
        },
        # ---- Unified QA Incident surface ----
        # These tools surface the same `qa_incidents` view that
        # `retrace qa list/show/reproduce/fix/auto` uses, so editor
        # agents (Cursor / Claude Desktop) can drive the killer demo
        # without shelling out.
        {
            "name": "retrace.list_qa_incidents",
            "description": (
                "List unified QA incidents (replay, UI test, API test, "
                "error monitor, PR review) in priority order."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "config": {"type": "string"},
                    "status": {"type": "string"},
                    "project_id": {"type": "string"},
                    "environment_id": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "retrace.get_qa_incident",
            "description": "Fetch a single QA incident by id or public id (INC-XXXXXX).",
            "inputSchema": {
                "type": "object",
                "required": ["incident_id"],
                "properties": {
                    "config": {"type": "string"},
                    "incident_id": {"type": "string"},
                },
            },
        },
        {
            "name": "retrace.reproduce_qa_incident",
            "description": (
                "Auto-generate a UI test that reproduces a QA incident, "
                "then run it via Browser Harness."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["incident_id"],
                "properties": {
                    "config": {"type": "string"},
                    "incident_id": {"type": "string"},
                    "app_url": {"type": "string"},
                    "harness_command": {"type": "string"},
                    "execution_engine": {"type": "string"},
                },
            },
        },
        {
            "name": "retrace.fix_qa_incident",
            "description": (
                "Build a fix prompt for a QA incident and open a draft PR "
                "(or just produce the prompt with --no-open-pr)."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["incident_id", "repo"],
                "properties": {
                    "config": {"type": "string"},
                    "incident_id": {"type": "string"},
                    "repo": {"type": "string"},
                    "repo_path": {"type": "string"},
                    "base_branch": {"type": "string"},
                    "open_pr": {"type": "boolean"},
                    "draft": {"type": "boolean"},
                    "apply_with_agent": {"type": "string"},
                },
            },
        },
    ]


def _cfg_path(args: dict[str, Any]) -> Path:
    return Path(str(args.get("config") or "config.yaml"))


def _handle_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    config_path = _cfg_path(args)
    cfg = load_config(config_path)
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()

    if name == "retrace.list_findings":
        rows = store.list_report_findings()
        return {
            "count": len(rows),
            "findings": [
                {
                    "id": r.id,
                    "finding_hash": r.finding_hash,
                    "title": r.title,
                    "severity": r.severity,
                    "category": r.category,
                    "regression_state": r.regression_state,
                    "regression_occurrence_count": r.regression_occurrence_count,
                }
                for r in rows
            ],
        }

    if name == "retrace.list_tester_specs":
        specs = list_specs(specs_dir_for_data_dir(cfg.run.data_dir))
        return {"count": len(specs), "specs": [s.__dict__ for s in specs]}

    if name == "retrace.list_replay_sessions":
        limit = max(1, min(int(args.get("limit", 50) or 50), 200))
        rows = store.list_recent_replay_sessions(limit=limit)
        return {
            "count": len(rows),
            "sessions": [
                {
                    "public_id": str(r["public_id"]),
                    "stable_id": str(r["stable_id"]),
                    "project_id": str(r["project_id"]),
                    "environment_id": str(r["environment_id"]),
                    "distinct_id": str(r["distinct_id"]),
                    "status": str(r["status"]),
                    "event_count": int(r["event_count"]),
                    "last_seen_at": str(r["last_seen_at"]),
                }
                for r in rows
            ],
        }

    if name == "retrace.list_replay_issues":
        limit = max(1, min(int(args.get("limit", 50) or 50), 200))
        rows = store.list_recent_replay_issues(limit=limit)
        return {
            "count": len(rows),
            "issues": [
                {
                    "public_id": str(r["public_id"]),
                    "status": str(r["status"]),
                    "priority": str(r["priority"]),
                    "severity": str(r["severity"]),
                    "title": str(r["title"]),
                    "affected_count": int(r["affected_count"]),
                    "updated_at": str(r["updated_at"]),
                }
                for r in rows
            ],
        }

    if name == "retrace.process_queued_replays":
        limit = max(1, min(int(args.get("limit", 25) or 25), 100))
        result = process_queued_replay_jobs(
            store=store, limit=limit, enricher=_build_enricher(cfg, store)
        )
        return {
            "jobs_seen": result.jobs_seen,
            "jobs_processed": result.jobs_processed,
            "jobs_failed": result.jobs_failed,
            "sessions_processed": result.sessions_processed,
            "issues_created_or_updated": result.issues_created_or_updated,
        }

    if name == "retrace.create_tester_spec":
        spec = create_spec(
            specs_dir=specs_dir_for_data_dir(cfg.run.data_dir),
            name=str(args.get("name", "")).strip() or "UI test",
            mode=str(args.get("mode", "describe")).strip() or "describe",
            prompt=str(args.get("prompt", "")).strip(),
            app_url=str(args.get("app_url", "")).strip()
            or "http://127.0.0.1:3000",
            start_command=str(args.get("start_command", "")).strip(),
            harness_command=str(args.get("harness_command", "")).strip(),
            auth_required=bool(args.get("auth_required", False)),
            auth_mode=str(args.get("auth_mode", "none")).strip(),
            auth_login_url=str(args.get("auth_login_url", "")).strip(),
            auth_username=str(args.get("auth_username", "")).strip(),
            execution_engine=str(args.get("execution_engine", "harness")).strip()
            or "harness",
            exact_steps=list(args.get("exact_steps") or []),
            assertions=list(args.get("assertions") or []),
        )
        return {"ok": True, "spec": spec.__dict__}

    if name == "retrace.run_tester_spec":
        spec_id = str(args.get("spec_id", "")).strip()
        if not spec_id:
            raise ValueError("spec_id is required")
        spec = load_spec(specs_dir_for_data_dir(cfg.run.data_dir), spec_id)
        result = run_spec(
            spec=spec,
            runs_dir=runs_dir_for_data_dir(cfg.run.data_dir),
            prompt_override=str(args.get("prompt", "")).strip() or None,
            app_url_override=str(args.get("app_url", "")).strip() or None,
            start_command_override=str(args.get("start_command", "")).strip() or None,
            max_retries=max(0, int(args.get("retries", 1) or 1)),
            cwd=config_path.parent,
        )
        return {"ok": result.ok, "result": result.__dict__}

    # ----- Unified QA Incident handlers -----

    if name == "retrace.list_qa_incidents":
        limit = max(1, min(int(args.get("limit", 25) or 25), 200))
        rows = store.list_qa_incidents(
            project_id=str(args.get("project_id", "") or "") or None,
            environment_id=str(args.get("environment_id", "") or "") or None,
            status=str(args.get("status", "") or "") or None,
            limit=limit,
        )
        return {
            "count": len(rows),
            "incidents": [_qa_incident_summary(Incident.from_row(r)) for r in rows],
        }

    if name == "retrace.get_qa_incident":
        incident_id = str(args.get("incident_id", "")).strip()
        if not incident_id:
            raise ValueError("incident_id is required")
        row = store.get_qa_incident(incident_id)
        if row is None:
            return {"found": False}
        return {"found": True, "incident": Incident.from_row(row).to_row()}

    if name == "retrace.reproduce_qa_incident":
        incident_id = str(args.get("incident_id", "")).strip()
        if not incident_id:
            raise ValueError("incident_id is required")
        outcome = reproduce_incident(
            store=store,
            data_dir=cfg.run.data_dir,
            incident_id=incident_id,
            app_url=str(args.get("app_url", "") or ""),
            harness_command=str(args.get("harness_command", "") or ""),
            execution_engine=str(args.get("execution_engine", "harness") or "harness").lower(),
        )
        return outcome.as_dict()

    if name == "retrace.fix_qa_incident":
        incident_id = str(args.get("incident_id", "")).strip()
        repo_full_name = str(args.get("repo", "")).strip()
        if not incident_id:
            raise ValueError("incident_id is required")
        if not repo_full_name:
            raise ValueError("repo is required (owner/name)")
        repo = store.get_github_repo(repo_full_name)
        if repo is None:
            raise ValueError(
                f"repo not connected: {repo_full_name}. "
                f"Run `retrace github connect --repo {repo_full_name}` first."
            )
        repo_path_arg = str(args.get("repo_path", "") or "").strip()
        effective_repo_path = (
            Path(repo_path_arg) if repo_path_arg
            else (Path(repo.local_path) if repo.local_path else None)
        )
        if effective_repo_path is None:
            raise ValueError(
                "No local repo path. Pass `repo_path` or connect the repo "
                "with --local-path."
            )
        outcome = propose_fix_for_incident(
            store=store,
            incident_id=incident_id,
            repo_full_name=repo_full_name,
            repo_path=effective_repo_path,
            base_branch=str(args.get("base_branch", "") or repo.default_branch or "main"),
            open_pr=bool(args.get("open_pr", True)),
            draft=bool(args.get("draft", True)),
            apply_with_agent=str(args.get("apply_with_agent", "") or ""),
        )
        return outcome.as_dict()

    raise ValueError(f"Unknown tool: {name}")


def _qa_incident_summary(inc: Incident) -> dict[str, Any]:
    """Compact summary an editor agent can scan quickly."""
    return {
        "public_id": inc.public_id,
        "title": inc.title,
        "severity": inc.severity,
        "status": inc.status,
        "primary_source_kind": inc.primary_source_kind,
        "affected_users": inc.affected_users,
        "affected_count": inc.affected_count,
        "repro_status": inc.repro_status,
        "fix_status": inc.fix_status,
        "fix_pr_url": inc.fix_pr_url,
        "updated_at": inc.updated_at,
    }


def _send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _serve_stdio() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}
            if method == "initialize":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": _server_info(),
                        },
                    }
                )
                continue
            if method == "tools/list":
                _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": _tools()}})
                continue
            if method == "tools/call":
                name = str(params.get("name") or "")
                arguments = params.get("arguments") or {}
                result = _handle_tool_call(name, arguments)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps(result, indent=2)}
                            ],
                            "isError": False,
                        },
                    }
                )
                continue
            if method == "ping":
                _send({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
                continue
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
        except Exception as exc:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            )


@click.group("mcp")
def mcp_group() -> None:
    """Run Retrace MCP server (single server with multiple tools)."""


@mcp_group.command("serve")
def mcp_serve() -> None:
    """Serve MCP-compatible JSON-RPC on stdio."""
    _serve_stdio()
