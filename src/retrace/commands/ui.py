"""Thin CLI wrapper for the Retrace local UI server.

The payload-building helpers live in ui_payloads.py and the HTML template
in ui_templates.py. This module re-exports everything for backward
compatibility and defines the ``ui`` click command.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import click

from retrace.api_testing import run_api_spec  # noqa: F401
from retrace.ingester import PostHogIngester
from retrace.llm.client import LLMClient
from retrace.sdk_keys import create_sdk_key  # noqa: F401
from retrace.storage import Storage
from retrace.tester import (
    DEFAULT_APP_URL,
    DEFAULT_HARNESS_COMMAND,
    create_spec,
    list_specs,
    load_run_summaries,
    load_spec,
    run_spec,
    runs_dir_for_data_dir,
    specs_dir_for_data_dir,
)

from retrace.commands.ui_payloads import (  # noqa: F401 — backward-compat re-exports
    _CLOUD_LLM_PROVIDERS,
    _LLM_PROVIDER_DEFAULTS,
    _api_specs_payload,
    _api_suites_payload,
    _connect_github_repo_payload,
    _create_pinned_transport,
    _create_sdk_key_payload,
    _default_config,
    _edit_ui_draft_payload,
    _failure_test_link_payload,
    _generate_replay_issue_api_spec_payload,
    _generate_replay_issue_fix_prompts_payload,
    _generate_replay_issue_spec_payload,
    _generate_replay_issue_specs_payload,
    _gh_checks,
    _github_repos_payload,
    _hosted_onboarding_readiness_payload,
    _issue_evidence_stitching_payload,
    _issue_has_replay_regression_link,
    _issue_workflow_payload,
    _json_field,
    _json_object_list_payload,
    _latest_report,
    _llm_check,
    _llm_defaults,
    _llm_models,
    _posthog_check,
    _read_config,
    _read_env,
    _replay_api_check,
    _replay_api_calls,
    _replay_evidence_timeline,
    _replay_issue_payload,
    _repair_task_payload,
    _resolve_llm_api_key,
    _run_api_spec_payload,
    _run_api_suite_payload,
    _run_replay_issue_api_spec_payload,
    _select_repo,
    _session_id_from_url,
    _to_findings_payload,
    _to_replay_dashboard_payload,
    _transition_replay_issue_payload,
    _truthy_env,
    _validate_base_url,
    _verify_resolved_issues_payload,
    _write_config,
    _write_env,
)

from retrace.commands.ui_templates import _INDEX_HTML  # noqa: F401

logger = logging.getLogger(__name__)


@click.command("ui")
@click.option(
    "--config-path",
    default=Path("config.yaml"),
    type=click.Path(path_type=Path),
    help="Path to config.yaml",
)
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8787, type=int, help="Port to listen on")
@click.option(
    "--repo-full-name",
    default=None,
    help="GitHub repo (owner/name) for code matching and fix prompts",
)
def ui_command(
    config_path: Path, host: str, port: int, repo_full_name: Optional[str]
) -> None:
    """Run local browser UI for onboarding + findings + rrweb replay."""
    env_path = config_path.parent / ".env"

    cfg_dict = _read_config(config_path)
    data_dir = Path(str(((cfg_dict.get("run") or {}).get("data_dir") or "./data")))
    output_dir = Path(
        str(((cfg_dict.get("run") or {}).get("output_dir") or "./reports"))
    )

    store = Storage(data_dir / "retrace.db")
    store.init_schema()

    def current_settings(*, include_secrets: bool = False) -> dict[str, Any]:
        cfg = _read_config(config_path)
        env = _read_env(env_path)
        llm_provider = str(
            ((cfg.get("llm") or {}).get("provider") or "openai_compatible")
        )
        effective_llm_key = _resolve_llm_api_key(llm_provider, env)
        settings: dict[str, Any] = {
            "posthog_host": str(
                ((cfg.get("posthog") or {}).get("host") or "https://us.i.posthog.com")
            ),
            "posthog_project_id": str(
                ((cfg.get("posthog") or {}).get("project_id") or "")
            ),
            "posthog_api_key_present": bool(
                env.get("RETRACE_POSTHOG_API_KEY", "").strip()
            ),
            "llm_provider": llm_provider,
            "llm_base_url": str(
                ((cfg.get("llm") or {}).get("base_url") or "http://localhost:8080/v1")
            ),
            "llm_model": str(
                ((cfg.get("llm") or {}).get("model") or "llama-3.1-8b-instruct")
            ),
            "llm_api_key_present": bool(effective_llm_key),
            "tester_app_url": str(
                ((cfg.get("tester") or {}).get("app_url") or DEFAULT_APP_URL)
            ),
            "tester_start_command": str(
                ((cfg.get("tester") or {}).get("start_command") or "")
            ),
            "tester_harness_command": str(
                (
                    (cfg.get("tester") or {}).get("harness_command")
                    or DEFAULT_HARNESS_COMMAND
                )
            ),
            "tester_max_retries": int(
                (cfg.get("tester") or {}).get("max_retries") or 1
            ),
            "tester_auth_required": bool(
                (cfg.get("tester") or {}).get("auth_required") or False
            ),
            "tester_auth_mode": str(
                ((cfg.get("tester") or {}).get("auth_mode") or "none")
            ),
            "tester_auth_login_url": str(
                ((cfg.get("tester") or {}).get("auth_login_url") or "")
            ),
            "tester_auth_username": str(
                ((cfg.get("tester") or {}).get("auth_username") or "")
            ),
            "tester_auth_password_present": bool(
                env.get("RETRACE_TESTER_AUTH_PASSWORD", "").strip()
            ),
            "tester_auth_jwt_present": bool(
                env.get("RETRACE_TESTER_AUTH_JWT", "").strip()
            ),
            "tester_auth_headers_present": bool(
                env.get("RETRACE_TESTER_AUTH_HEADERS", "").strip()
            ),
        }
        if include_secrets:
            settings["posthog_api_key"] = env.get("RETRACE_POSTHOG_API_KEY", "")
            settings["llm_api_key"] = effective_llm_key
        return settings

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str, status: int = 200) -> None:
            b = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _read_json_body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(n) if n > 0 else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self._html(_INDEX_HTML)
                return

            if path == "/api/settings":
                self._json(current_settings())
                return

            if path == "/api/github/repos":
                self._json(_github_repos_payload(store))
                return

            if path == "/api/system-checks":
                s = current_settings(include_secrets=True)
                self._json(
                    {
                        "gh": _gh_checks(),
                        "posthog": _posthog_check(
                            s["posthog_host"],
                            s["posthog_project_id"],
                            s["posthog_api_key"],
                        ),
                        "llm": _llm_check(
                            s["llm_provider"],
                            s["llm_base_url"],
                            s["llm_model"],
                            s["llm_api_key"],
                        ),
                        "replay_api": _replay_api_check(),
                    }
                )
                return

            if path == "/api/onboarding/readiness":
                s = current_settings(include_secrets=True)
                checks = {
                    "gh": _gh_checks(),
                    "posthog": _posthog_check(
                        s["posthog_host"],
                        s["posthog_project_id"],
                        s["posthog_api_key"],
                    ),
                    "llm": _llm_check(
                        s["llm_provider"],
                        s["llm_base_url"],
                        s["llm_model"],
                        s["llm_api_key"],
                    ),
                    "replay_api": _replay_api_check(),
                }
                self._json(
                    _hosted_onboarding_readiness_payload(
                        store=store,
                        data_dir=data_dir,
                        settings=s,
                        checks=checks,
                    )
                )
                return

            if path == "/api/findings":
                rp = _latest_report(output_dir)
                findings = _to_findings_payload(
                    store=store,
                    report_path=rp,
                    repo_full_name=repo_full_name,
                )
                self._json({"report_path": str(rp) if rp else "", "findings": findings})
                return

            if path == "/api/qa-incidents":
                # Unified QA incident queue (replay/UI/API/monitor/review).
                query_args = parse_qs(urlparse(self.path).query)
                source_filter = (query_args.get("source") or [""])[0]
                # Page through the unified queue and filter on the way so a
                # filtered source whose matching rows fall outside the first
                # 200 still surfaces.
                rows: list = []
                page_size = 200
                offset = 0
                while len(rows) < 200 and offset < 5000:
                    page = store.list_qa_incidents(limit=page_size, offset=offset)
                    if not page:
                        break
                    if source_filter:
                        rows.extend(
                            r for r in page if str(r["primary_source_kind"]) == source_filter
                        )
                    else:
                        rows.extend(page)
                    if len(page) < page_size:
                        break
                    offset += page_size
                rows = rows[:200]
                incidents = [
                    {
                        "public_id": str(r["public_id"]),
                        "title": str(r["title"] or ""),
                        "summary": str(r["summary"] or ""),
                        "severity": str(r["severity"] or ""),
                        "confidence": str(r["confidence"] or ""),
                        "status": str(r["status"] or ""),
                        "primary_source_kind": str(r["primary_source_kind"] or ""),
                        "affected_users": int(r["affected_users"] or 0),
                        "affected_count": int(r["affected_count"] or 0),
                        "fix_pr_url": str(r["fix_pr_url"] or ""),
                        "updated_at": str(r["updated_at"] or ""),
                    }
                    for r in rows
                ]
                self._json({"incidents": incidents, "count": len(incidents)})
                return

            if path.startswith("/api/qa-incidents/"):
                public_id = path[len("/api/qa-incidents/"):]
                if not re.match(r"^INC-[A-Z0-9]+$", public_id):
                    self._json({"error": "invalid incident id"}, status=400)
                    return
                row = store.get_qa_incident(public_id)
                if row is None:
                    self._json({"error": "not found"}, status=404)
                    return
                # Project the row to a plain dict the frontend can consume.
                incident = {
                    key: (str(row[key]) if row[key] is not None else "")
                    for key in row.keys()
                }
                # Cast known numeric columns.
                for k in ("affected_count", "affected_users", "first_seen_ms", "last_seen_ms"):
                    if k in incident:
                        try:
                            incident[k] = int(incident[k] or 0)
                        except (TypeError, ValueError):
                            incident[k] = 0
                self._json({"incident": incident})
                return

            if path == "/api/tester/specs":
                specs = [
                    s.__dict__ for s in list_specs(specs_dir_for_data_dir(data_dir))
                ]
                self._json({"specs": specs})
                return

            if path == "/api/tester/runs":
                runs = load_run_summaries(runs_dir_for_data_dir(data_dir), limit=20)
                self._json({"runs": runs})
                return

            if path == "/api/api-suites":
                self._json(_api_suites_payload(data_dir))
                return

            if path == "/api/api-specs":
                self._json(_api_specs_payload(data_dir))
                return

            if path == "/api/replay-dashboard":
                self._json(_to_replay_dashboard_payload(store))
                return

            if path.startswith("/api/replay-session/") and path.endswith("/events"):
                session_id = path.split("/")[3]
                if not re.match(r"^[A-Za-z0-9._-]+$", session_id):
                    self._json({"error": "invalid session_id"}, status=400)
                    return
                match = None
                for session in store.list_recent_replay_sessions(limit=500):
                    if str(session["stable_id"]) == session_id:
                        match = session
                        break
                if match is None:
                    self._json({"error": "not found", "events": []}, status=404)
                    return
                playback = store.get_replay_playback(
                    project_id=str(match["project_id"]),
                    environment_id=str(match["environment_id"]),
                    session_id=session_id,
                )
                self._json({"session_id": session_id, "events": playback.events if playback else []})
                return

            if path.startswith("/api/session/") and path.endswith("/events"):
                session_id = path.split("/")[3]
                # Validate session_id to prevent path traversal
                if not re.match(r"^[A-Za-z0-9._-]+$", session_id):
                    self._json({"error": "invalid session_id"}, status=400)
                    return
                sp = data_dir / "sessions" / f"{session_id}.json"
                # Ensure the resolved path is inside sessions directory
                try:
                    resolved = sp.resolve()
                    sessions_dir = (data_dir / "sessions").resolve()
                    if (
                        sessions_dir not in resolved.parents
                        and resolved != sessions_dir
                    ):
                        self._json({"error": "invalid session path"}, status=400)
                        return
                except Exception:
                    self._json({"error": "invalid session path"}, status=400)
                    return
                if not sp.exists():
                    self._json({"error": "session not found"}, status=404)
                    return
                try:
                    events = json.loads(sp.read_text())
                except Exception:
                    self._json({"error": "invalid session payload"}, status=500)
                    return
                self._json({"session_id": session_id, "events": events})
                return

            self._json({"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/settings":
                body = self._read_json_body()
                host_v = (
                    str(body.get("posthog_host", "")).strip()
                    or "https://us.i.posthog.com"
                )
                project_v = str(body.get("posthog_project_id", "")).strip()
                key_v = str(body.get("posthog_api_key", "")).strip()
                llm_provider_v = (
                    str(body.get("llm_provider", "")).strip() or "openai_compatible"
                )
                defaults = _llm_defaults(llm_provider_v)
                llm_base_url_v = (
                    str(body.get("llm_base_url", "")).strip() or defaults["base_url"]
                )
                llm_model_v = (
                    str(body.get("llm_model", "")).strip() or defaults["model"]
                )
                llm_key_v = str(body.get("llm_api_key", "")).strip()
                tester_app_url_v = (
                    str(body.get("tester_app_url", "")).strip() or DEFAULT_APP_URL
                )
                tester_start_command_v = str(
                    body.get("tester_start_command", "")
                ).strip()
                tester_harness_command_v = (
                    str(body.get("tester_harness_command", "")).strip()
                    or DEFAULT_HARNESS_COMMAND
                )
                try:
                    tester_max_retries_v = max(
                        0, int(str(body.get("tester_max_retries", "1")).strip() or "1")
                    )
                except Exception:
                    tester_max_retries_v = 1
                tester_auth_required_v = (
                    str(body.get("tester_auth_required", "false")).strip().lower()
                    in {"1", "true", "yes", "on"}
                )
                tester_auth_mode_v = (
                    str(body.get("tester_auth_mode", "")).strip().lower() or "none"
                )
                tester_auth_login_url_v = str(
                    body.get("tester_auth_login_url", "")
                ).strip()
                tester_auth_username_v = str(
                    body.get("tester_auth_username", "")
                ).strip()
                tester_auth_password_v = str(
                    body.get("tester_auth_password", "")
                ).strip()
                tester_auth_jwt_v = str(body.get("tester_auth_jwt", "")).strip()
                tester_auth_headers_v = str(
                    body.get("tester_auth_headers", "")
                ).strip()
                env = _read_env(env_path)
                # Compute effective LLM key: prefer new value from form, else resolve from env
                effective_llm_key = llm_key_v or _resolve_llm_api_key(
                    llm_provider_v, env
                )
                if llm_provider_v in _CLOUD_LLM_PROVIDERS and not effective_llm_key:
                    self._json(
                        {"error": f"{llm_provider_v} requires an API key."},
                        status=400,
                    )
                    return

                cfg = _read_config(config_path)
                cfg.setdefault("posthog", {})["host"] = host_v
                cfg.setdefault("posthog", {})["project_id"] = project_v
                cfg.setdefault("llm", {})["provider"] = llm_provider_v
                cfg.setdefault("llm", {})["base_url"] = llm_base_url_v
                cfg.setdefault("llm", {})["model"] = llm_model_v
                cfg.setdefault("tester", {})["app_url"] = tester_app_url_v
                cfg.setdefault("tester", {})["start_command"] = tester_start_command_v
                cfg.setdefault("tester", {})[
                    "harness_command"
                ] = tester_harness_command_v
                cfg.setdefault("tester", {})["max_retries"] = tester_max_retries_v
                cfg.setdefault("tester", {})["auth_required"] = tester_auth_required_v
                cfg.setdefault("tester", {})["auth_mode"] = tester_auth_mode_v
                cfg.setdefault("tester", {})["auth_login_url"] = tester_auth_login_url_v
                cfg.setdefault("tester", {})["auth_username"] = tester_auth_username_v
                _write_config(config_path, cfg)

                # Empty secret fields mean "keep existing" to avoid accidental secret clearing.
                if key_v:
                    env["RETRACE_POSTHOG_API_KEY"] = key_v
                if llm_key_v:
                    env["RETRACE_LLM_API_KEY"] = llm_key_v
                if tester_auth_password_v:
                    env["RETRACE_TESTER_AUTH_PASSWORD"] = tester_auth_password_v
                if tester_auth_jwt_v:
                    env["RETRACE_TESTER_AUTH_JWT"] = tester_auth_jwt_v
                if tester_auth_headers_v:
                    env["RETRACE_TESTER_AUTH_HEADERS"] = tester_auth_headers_v
                _write_env(env_path, env)

                self._json({"ok": True, "settings": current_settings()})
                return

            if path == "/api/github/repos":
                body = self._read_json_body()
                payload, status = _connect_github_repo_payload(
                    store=store,
                    repo_full_name=str(body.get("repo", "")).strip(),
                    default_branch=str(body.get("branch", "")).strip(),
                    local_path=str(body.get("local_path", "")).strip(),
                )
                self._json(payload, status=status)
                return

            if path == "/api/sdk-keys":
                body = self._read_json_body()
                payload, status = _create_sdk_key_payload(
                    store=store,
                    project_name=str(body.get("project", "")).strip(),
                    environment_name=str(body.get("environment", "")).strip(),
                    name=str(body.get("name", "")).strip(),
                )
                self._json(payload, status=status)
                return

            if path == "/api/llm/models":
                body = self._read_json_body()
                provider_v = (
                    str(body.get("provider", "")).strip() or "openai_compatible"
                )
                base_url_v = str(body.get("base_url", "")).strip()
                # Compute effective LLM key: prefer value from request, else resolve from env
                env = _read_env(env_path)
                key_v = str(body.get("api_key", "")).strip() or _resolve_llm_api_key(
                    provider_v, env
                )
                result = _llm_models(provider_v, base_url_v, key_v)
                self._json(result, status=200 if result.get("ok") else 400)
                return

            if path == "/api/tester/specs":
                body = self._read_json_body()
                settings = current_settings()
                profile_name = str(body.get("auth_profile", "")).strip()
                profile: dict[str, Any] = {}
                if profile_name:
                    tester_cfg = _read_config(config_path).get("tester") or {}
                    profiles = tester_cfg.get("auth_profiles") or {}
                    if not isinstance(profiles, dict) or profile_name not in profiles:
                        self._json(
                            {"ok": False, "error": f"unknown auth profile: {profile_name}"},
                            status=400,
                        )
                        return
                    profile_raw = profiles.get(profile_name) or {}
                    if not isinstance(profile_raw, dict):
                        self._json(
                            {
                                "ok": False,
                                "error": f"auth profile must be an object: {profile_name}",
                            },
                            status=400,
                        )
                        return
                    profile = dict(profile_raw)
                    forbidden = {"password", "jwt", "token", "headers", "headers_json"}
                    leaked: list[str] = []

                    def scan_secret_keys(value: Any, path_s: str = "") -> None:
                        if isinstance(value, dict):
                            for key, nested in value.items():
                                key_s = str(key)
                                child_path = f"{path_s}.{key_s}" if path_s else key_s
                                if key_s in forbidden:
                                    leaked.append(child_path)
                                scan_secret_keys(nested, child_path)
                        elif isinstance(value, list):
                            for idx, item in enumerate(value):
                                scan_secret_keys(item, f"{path_s}[{idx}]")

                    scan_secret_keys(profile)
                    if leaked:
                        self._json(
                            {
                                "ok": False,
                                "error": (
                                    "auth profile must reference env vars, not "
                                    "secret values: "
                                    + ", ".join(sorted(leaked))
                                ),
                            },
                            status=400,
                        )
                        return
                auth_setup_steps = body.get("auth_setup_steps")
                if auth_setup_steps is None and profile:
                    auth_setup_steps = profile.get(
                        "auth_setup_steps", profile.get("setup_steps", [])
                    )
                if auth_setup_steps is None:
                    auth_setup_steps = []
                if not isinstance(auth_setup_steps, list):
                    self._json(
                        {"ok": False, "error": "auth_setup_steps must be a list"},
                        status=400,
                    )
                    return
                if not all(isinstance(step, dict) for step in auth_setup_steps):
                    self._json(
                        {
                            "ok": False,
                            "error": "auth_setup_steps entries must be objects",
                        },
                        status=400,
                    )
                    return
                auth_required = bool(settings["tester_auth_required"])
                auth_mode = str(settings["tester_auth_mode"] or "none")
                auth_login_url = str(settings["tester_auth_login_url"] or "")
                auth_username = str(settings["tester_auth_username"] or "")
                auth_password_env = "RETRACE_TESTER_AUTH_PASSWORD"
                auth_jwt_env = "RETRACE_TESTER_AUTH_JWT"
                auth_headers_env = "RETRACE_TESTER_AUTH_HEADERS"
                if profile:
                    auth_required = True
                    auth_mode = str(profile.get("mode") or auth_mode)
                    auth_login_url = str(profile.get("login_url") or auth_login_url)
                    auth_username = str(profile.get("username") or auth_username)
                    auth_password_env = str(
                        profile.get("password_env") or auth_password_env
                    )
                    auth_jwt_env = str(profile.get("jwt_env") or auth_jwt_env)
                    auth_headers_env = str(profile.get("headers_env") or auth_headers_env)
                try:
                    spec = create_spec(
                        specs_dir=specs_dir_for_data_dir(data_dir),
                        name=str(body.get("name", "")).strip() or "UI test",
                        prompt=str(body.get("prompt", "")).strip(),
                        app_url=str(body.get("app_url", "")).strip()
                        or settings["tester_app_url"],
                        start_command=str(body.get("start_command", "")).strip()
                        or settings["tester_start_command"],
                        harness_command=str(body.get("harness_command", "")).strip()
                        or settings["tester_harness_command"],
                        mode=str(body.get("mode", "")).strip() or "describe",
                        auth_required=auth_required,
                        auth_mode=auth_mode,
                        auth_login_url=auth_login_url,
                        auth_username=auth_username,
                        auth_password_env=auth_password_env,
                        auth_jwt_env=auth_jwt_env,
                        auth_headers_env=auth_headers_env,
                        auth_profile=profile_name,
                        auth_setup_steps=[dict(step) for step in auth_setup_steps],
                    )
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, status=400)
                    return
                self._json({"ok": True, "spec": spec.__dict__})
                return

            if path == "/api/tester/run":
                body = self._read_json_body()
                spec_id = str(body.get("spec_id", "")).strip()
                if not spec_id:
                    self._json({"ok": False, "error": "spec_id is required"}, status=400)
                    return
                try:
                    spec = load_spec(specs_dir_for_data_dir(data_dir), spec_id)
                except Exception:
                    self._json({"ok": False, "error": "spec not found"}, status=404)
                    return
                try:
                    retries_v = max(
                        0,
                        int(
                            body.get(
                                "retries",
                                current_settings().get("tester_max_retries", 1),
                            )
                        ),
                    )
                except Exception:
                    retries_v = int(current_settings().get("tester_max_retries", 1))
                result = run_spec(
                    spec=spec,
                    runs_dir=runs_dir_for_data_dir(data_dir),
                    prompt_override=str(body.get("prompt", "")).strip() or None,
                    app_url_override=str(body.get("app_url", "")).strip() or None,
                    start_command_override=str(body.get("start_command", "")).strip()
                    or None,
                    max_retries=retries_v,
                    auth_context_override={
                        "required": "true" if bool(spec.auth_required) else "false",
                        "mode": str(spec.auth_mode or "none"),
                        "login_url": str(spec.auth_login_url or ""),
                        "username": str(spec.auth_username or ""),
                        "password": _read_env(env_path).get(
                            spec.auth_password_env, ""
                        ),
                        "jwt": _read_env(env_path).get(spec.auth_jwt_env, ""),
                        "headers_json": _read_env(env_path).get(
                            spec.auth_headers_env, ""
                        ),
                    },
                    cwd=config_path.parent,
                )
                try:
                    links = store.list_failure_test_links(spec_id=result.spec_id, limit=2)
                    if len(links) == 1:
                        store.update_failure_test_link_run(
                            spec_id=result.spec_id,
                            run_result=result,
                            link_id=links[0].id,
                        )
                except Exception:
                    logger.warning(
                        "failed to persist failure_test_link run metadata",
                        extra={"spec_id": result.spec_id, "run_id": result.run_id},
                        exc_info=True,
                    )
                status = 200 if result.ok else 400
                self._json({"ok": result.ok, "result": result.__dict__}, status=status)
                return

            if path == "/api/tester/draft":
                body = self._read_json_body()
                payload, status = _edit_ui_draft_payload(
                    data_dir=data_dir,
                    spec_id=str(body.get("spec_id", "")).strip(),
                    name=str(body.get("name", "")).strip(),
                    prompt=str(body.get("prompt", "")).strip(),
                    app_url=str(body.get("app_url", "")).strip(),
                    steps=body.get("steps") if "steps" in body else None,
                    assertions=body.get("assertions") if "assertions" in body else None,
                    review_note=str(body.get("review_note", "")).strip(),
                    accept=bool(body.get("accept") or False),
                )
                self._json(payload, status=status)
                return

            if path == "/api/replay-issue/spec":
                body = self._read_json_body()
                workspace = store.ensure_workspace(project_name="Default")
                payload, status = _generate_replay_issue_spec_payload(
                    store=store,
                    data_dir=data_dir,
                    issue_id=str(body.get("issue_id", "")).strip(),
                    project_id=str(body.get("project_id", "")).strip()
                    or workspace.project_id,
                    environment_id=str(body.get("environment_id", "")).strip()
                    or workspace.environment_id,
                    app_url=str(body.get("app_url", "")).strip(),
                )
                self._json(payload, status=status)
                return

            if path == "/api/replay-issues/specs":
                body = self._read_json_body()
                workspace = store.ensure_workspace(project_name="Default")
                raw_issue_ids = body.get("issue_ids")
                issue_ids = raw_issue_ids if isinstance(raw_issue_ids, list) else []
                payload, status = _generate_replay_issue_specs_payload(
                    store=store,
                    data_dir=data_dir,
                    project_id=str(body.get("project_id", "")).strip()
                    or workspace.project_id,
                    environment_id=str(body.get("environment_id", "")).strip()
                    or workspace.environment_id,
                    issue_ids=[str(item) for item in issue_ids],
                    status=str(body.get("status", "")).strip(),
                    app_url=str(body.get("app_url", "")).strip(),
                    limit=int(body.get("limit") or 25),
                    missing_only=bool(body.get("missing_only", True)),
                )
                self._json(payload, status=status)
                return

            if path == "/api/replay-issue/api-spec":
                body = self._read_json_body()
                workspace = store.ensure_workspace(project_name="Default")
                payload, status = _generate_replay_issue_api_spec_payload(
                    store=store,
                    data_dir=data_dir,
                    issue_id=str(body.get("issue_id", "")).strip(),
                    project_id=str(body.get("project_id", "")).strip()
                    or workspace.project_id,
                    environment_id=str(body.get("environment_id", "")).strip()
                    or workspace.environment_id,
                    app_url=str(body.get("app_url", "")).strip(),
                )
                self._json(payload, status=status)
                return

            if path == "/api/replay-issue/api-run":
                body = self._read_json_body()
                payload, status = _run_replay_issue_api_spec_payload(
                    store=store,
                    data_dir=data_dir,
                    spec_id=str(body.get("spec_id", "")).strip(),
                )
                self._json(payload, status=status)
                return

            if path == "/api/api-spec/run":
                body = self._read_json_body()
                payload, status = _run_api_spec_payload(
                    data_dir=data_dir,
                    spec_id=str(body.get("spec_id", "")).strip(),
                )
                self._json(payload, status=status)
                return

            if path == "/api/api-suite/run":
                body = self._read_json_body()
                payload, status = _run_api_suite_payload(
                    data_dir=data_dir,
                    suite_id=str(body.get("suite_id", "")).strip(),
                )
                self._json(payload, status=status)
                return

            if path == "/api/replay-issue/fix-prompts":
                body = self._read_json_body()
                workspace = store.ensure_workspace(project_name="Default")
                payload, status = _generate_replay_issue_fix_prompts_payload(
                    store=store,
                    output_dir=output_dir,
                    issue_id=str(body.get("issue_id", "")).strip(),
                    project_id=str(body.get("project_id", "")).strip()
                    or workspace.project_id,
                    environment_id=str(body.get("environment_id", "")).strip()
                    or workspace.environment_id,
                    repo_full_name=str(body.get("repo", "")).strip()
                    or repo_full_name
                    or "",
                )
                self._json(payload, status=status)
                return

            if path == "/api/replay-issue/status":
                body = self._read_json_body()
                workspace = store.ensure_workspace(project_name="Default")
                payload, status = _transition_replay_issue_payload(
                    store=store,
                    issue_id=str(body.get("issue_id", "")).strip(),
                    project_id=str(body.get("project_id", "")).strip()
                    or workspace.project_id,
                    environment_id=str(body.get("environment_id", "")).strip()
                    or workspace.environment_id,
                    status=str(body.get("status", "")).strip(),
                )
                self._json(payload, status=status)
                return

            if path == "/api/replay-issues/verify-resolved":
                body = self._read_json_body()
                workspace = store.ensure_workspace(project_name="Default")
                try:
                    limit_v = int(body.get("limit") or 10)
                except (TypeError, ValueError):
                    limit_v = 10
                payload, status = _verify_resolved_issues_payload(
                    store=store,
                    data_dir=data_dir,
                    cwd=config_path.parent,
                    project_id=str(body.get("project_id", "")).strip()
                    or workspace.project_id,
                    environment_id=str(body.get("environment_id", "")).strip()
                    or workspace.environment_id,
                    limit=limit_v,
                    dry_run=bool(body.get("dry_run") or False),
                )
                self._json(payload, status=status)
                return

            if path == "/api/replays/process":
                from retrace.commands.api import _build_enricher
                from retrace.config import load_config
                from retrace.replay_core import process_queued_replay_jobs

                body = self._read_json_body()
                try:
                    limit_v = max(1, min(int(body.get("limit") or 25), 100))
                except (TypeError, ValueError):
                    limit_v = 25
                ai_enabled = bool(body.get("ai") or False)
                try:
                    loaded_cfg = load_config(config_path)
                    enricher = _build_enricher(loaded_cfg, store)
                except Exception:
                    loaded_cfg = None
                    enricher = None
                llm_client = LLMClient(loaded_cfg.llm) if ai_enabled and loaded_cfg else None
                try:
                    result = process_queued_replay_jobs(
                        store=store,
                        limit=limit_v,
                        enricher=enricher,
                        llm_client=llm_client,
                    )
                finally:
                    if llm_client is not None:
                        llm_client.close()
                self._json(
                    {
                        "ok": True,
                        "result": {
                            "jobs_seen": result.jobs_seen,
                            "jobs_processed": result.jobs_processed,
                            "jobs_failed": result.jobs_failed,
                            "sessions_processed": result.sessions_processed,
                            "issues_created_or_updated": result.issues_created_or_updated,
                            "ai_analysis": ai_enabled,
                        },
                    }
                )
                return

            if path == "/api/replays/import-posthog":
                from retrace.commands.api import _build_enricher
                from retrace.config import load_config
                from retrace.replay_core import process_queued_replay_jobs

                body = self._read_json_body()
                try:
                    since_hours = max(1, min(int(body.get("since_hours") or 24), 24 * 30))
                except (TypeError, ValueError):
                    since_hours = 24
                try:
                    max_sessions = max(1, min(int(body.get("max_sessions") or 50), 500))
                except (TypeError, ValueError):
                    max_sessions = 50
                process_now = bool(body.get("process", True))
                ai_enabled = bool(body.get("ai") or False)
                try:
                    loaded_cfg = load_config(config_path)
                    workspace = store.ensure_workspace(project_name="Default")
                    ingester = PostHogIngester(
                        loaded_cfg.posthog,
                        store,
                        data_dir=loaded_cfg.run.data_dir,
                    )
                    imported = ingester.import_since_as_replays(
                        datetime.now(timezone.utc) - timedelta(hours=since_hours),
                        max_sessions,
                        project_id=workspace.project_id,
                        environment_id=workspace.environment_id,
                    )
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, status=400)
                    return
                processed_payload: dict[str, Any] = {}
                if process_now:
                    llm_client = LLMClient(loaded_cfg.llm) if ai_enabled else None
                    try:
                        processed = process_queued_replay_jobs(
                            store=store,
                            limit=max_sessions,
                            project_id=workspace.project_id,
                            enricher=_build_enricher(loaded_cfg, store),
                            llm_client=llm_client,
                        )
                    finally:
                        if llm_client is not None:
                            llm_client.close()
                    processed_payload = {
                        "jobs_seen": processed.jobs_seen,
                        "jobs_processed": processed.jobs_processed,
                        "jobs_failed": processed.jobs_failed,
                        "sessions_processed": processed.sessions_processed,
                        "issues_created_or_updated": processed.issues_created_or_updated,
                        "issues_inserted": processed.issues_inserted,
                        "issues_regressed": processed.issues_regressed,
                    }
                self._json(
                    {
                        "ok": True,
                        "imported_sessions": imported.session_ids,
                        "skipped_sessions": imported.skipped_session_ids,
                        "processing_job_ids": imported.processing_job_ids,
                        "processed": processed_payload,
                        "ai_analysis": bool(ai_enabled and process_now),
                    }
                )
                return

            self._json({"error": "not found"}, status=404)

    server = ThreadingHTTPServer((host, port), Handler)
    click.echo(f"Retrace UI running at http://{host}:{port}")
    click.echo("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
