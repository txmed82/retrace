from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from retrace.config import load_config
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

    raise ValueError(f"Unknown tool: {name}")


def _send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _serve_stdio() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
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
                    "id": None,
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
