from __future__ import annotations

import ipaddress
import json
import logging
import os
import platform
import re
import socket
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import click
import httpx
import yaml

from retrace.api_suites import api_suites_dir_for_data_dir, list_api_suites
from retrace.api_testing import (
    api_runs_dir_for_data_dir,
    api_specs_dir_for_data_dir,
    load_api_spec,
    run_api_spec,
)
from retrace.fix_suggestions import (
    generate_fix_suggestions,
    parsed_finding_from_replay_issue,
    replay_issue_report_key,
    slugify,
)
from retrace.evidence import build_evidence_timeline
from retrace.ingester import PostHogIngester
from retrace.llm.client import LLMClient
from retrace.llm.client import build_llm_http_request
from retrace.reports.parser import parse_report_findings
from retrace.replay_specs import (
    _redacted_url,
    generate_api_spec_from_replay_issue,
    generate_spec_from_replay_issue,
)
from retrace.sdk_keys import create_sdk_key
from retrace.sentry_compat import build_sentry_dsn
from retrace.storage import GitHubRepoRow, Storage
from retrace.tester import (
    DEFAULT_APP_URL,
    DEFAULT_HARNESS_COMMAND,
    create_spec,
    list_specs,
    load_run_summaries,
    load_spec,
    now_iso,
    run_spec,
    runs_dir_for_data_dir,
    save_spec,
    specs_dir_for_data_dir,
    validate_spec,
)

logger = logging.getLogger(__name__)

_CLOUD_LLM_PROVIDERS = {"openai", "anthropic", "openrouter"}


def _create_pinned_transport(
    pinned_ip: str, hostname: str, scheme: str
) -> httpx.HTTPTransport:
    """Create an httpx transport that connects to a pinned IP address.

    This prevents DNS rebinding/TOCTOU attacks by ensuring the HTTP connection uses the
    IP address that was validated during URL checking, rather than performing a fresh
    DNS resolution that could return a different (malicious) IP.

    Args:
        pinned_ip: The IP address to connect to (already validated)
        hostname: The original hostname (used for Host header and, for HTTPS, SNI)
        scheme: The URL scheme ('http' or 'https')

    Returns:
        An HTTPTransport configured to connect to the pinned IP with proper SNI for HTTPS

    Implementation:
        For HTTPS, this creates a custom NetworkBackend that wraps SSL sockets with the
        correct server_hostname for SNI, ensuring TLS certificate validation works properly
        even though we're connecting to an IP address.
    """
    import ssl

    class PinnedHTTPTransport(httpx.HTTPTransport):
        """Transport that rewrites requests to use a pinned IP address."""

        def __init__(
            self, pinned_ip: str, original_hostname: str, *args: Any, **kwargs: Any
        ):
            self._pinned_ip = pinned_ip
            self._original_hostname = original_hostname
            super().__init__(*args, **kwargs)

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            # Rewrite the request URL to use the pinned IP instead of hostname
            original_url = str(request.url)
            parsed = urlparse(original_url)

            # Set Host header to the original hostname (required for virtual hosting)
            request.headers["Host"] = self._original_hostname

            # Prepare the pinned IP for use in URL netloc
            # IPv6 addresses must be bracketed; also percent-encode zone IDs
            pinned_ip_for_url = self._pinned_ip
            if ":" in self._pinned_ip:
                # This looks like an IPv6 address
                # Percent-encode any zone identifier (% becomes %25)
                pinned_ip_for_url = self._pinned_ip.replace("%", "%25")
                # Wrap in brackets for URL
                pinned_ip_for_url = f"[{pinned_ip_for_url}]"

            # Replace hostname with pinned IP in the netloc
            if ":" in parsed.netloc and not parsed.netloc.startswith("["):
                # Explicit port present: hostname:port -> ip:port
                _, port = parsed.netloc.rsplit(":", 1)
                new_netloc = f"{pinned_ip_for_url}:{port}"
            else:
                # No explicit port: hostname -> ip
                new_netloc = pinned_ip_for_url

            # Reconstruct the URL with pinned IP
            new_path = parsed.path or "/"
            new_url = f"{parsed.scheme}://{new_netloc}{new_path}"
            if parsed.query:
                new_url += f"?{parsed.query}"
            if parsed.fragment:
                new_url += f"#{parsed.fragment}"

            request.url = httpx.URL(new_url)

            return super().handle_request(request)

    if scheme == "https":
        # For HTTPS, create a custom network backend that sets correct SNI
        ssl_context = ssl.create_default_context()

        # Create a custom NetworkBackend that wraps sockets with correct server_hostname
        from httpcore._backends.sync import SyncBackend

        class SNINetworkBackend(SyncBackend):
            """NetworkBackend that overrides SNI hostname for SSL connections."""

            def __init__(self, pinned_ip: str, sni_hostname: str):
                super().__init__()
                self._pinned_ip = pinned_ip
                self._sni_hostname = sni_hostname

            def start_tls(
                self,
                sock: socket.socket,
                ssl_context: ssl.SSLContext,
                server_hostname: Optional[str] = None,
                timeout: Optional[float] = None,
            ) -> ssl.SSLSocket:
                # Override server_hostname to use the original hostname for SNI
                # when we're connecting to our pinned IP
                try:
                    peername = sock.getpeername()
                    if peername and peername[0] == self._pinned_ip:
                        server_hostname = self._sni_hostname
                except Exception:
                    pass

                # Wrap the socket with SSL using the correct server_hostname
                return super().start_tls(sock, ssl_context, server_hostname, timeout)

        network_backend = SNINetworkBackend(pinned_ip=pinned_ip, sni_hostname=hostname)

        transport = PinnedHTTPTransport(
            pinned_ip=pinned_ip,
            original_hostname=hostname,
            verify=ssl_context,
            network_backend=network_backend,
        )

        return transport
    else:
        # For HTTP, no SSL/SNI concerns
        transport = PinnedHTTPTransport(
            pinned_ip=pinned_ip,
            original_hostname=hostname,
        )

        return transport


_LLM_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai_compatible": {
        "base_url": "http://localhost:8080/v1",
        "model": "llama-3.1-8b-instruct",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-3-5-sonnet-latest",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
    },
}


def _default_config() -> dict[str, Any]:
    llm_default = _LLM_PROVIDER_DEFAULTS["openai_compatible"]
    return {
        "posthog": {
            "host": "https://us.i.posthog.com",
            "project_id": "",
        },
        "llm": {
            "provider": "openai_compatible",
            "base_url": llm_default["base_url"],
            "model": llm_default["model"],
        },
        "run": {
            "lookback_hours": 168,
            "max_sessions_per_run": 50,
            "output_dir": "./reports",
            "data_dir": "./data",
        },
        "detectors": {
            "console_error": True,
            "network_5xx": True,
            "network_4xx": True,
            "rage_click": True,
            "dead_click": True,
            "error_toast": True,
            "blank_render": True,
            "session_abandon_on_error": True,
        },
        "cluster": {
            "min_size": 1,
        },
        "tester": {
            "app_url": DEFAULT_APP_URL,
            "start_command": "",
            "harness_command": DEFAULT_HARNESS_COMMAND,
            "max_retries": 1,
            "auth_required": False,
            "auth_mode": "none",
            "auth_login_url": "",
            "auth_username": "",
            "auth_password_env": "RETRACE_TESTER_AUTH_PASSWORD",
            "auth_jwt_env": "RETRACE_TESTER_AUTH_JWT",
            "auth_headers_env": "RETRACE_TESTER_AUTH_HEADERS",
        },
    }


def _read_config(config_path: Path) -> dict[str, Any]:
    cfg = _default_config()
    if not config_path.exists():
        return cfg
    raw = yaml.safe_load(config_path.read_text()) or {}
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def _write_config(config_path: Path, cfg: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))


def _read_env(env_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _write_env(env_path: Path, vals: dict[str, str]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in vals.items()]
    env_path.write_text("\n".join(lines) + "\n")


def _latest_report(report_dir: Path) -> Optional[Path]:
    files = sorted(
        report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return files[0] if files else None


def _session_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _gh_checks() -> dict[str, Any]:
    sys_name = platform.system().lower()
    if "darwin" in sys_name:
        install_cmd = "brew install gh"
    elif "linux" in sys_name:
        install_cmd = "sudo apt install gh"
    elif "windows" in sys_name:
        install_cmd = "winget install --id GitHub.cli"
    else:
        install_cmd = "See https://cli.github.com/ for install instructions"

    gh_path = shutil.which("gh")
    installed = gh_path is not None
    authed = False
    auth_detail = ""
    if installed:
        try:
            proc = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
            authed = proc.returncode == 0
            auth_detail = (proc.stdout or proc.stderr or "").strip()[:500]
        except Exception as exc:
            auth_detail = str(exc)

    return {
        "installed": installed,
        "authed": authed,
        "gh_path": gh_path or "",
        "auth_detail": auth_detail,
        "commands": {
            "install": install_cmd,
            "login": "gh auth login",
            "status": "gh auth status",
        },
    }


def _posthog_check(host: str, project_id: str, api_key: str) -> dict[str, Any]:
    configured = bool(host.strip() and project_id.strip() and api_key.strip())
    if not configured:
        return {
            "configured": False,
            "reachable": None,
            "detail": "Missing host/project/API key.",
        }

    # Validate and pin the IP to prevent DNS rebinding
    ok_url, safe_host, err, pinned_ips = _validate_base_url(host)
    if not ok_url:
        return {"configured": True, "reachable": False, "detail": err}

    url = f"{safe_host.rstrip('/')}/api/projects/{project_id}/"
    parsed = urlparse(safe_host)
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    try:
        for pinned_ip in pinned_ips:
            try:
                # Use validated/pinned IP to prevent TOCTOU/DNS rebinding.
                transport = _create_pinned_transport(
                    pinned_ip, parsed.hostname or "", parsed.scheme or ""
                )
                with httpx.Client(timeout=8, transport=transport) as c:
                    r = c.get(url, headers={"Authorization": f"Bearer {api_key}"})
                last_status = r.status_code
                if r.status_code // 100 == 2:
                    return {
                        "configured": True,
                        "reachable": True,
                        "detail": f"OK ({r.status_code})",
                    }
            except Exception as exc:
                last_exc = exc
                continue
        if last_status is not None:
            return {
                "configured": True,
                "reachable": False,
                "detail": f"HTTP {last_status}",
            }
        if last_exc:
            raise last_exc
        return {"configured": True, "reachable": False, "detail": "No pinned IPs available."}
    except Exception as exc:
        return {"configured": True, "reachable": False, "detail": str(exc)}


def _replay_api_check(base_url: str = "http://127.0.0.1:8788") -> dict[str, Any]:
    url = base_url.rstrip("/") + "/healthz"
    try:
        with httpx.Client(timeout=2) as c:
            response = c.get(url)
        if response.status_code // 100 == 2:
            return {
                "configured": True,
                "reachable": True,
                "detail": f"OK ({response.status_code})",
                "url": base_url.rstrip("/"),
                "commands": {"serve": "retrace api serve"},
            }
        return {
            "configured": True,
            "reachable": False,
            "detail": f"HTTP {response.status_code}",
            "url": base_url.rstrip("/"),
            "commands": {"serve": "retrace api serve"},
        }
    except Exception as exc:
        return {
            "configured": True,
            "reachable": False,
            "detail": str(exc),
            "url": base_url.rstrip("/"),
            "commands": {"serve": "retrace api serve"},
        }


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _validate_base_url(base_url: str) -> tuple[bool, str, str, list[str]]:
    """Validate outbound model-provider URLs to reduce SSRF risk.

    Returns: (ok, normalized_url, error_message, pinned_ips)
    pinned_ips are acceptable IPs resolved during validation and must be used
    for actual HTTP requests to prevent DNS rebinding attacks.
    """
    raw = base_url.strip()
    if not raw:
        return False, "", "Base URL is required.", []

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return False, "", "Base URL must use http or https.", []
    if not parsed.hostname:
        return False, "", "Base URL must include a hostname.", []

    if parsed.query or parsed.fragment:
        return False, "", "Base URL must not include query parameters or fragments.", []

    default_port = 443 if scheme == "https" else 80
    port = parsed.port or default_port
    allow_internal = _truthy_env("RETRACE_ALLOW_INTERNAL_URLS")
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, "", f"Base URL hostname resolution failed: {exc}", []

    pinned_ips: list[str] = []
    if not allow_internal:
        for _, _, _, _, sockaddr in infos:
            if not sockaddr:
                continue
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                continue
            ip_s = sockaddr[0]
            if ip_s not in pinned_ips:
                pinned_ips.append(ip_s)
    else:
        # If internal URLs are allowed, keep all resolved IPs (deduped).
        for _, _, _, _, sockaddr in infos:
            if sockaddr:
                ip_s = sockaddr[0]
                if ip_s not in pinned_ips:
                    pinned_ips.append(ip_s)

    if not pinned_ips:
        return (
            False,
            "",
            "No acceptable IP addresses found for base URL. "
            "Set RETRACE_ALLOW_INTERNAL_URLS=true to allow internal hosts.",
            [],
        )

    normalized = f"{scheme}://{parsed.netloc}{parsed.path or ''}".rstrip("/")
    return True, normalized, "", pinned_ips


def _llm_check(
    provider: str, base_url: str, model: str, api_key: str
) -> dict[str, Any]:
    p = provider.strip().lower()
    configured = bool(p and base_url.strip() and model.strip())
    if not configured:
        return {
            "configured": False,
            "reachable": None,
            "detail": "Missing provider/base URL/model.",
        }
    ok_url, safe_base_url, err, pinned_ips = _validate_base_url(base_url)
    if not ok_url:
        return {"configured": True, "reachable": False, "detail": err}
    parsed = urlparse(safe_base_url)
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    try:
        url, headers, body = build_llm_http_request(
            provider=p,
            base_url=safe_base_url,
            model=model,
            api_key=api_key,
            system="You are a test assistant.",
            user="reply with ping",
            temperature=0.0,
            response_json=False,
            max_tokens=8,
        )
        for pinned_ip in pinned_ips:
            try:
                transport = _create_pinned_transport(
                    pinned_ip, parsed.hostname or "", parsed.scheme or ""
                )
                with httpx.Client(timeout=12, transport=transport) as c:
                    r = c.post(url, headers=headers, json=body)
                last_status = r.status_code
                if r.status_code // 100 == 2:
                    return {
                        "configured": True,
                        "reachable": True,
                        "detail": f"OK ({r.status_code})",
                    }
            except Exception as exc:
                last_exc = exc
                continue
        if last_status is not None:
            return {
                "configured": True,
                "reachable": False,
                "detail": f"HTTP {last_status}",
            }
        if last_exc:
            raise last_exc
        return {"configured": True, "reachable": False, "detail": "No pinned IPs available."}
    except Exception as exc:
        return {"configured": True, "reachable": False, "detail": str(exc)}


def _llm_defaults(provider: str) -> dict[str, str]:
    return _LLM_PROVIDER_DEFAULTS.get(
        provider.strip().lower(), _LLM_PROVIDER_DEFAULTS["openai_compatible"]
    )


def _resolve_llm_api_key(provider: str, env_vars: dict[str, str]) -> str:
    """Resolve the effective LLM API key using the same logic as load_config().

    Args:
        provider: The LLM provider name
        env_vars: Dictionary of environment variables (from .env file or os.environ)

    Returns:
        The effective API key (may be empty string)
    """
    llm_key_env = env_vars.get("RETRACE_LLM_API_KEY", "").strip()
    if not llm_key_env:
        provider_env_map = {
            "openai": "RETRACE_OPENAI_API_KEY",
            "anthropic": "RETRACE_ANTHROPIC_API_KEY",
            "openrouter": "RETRACE_OPENROUTER_API_KEY",
        }
        provider_env = provider_env_map.get(provider.strip().lower())
        if provider_env:
            llm_key_env = env_vars.get(provider_env, "").strip()
    return llm_key_env


def _llm_models(provider: str, base_url: str, api_key: str) -> dict[str, Any]:
    p = provider.strip().lower() or "openai_compatible"
    if p in _CLOUD_LLM_PROVIDERS and not api_key.strip():
        return {"ok": False, "error": "API key required for selected provider."}
    ok_url, safe_base_url, err, pinned_ips = _validate_base_url(base_url)
    if not ok_url:
        return {"ok": False, "error": err}
    parsed = urlparse(safe_base_url)
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    try:
        # We can't pass transport to fetch_llm_models without modifying it,
        # so we inline the model fetching logic here with our secure transport
        from retrace.llm.client import _build_headers, _extract_model_ids

        headers = _build_headers(provider=p, api_key=api_key.strip() or None)
        url = f"{safe_base_url.rstrip('/')}/models"
        for pinned_ip in pinned_ips:
            try:
                transport = _create_pinned_transport(
                    pinned_ip, parsed.hostname or "", parsed.scheme or ""
                )
                with httpx.Client(timeout=10, transport=transport) as c:
                    resp = c.get(url, headers=headers)
                    last_status = resp.status_code
                    resp.raise_for_status()
                    payload = resp.json()
                models = _extract_model_ids(payload)
                return {"ok": True, "models": models}
            except Exception as exc:
                last_exc = exc
                continue
        if last_status is not None:
            return {"ok": False, "error": f"HTTP {last_status}"}
        if last_exc:
            raise last_exc
        return {"ok": False, "error": "No pinned IPs available."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _to_findings_payload(
    *,
    store: Storage,
    report_path: Optional[Path],
    repo_full_name: Optional[str],
) -> list[dict[str, Any]]:
    if report_path is None or not report_path.exists():
        return []

    parsed = parse_report_findings(report_path)
    rows = store.list_report_findings(str(report_path))
    by_hash = {r.finding_hash: r for r in rows}
    repos = store.list_github_repos()
    chosen_repo = None
    if repo_full_name:
        for r in repos:
            if r.repo_full_name == repo_full_name:
                chosen_repo = r
                break
    if chosen_repo is None and repos:
        chosen_repo = repos[0]

    out: list[dict[str, Any]] = []
    for f in parsed:
        h = f.finding_hash()
        row = by_hash.get(h)
        candidates: list[dict[str, Any]] = []
        prompts: dict[str, str] = {}
        if row and chosen_repo:
            for c in store.list_code_candidates(
                finding_id=row.id, repo_id=chosen_repo.id
            ):
                rationale = ""
                try:
                    rationale = (json.loads(str(c["rationale_json"])) or {}).get(
                        "rationale", ""
                    )
                except Exception:
                    rationale = str(c["rationale_json"])
                candidates.append(
                    {
                        "file_path": str(c["file_path"]),
                        "symbol": c["symbol"],
                        "score": float(c["score"]),
                        "rationale": rationale,
                    }
                )
            for p in store.list_fix_prompts(finding_id=row.id, repo_id=chosen_repo.id):
                prompts[p.agent_target] = p.prompt_markdown

        out.append(
            {
                "id": h,
                "title": f.title,
                "severity": f.severity,
                "category": f.category,
                "session_id": _session_id_from_url(f.session_url),
                "session_url": f.session_url,
                "evidence_text": f.evidence_text,
                "distinct_id": row.distinct_id if row else "",
                "error_issue_ids": row.error_issue_ids if row else [],
                "trace_ids": row.trace_ids if row else [],
                "top_stack_frame": row.top_stack_frame if row else "",
                "error_tracking_url": row.error_tracking_url if row else "",
                "logs_url": row.logs_url if row else "",
                "first_error_ts_ms": row.first_error_ts_ms if row else 0,
                "last_error_ts_ms": row.last_error_ts_ms if row else 0,
                "regression_state": row.regression_state if row else "new",
                "regression_occurrence_count": row.regression_occurrence_count
                if row
                else 1,
                "candidates": candidates,
                "prompts": prompts,
            }
        )
    return out


def _json_field(row: Any, key: str, fallback: Any) -> Any:
    try:
        return json.loads(str(row[key] or ""))
    except Exception:
        return fallback


def _failure_test_link_payload(link: Any) -> dict[str, Any]:
    return {
        "id": link.id,
        "failure_id": link.failure_id,
        "issue_id": link.issue_id,
        "issue_public_id": link.issue_public_id,
        "spec_id": link.spec_id,
        "spec_name": link.spec_name,
        "spec_path": link.spec_path,
        "source": link.source,
        "coverage_state": link.coverage_state,
        "latest_run_id": link.latest_run_id,
        "latest_run_status": link.latest_run_status,
        "latest_run_classification": link.latest_run_classification,
        "latest_run_ok": link.latest_run_ok,
        "latest_run_at": (
            link.latest_run_at.isoformat() if link.latest_run_at is not None else ""
        ),
        "created_at": link.created_at.isoformat(),
        "updated_at": link.updated_at.isoformat(),
    }


def _repair_task_payload(task: Any | None) -> dict[str, Any] | None:
    if task is None:
        return None
    return {
        "id": task.id,
        "public_id": task.public_id,
        "failure_id": task.failure_id,
        "source_type": task.source_type,
        "source_external_id": task.source_external_id,
        "title": task.title,
        "status": task.status,
        "likely_files": task.likely_files,
        "prompt_artifacts": task.prompt_artifacts,
        "validation_commands": task.validation_commands,
        "branch": task.branch,
        "pr_url": task.pr_url,
        "risk_notes": task.risk_notes,
        "metadata": task.metadata,
        "evidence_ids": task.evidence_ids,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def _issue_workflow_payload(issue: dict[str, Any]) -> dict[str, Any]:
    timeline_count = len(issue.get("timeline") or [])
    reproduction_count = len(issue.get("reproduction_steps") or [])
    test_links = issue.get("test_links") if isinstance(issue.get("test_links"), list) else []
    repair_task = issue.get("repair_task") if isinstance(issue.get("repair_task"), dict) else None
    api_call_count = len(issue.get("api_calls") or [])
    replay_count = len(issue.get("sessions") or [])
    status = str(issue.get("status") or "")
    coverage_states = [str(link.get("coverage_state") or "") for link in test_links]
    latest_statuses = [str(link.get("latest_run_status") or "") for link in test_links]
    api_links = [
        link
        for link in test_links
        if "api" in str(link.get("source") or "").lower()
        or "/api-tests/" in str(link.get("spec_path") or "")
    ]
    if not test_links:
        coverage_state = "not_covered"
    elif "covered_failing" in coverage_states:
        coverage_state = "covered_failing"
    elif "covered_passing" in coverage_states:
        coverage_state = "covered_passing"
    elif "covered_flaky" in coverage_states:
        coverage_state = "covered_flaky"
    else:
        coverage_state = coverage_states[0] or "covered_unverified"

    stages = {
        "evidence": "complete" if timeline_count else "blocked",
        "reproduction": "complete" if reproduction_count or replay_count else "blocked",
        "test": "complete" if test_links else "current",
        "repair": "complete" if repair_task else "current",
        "verification": "complete" if coverage_state == "covered_passing" else "current",
    }
    if not test_links:
        stages["repair"] = "blocked"
        stages["verification"] = "blocked"
    elif coverage_state == "covered_failing":
        stages["verification"] = "blocked"
    if status == "ignored":
        primary_label = "Ignored fingerprint"
        primary_action = "none"
    elif not timeline_count:
        primary_label = "Review raw evidence"
        primary_action = "review_timeline"
    elif not test_links:
        primary_label = "Generate regression test"
        primary_action = "generate_replay_spec"
    elif coverage_state == "covered_failing" and not repair_task:
        primary_label = "Generate repair task"
        primary_action = "generate_repair"
    elif coverage_state == "covered_failing":
        primary_label = "Fix and rerun linked tests"
        primary_action = "run_tests"
    elif status == "resolved" and coverage_state != "covered_passing":
        primary_label = "Verify resolved issue"
        primary_action = "verify_resolved"
    elif coverage_state == "covered_passing":
        primary_label = "Covered by passing test"
        primary_action = "none"
    elif not repair_task and status not in {"resolved", "ignored"}:
        primary_label = "Generate repair task"
        primary_action = "generate_repair"
    else:
        primary_label = "Run linked tests"
        primary_action = "run_tests"

    blockers: list[str] = []
    recommended_actions: list[dict[str, str]] = []
    capture_blocked = False
    if not timeline_count:
        capture_blocked = True
        blockers.append("No normalized evidence timeline is available.")
    if not reproduction_count and not replay_count:
        capture_blocked = True
        blockers.append("No replay or reproduction steps are linked.")
    if not test_links:
        blockers.append("No regression test covers this issue yet.")
        recommended_actions.append(
            {
                "action": "generate_replay_spec",
                "label": "Generate UI regression",
                "reason": "Create an editable UI test from replay evidence.",
            }
        )
    if api_call_count and not api_links:
        recommended_actions.append(
            {
                "action": "generate_api_regression",
                "label": "Generate API regression",
                "reason": "A failed network call is present without API coverage.",
            }
        )
    if test_links and coverage_state == "covered_failing" and not repair_task:
        recommended_actions.append(
            {
                "action": "generate_repair",
                "label": "Generate repair task",
                "reason": "A linked regression still fails and needs repair context.",
            }
        )
    if status == "resolved" and coverage_state != "covered_passing":
        recommended_actions.append(
            {
                "action": "verify_resolved",
                "label": "Verify resolved issue",
                "reason": "Resolved issues should pass linked UI/API regressions.",
            }
        )
    if test_links and not latest_statuses:
        recommended_actions.append(
            {
                "action": "run_tests",
                "label": "Run linked tests",
                "reason": "Coverage exists but has no recorded run result.",
            }
        )
    readiness = "ready_for_repair"
    if capture_blocked:
        readiness = "needs_capture"
    elif not test_links:
        readiness = "needs_test"
    elif coverage_state == "covered_passing":
        readiness = "verified"
    elif coverage_state == "covered_failing" and repair_task:
        readiness = "repair_ready"
    elif coverage_state == "covered_failing":
        readiness = "needs_repair_task"

    return {
        "coverage_state": coverage_state,
        "latest_run_statuses": latest_statuses,
        "primary_action": primary_action,
        "primary_label": primary_label,
        "readiness": readiness,
        "blockers": blockers,
        "recommended_actions": recommended_actions,
        "stage_states": stages,
        "counts": {
            "timeline": timeline_count,
            "reproduction_steps": reproduction_count,
            "replays": replay_count,
            "api_calls": api_call_count,
            "tests": len(test_links),
            "api_tests": len(api_links),
            "repair_tasks": 1 if repair_task else 0,
        },
    }


def _replay_issue_payload(
    row: Any, *, sessions: list[dict[str, Any]]
) -> dict[str, Any]:
    canonical_failure_id = row["canonical_failure_id"]
    evidence = _json_field(row, "evidence_json", {})
    return {
        "id": str(row["id"]),
        "public_id": str(row["public_id"]),
        "project_id": str(row["project_id"]),
        "environment_id": str(row["environment_id"]),
        "status": str(row["status"]),
        "priority": str(row["priority"]),
        "severity": str(row["severity"]),
        "confidence": str(row["confidence"]),
        "title": str(row["title"]),
        "summary": str(row["summary"]),
        "likely_cause": str(row["likely_cause"]),
        "reproduction_steps": _json_field(row, "reproduction_steps_json", []),
        "signal_summary": _json_field(row, "signal_summary_json", {}),
        "evidence": evidence,
        "api_calls": _replay_api_calls(evidence),
        "fingerprint": str(row["fingerprint"]),
        "analysis_status": str(row["analysis_status"]),
        "analysis_model": str(row["analysis_model"]),
        "analysis_prompt_version": str(row["analysis_prompt_version"]),
        "analysis_error": str(row["analysis_error"]),
        "affected_count": int(row["affected_count"]),
        "affected_users": int(row["affected_users"]),
        "representative_session_id": str(row["representative_session_id"]),
        "external_ticket_state": str(row["external_ticket_state"]),
        "external_ticket_url": str(row["external_ticket_url"]),
        "external_ticket_id": str(row["external_ticket_id"]),
        "canonical_failure_id": (
            None if canonical_failure_id is None else str(canonical_failure_id)
        ),
        "first_seen_ms": int(row["first_seen_ms"]),
        "last_seen_ms": int(row["last_seen_ms"]),
        "updated_at": str(row["updated_at"]),
        "sessions": sessions,
        "timeline": _replay_evidence_timeline(evidence),
        "test_links": [],
        "repair_task": None,
        "workflow": {},
        "share_url": f"#issue={str(row['public_id'])}",
    }


def _replay_evidence_timeline(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for event in evidence.get("events") if isinstance(evidence.get("events"), list) else []:
        if not isinstance(event, dict):
            continue
        data_type = event.get("data_type")
        if event.get("type") == 4:
            title = "Navigation"
            summary = str(event.get("href") or "")
            kind = "navigation"
        elif event.get("type") == 3 and event.get("source") == 2 and data_type == 2:
            title = "Click"
            summary = f"Clicked element id {event.get('id', 'unknown')}"
            kind = "interaction"
        elif event.get("type") == 3 and event.get("source") == 5:
            title = "Input"
            summary = f"Entered text in element id {event.get('id', 'unknown')}"
            kind = "interaction"
        else:
            continue
        items.append(
            {
                "id": "",
                "type": "replay_event",
                "kind": kind,
                "occurred_at_ms": int(event.get("timestamp_ms") or 0),
                "source": "replay",
                "title": title,
                "summary": summary,
                "detector": "",
                "detector_hit": False,
                "confidence": "",
                "reason_codes": [],
                "payload": event,
            }
        )
    for signal in evidence.get("signals") if isinstance(evidence.get("signals"), list) else []:
        if not isinstance(signal, dict):
            continue
        details = signal.get("details") if isinstance(signal.get("details"), dict) else {}
        detector = str(signal.get("detector") or "")
        status = details.get("status")
        request_url = details.get("request_url") or details.get("url")
        if detector.startswith("network") and request_url:
            title = f"Network {status or ''}".strip()
            summary = f"{details.get('method') or 'GET'} {request_url} returned {status or 'unknown'}"
            kind = "network"
        elif detector == "console_error":
            title = "Console error"
            summary = str(details.get("message") or details.get("payload") or detector)
            kind = "error"
        else:
            title = detector.replace("_", " ").title() or "Detector signal"
            summary = json.dumps(details, sort_keys=True) if details else title
            kind = "detector"
        items.append(
            {
                "id": "",
                "type": "replay_signal",
                "kind": kind,
                "occurred_at_ms": int(signal.get("timestamp_ms") or 0),
                "source": "replay",
                "title": title,
                "summary": summary,
                "detector": detector,
                "detector_hit": True,
                "confidence": str(signal.get("confidence") or ""),
                "reason_codes": signal.get("reason_codes") if isinstance(signal.get("reason_codes"), list) else [],
                "payload": signal,
            }
        )
    return sorted(items, key=lambda item: (int(item.get("occurred_at_ms") or 0), str(item.get("title") or "")))


def _replay_api_calls(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for signal in evidence.get("signals") if isinstance(evidence.get("signals"), list) else []:
        if not isinstance(signal, dict):
            continue
        detector = str(signal.get("detector") or "")
        if detector not in {"network_4xx", "network_5xx"}:
            continue
        details = signal.get("details") if isinstance(signal.get("details"), dict) else {}
        raw_url = str(details.get("request_url") or details.get("url") or "")
        calls.append(
            {
                "detector": detector,
                "timestamp_ms": int(signal.get("timestamp_ms") or 0),
                "method": str(details.get("method") or details.get("request_method") or "GET").upper(),
                "url": _redacted_url(raw_url),
                "status": details.get("status") or details.get("status_code") or "",
                "confidence": str(signal.get("confidence") or ""),
                "reason_codes": signal.get("reason_codes") if isinstance(signal.get("reason_codes"), list) else [],
                "trace": details.get("trace") if isinstance(details.get("trace"), dict) else {},
            }
        )
    return calls


def _to_replay_dashboard_payload(store: Storage) -> dict[str, Any]:
    issues = []
    issue_rows = store.list_recent_replay_issues(limit=50)
    issue_sessions_by_id: dict[str, list[dict[str, Any]]] = {}
    for session in store.list_replay_issue_sessions_for_issues(
        [str(row["id"]) for row in issue_rows]
    ):
        issue_sessions_by_id.setdefault(str(session["issue_id"]), []).append(
            {
                "session_id": str(session["session_id"]),
                "stable_id": str(session["replay_stable_id"] or session["session_id"]),
                "public_id": str(session["replay_public_id"] or ""),
                "role": str(session["role"]),
                "first_seen_ms": int(session["first_seen_ms"]),
                "last_seen_ms": int(session["last_seen_ms"]),
            }
        )
    for row in issue_rows:
        payload = _replay_issue_payload(
            row,
            sessions=issue_sessions_by_id.get(str(row["id"]), []),
        )
        failure_id = str(row["canonical_failure_id"] or "")
        if failure_id:
            payload["timeline"] = build_evidence_timeline(
                store.list_failure_evidence(failure_id=failure_id)
            )
            payload["test_links"] = [
                _failure_test_link_payload(link)
                for link in store.list_failure_test_links(failure_id=failure_id)
            ]
            repair_tasks = store.list_repair_tasks(failure_id=failure_id, limit=1)
            payload["repair_task"] = _repair_task_payload(
                repair_tasks[0] if repair_tasks else None
            )
        payload["workflow"] = _issue_workflow_payload(payload)
        issues.append(payload)
    sessions = []
    for row in store.list_recent_replay_sessions(limit=50):
        sessions.append(
            {
                "id": str(row["id"]),
                "project_id": str(row["project_id"]),
                "environment_id": str(row["environment_id"]),
                "stable_id": str(row["stable_id"]),
                "public_id": str(row["public_id"]),
                "distinct_id": str(row["distinct_id"]),
                "status": str(row["status"]),
                "event_count": int(row["event_count"]),
                "metadata": _json_field(row, "metadata_json", {}),
                "preview": _json_field(row, "preview_json", {}),
                "last_seen_at": str(row["last_seen_at"]),
                "share_url": f"#replay={str(row['public_id'])}",
            }
        )
    return {"issues": issues, "sessions": sessions}


def _generate_replay_issue_spec_payload(
    *,
    store: Storage,
    data_dir: Path,
    issue_id: str,
    project_id: str,
    environment_id: str,
    app_url: str = "",
) -> tuple[dict[str, Any], int]:
    try:
        generated = generate_spec_from_replay_issue(
            store=store,
            specs_dir=specs_dir_for_data_dir(data_dir),
            project_id=project_id,
            environment_id=environment_id,
            issue_id=issue_id,
            app_url=app_url,
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message.lower() else 400
        return {"ok": False, "error": message}, status
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 400
    return (
        {
            "ok": True,
            "spec": generated.spec.__dict__,
            "issue_public_id": generated.issue_public_id,
            "replay_public_id": generated.replay_public_id,
            "confidence": generated.confidence,
            "known_gaps": generated.known_gaps,
        },
        200,
    )


def _issue_has_replay_regression_link(store: Storage, issue: Any) -> bool:
    failure_id = str(issue["canonical_failure_id"] or "")
    if not failure_id:
        return False
    return any(
        link.source == "replay_issue"
        for link in store.list_failure_test_links(failure_id=failure_id)
    )


def _generate_replay_issue_specs_payload(
    *,
    store: Storage,
    data_dir: Path,
    project_id: str,
    environment_id: str,
    issue_ids: list[str] | None = None,
    status: str = "",
    app_url: str = "",
    limit: int = 25,
    missing_only: bool = True,
) -> tuple[dict[str, Any], int]:
    try:
        limit_v = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit_v = 25
    clean_issue_ids = [
        str(item).strip()
        for item in (issue_ids or [])
        if str(item or "").strip()
    ][:limit_v]
    selected: list[Any] = []
    seen: set[str] = set()
    if clean_issue_ids:
        for issue_id in clean_issue_ids:
            row = store.get_replay_issue(
                project_id=project_id,
                environment_id=environment_id,
                issue_id=issue_id,
            )
            if row is None:
                continue
            public_id = str(row["public_id"])
            if public_id in seen:
                continue
            seen.add(public_id)
            selected.append(row)
    else:
        selected = store.list_replay_issues(
            project_id=project_id,
            environment_id=environment_id,
            status=status or None,
        )[:limit_v]

    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for issue in selected:
        public_id = str(issue["public_id"])
        issue_status = str(issue["status"] or "")
        if issue_status == "ignored":
            skipped.append({"issue_public_id": public_id, "reason": "ignored"})
            continue
        if missing_only and _issue_has_replay_regression_link(store, issue):
            skipped.append(
                {"issue_public_id": public_id, "reason": "already_covered"}
            )
            continue
        try:
            generated = generate_spec_from_replay_issue(
                store=store,
                specs_dir=specs_dir_for_data_dir(data_dir),
                project_id=project_id,
                environment_id=environment_id,
                issue_id=public_id,
                app_url=app_url,
            )
        except Exception as exc:
            failed.append({"issue_public_id": public_id, "error": str(exc)})
            continue
        results.append(
            {
                "issue_public_id": generated.issue_public_id,
                "replay_public_id": generated.replay_public_id,
                "spec_id": generated.spec.spec_id,
                "spec_name": generated.spec.name,
                "confidence": generated.confidence,
                "known_gaps": generated.known_gaps,
            }
        )
    ok = not failed
    return (
        {
            "ok": ok,
            "requested": len(clean_issue_ids) if clean_issue_ids else len(selected),
            "considered": len(selected),
            "generated": len(results),
            "skipped": skipped,
            "failed": failed,
            "results": results,
        },
        200 if ok else 207,
    )


def _generate_replay_issue_api_spec_payload(
    *,
    store: Storage,
    data_dir: Path,
    issue_id: str,
    project_id: str,
    environment_id: str,
    app_url: str = "",
) -> tuple[dict[str, Any], int]:
    try:
        generated = generate_api_spec_from_replay_issue(
            store=store,
            specs_dir=api_specs_dir_for_data_dir(data_dir),
            project_id=project_id,
            environment_id=environment_id,
            issue_id=issue_id,
            app_url=app_url,
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message.lower() else 400
        return {"ok": False, "error": message}, status
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 400
    return (
        {
            "ok": True,
            "spec": generated.spec.__dict__,
            "issue_public_id": generated.issue_public_id,
            "replay_public_id": generated.replay_public_id,
            "source_signal": generated.source_signal,
        },
        200,
    )


def _run_replay_issue_api_spec_payload(
    *,
    store: Storage,
    data_dir: Path,
    spec_id: str,
) -> tuple[dict[str, Any], int]:
    try:
        spec = load_api_spec(api_specs_dir_for_data_dir(data_dir), spec_id)
        result = run_api_spec(
            spec=spec,
            runs_dir=api_runs_dir_for_data_dir(data_dir),
        )
        links = store.list_failure_test_links(spec_id=result.spec_id, limit=10)
        if not links:
            failure_id = str(spec.fixtures.get("canonical_failure_id") or "")
            issue_id = str(spec.fixtures.get("issue_id") or "")
            issue_public_id = str(spec.fixtures.get("issue_public_id") or "")
            if failure_id:
                link_id = store.upsert_failure_test_link(
                    failure_id=failure_id,
                    issue_id=issue_id,
                    issue_public_id=issue_public_id,
                    spec_id=spec.spec_id,
                    spec_name=spec.name,
                    spec_path=str(api_specs_dir_for_data_dir(data_dir) / f"{spec.spec_id}.json"),
                    source=str(spec.fixtures.get("source") or "replay_issue_api"),
                )
                links = store.list_failure_test_links(
                    spec_id=result.spec_id,
                    limit=10,
                )
                links = [link for link in links if link.id == link_id] or links
        updated = []
        for link in links:
            updated.extend(
                store.update_failure_test_link_run(
                    spec_id=result.spec_id,
                    run_result=result,
                    link_id=link.id,
                )
            )
    except FileNotFoundError:
        return {"ok": False, "error": f"API spec not found: {spec_id}"}, 404
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 400
    return (
        {
            "ok": result.ok,
            "result": result.__dict__,
            "updated_links": [_failure_test_link_payload(link) for link in updated],
        },
        200 if result.ok else 400,
    )


def _select_repo(
    *, store: Storage, repo_full_name: str = ""
) -> tuple[GitHubRepoRow | None, str]:
    repos = store.list_github_repos()
    requested = repo_full_name.strip()
    if requested:
        repo = store.get_github_repo(requested)
        if repo is None:
            return None, (
                f"Repo not connected: {requested}. "
                "Run `retrace github connect --repo org/name` first."
            )
        return repo, ""
    if not repos:
        return None, "No connected repo. Run `retrace github connect --repo org/name` first."
    return repos[0], ""


def _generate_replay_issue_fix_prompts_payload(
    *,
    store: Storage,
    output_dir: Path,
    issue_id: str,
    project_id: str,
    environment_id: str,
    repo_full_name: str = "",
) -> tuple[dict[str, Any], int]:
    repo, error = _select_repo(store=store, repo_full_name=repo_full_name)
    if repo is None:
        return {"ok": False, "error": error}, 400

    try:
        issue = store.get_replay_issue(
            project_id=project_id,
            environment_id=environment_id,
            issue_id=issue_id,
        )
        if issue is None:
            return {"ok": False, "error": f"Replay issue not found: {issue_id}"}, 404
        if str(issue["status"] or "") == "ignored":
            return {"ok": False, "error": f"Replay issue is ignored: {issue_id}"}, 409
        finding = parsed_finding_from_replay_issue(issue)
        repo_path = Path(repo.local_path) if repo.local_path else None
        result = generate_fix_suggestions(
            store=store,
            repo=repo,
            repo_path=repo_path,
            out_dir=output_dir / "fix-prompts",
            report_key=replay_issue_report_key(str(issue["public_id"])),
            source_label=f"replay issue {issue['public_id']}",
            artifact_stem=f"replay-{slugify(str(issue['public_id']))}",
            findings=[finding],
            project_id=project_id,
            environment_id=environment_id,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 400

    artifact = result.artifacts[0] if result.artifacts else None
    return (
        {
            "ok": True,
            "issue_public_id": str(issue["public_id"]),
            "repo": result.repo_full_name,
            "repo_path": result.repo_path,
            "out_dir": str(result.out_dir),
            "stored": result.stored,
            "generated": result.generated,
            "regression_counts": result.regression_counts,
            "finding_hash": artifact.finding_hash if artifact else "",
            "candidates": [
                {
                    "file_path": c.file_path,
                    "symbol": c.symbol,
                    "score": c.score,
                    "rationale": c.rationale,
                }
                for c in (artifact.candidates if artifact else [])
            ],
            "prompts": artifact.prompts if artifact else {},
            "prompt_files": artifact.prompt_files if artifact else {},
            "artifact_json": artifact.artifact_json if artifact else "",
            "artifact_manifest_json": artifact.artifact_manifest_json if artifact else "",
            "repair_task_id": artifact.repair_task_id if artifact else "",
        },
        200,
    )


def _transition_replay_issue_payload(
    *,
    store: Storage,
    issue_id: str,
    project_id: str,
    environment_id: str,
    status: str,
) -> tuple[dict[str, Any], int]:
    if status not in {"resolved", "unresolved", "ignored"}:
        return {"ok": False, "error": "status must be resolved, unresolved, or ignored"}, 400
    issue = store.get_replay_issue(
        project_id=project_id,
        environment_id=environment_id,
        issue_id=issue_id,
    )
    if issue is None:
        return {"ok": False, "error": f"Replay issue not found: {issue_id}"}, 404
    try:
        updated = store.transition_replay_issue(str(issue["id"]), status=status)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400
    refreshed = store.get_replay_issue(
        project_id=project_id,
        environment_id=environment_id,
        issue_id=issue_id,
    )
    return (
        {
            "ok": True,
            "updated": updated,
            "issue": _replay_issue_payload(
                refreshed if refreshed is not None else issue,
                sessions=[],
            ),
        },
        200,
    )


def _verify_resolved_issues_payload(
    *,
    store: Storage,
    data_dir: Path,
    cwd: Path,
    project_id: str,
    environment_id: str,
    limit: int = 10,
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    try:
        limit_v = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit_v = 10
    specs_by_id: dict[str, Any] = {}
    specs_by_issue: dict[str, Any] = {}
    for spec in list_specs(specs_dir_for_data_dir(data_dir)):
        specs_by_id[spec.spec_id] = spec
        public_id = str(spec.fixtures.get("issue_public_id") or "").strip()
        if not public_id:
            continue
        existing = specs_by_issue.get(public_id)
        if existing is None or spec.updated_at > existing.updated_at:
            specs_by_issue[public_id] = spec
    api_specs_by_id: dict[str, Any] = {}
    api_specs_by_issue: dict[str, Any] = {}
    for spec_path in api_specs_dir_for_data_dir(data_dir).glob("*.json"):
        try:
            spec = load_api_spec(api_specs_dir_for_data_dir(data_dir), spec_path.stem)
        except Exception:
            continue
        api_specs_by_id[spec.spec_id] = spec
        public_id = str(spec.fixtures.get("issue_public_id") or "").strip()
        if not public_id:
            continue
        existing = api_specs_by_issue.get(public_id)
        if existing is None or spec.updated_at > existing.updated_at:
            api_specs_by_issue[public_id] = spec

    resolved = store.list_replay_issues(
        project_id=project_id,
        environment_id=environment_id,
        status="resolved",
    )
    plan: list[dict[str, Any]] = []
    for row in resolved[:limit_v]:
        public_id = str(row["public_id"])
        failure_id = str(row["canonical_failure_id"] or "")
        planned_tests: list[dict[str, str]] = []
        if failure_id:
            for link in store.list_failure_test_links(failure_id=failure_id):
                linked_spec = specs_by_id.get(link.spec_id)
                if linked_spec is not None:
                    planned_tests.append(
                        {
                            "kind": "ui",
                            "spec_id": linked_spec.spec_id,
                            "coverage_link_id": link.id,
                        }
                    )
                    continue
                linked_api_spec = api_specs_by_id.get(link.spec_id)
                if linked_api_spec is not None:
                    planned_tests.append(
                        {
                            "kind": "api",
                            "spec_id": linked_api_spec.spec_id,
                            "coverage_link_id": link.id,
                        }
                    )
        if not planned_tests:
            spec = specs_by_issue.get(public_id)
            if spec is not None:
                planned_tests.append(
                    {"kind": "ui", "spec_id": spec.spec_id, "coverage_link_id": ""}
                )
        if not planned_tests:
            api_spec = api_specs_by_issue.get(public_id)
            if api_spec is not None:
                planned_tests.append(
                    {"kind": "api", "spec_id": api_spec.spec_id, "coverage_link_id": ""}
                )
        plan.append(
            {
                "public_id": public_id,
                "issue_id": str(row["id"]),
                "failure_id": failure_id,
                "title": str(row["title"] or "Replay issue"),
                "spec_id": planned_tests[0]["spec_id"] if planned_tests else "",
                "spec_kind": planned_tests[0]["kind"] if planned_tests else "",
                "coverage_link_id": (
                    planned_tests[0]["coverage_link_id"] if planned_tests else ""
                ),
                "tests": planned_tests,
                "has_spec": bool(planned_tests),
            }
        )

    if dry_run:
        return {"ok": True, "plan": plan, "verified": [], "regressed": []}, 200

    verified: list[str] = []
    regressed: list[dict[str, Any]] = []
    for entry in plan:
        tests = entry.get("tests") if isinstance(entry.get("tests"), list) else []
        if not tests:
            continue
        failures: list[dict[str, str]] = []
        for test in tests:
            spec_id = str(test.get("spec_id") or "")
            kind = str(test.get("kind") or "ui")
            coverage_link_id = str(test.get("coverage_link_id") or "")
            try:
                if kind == "api":
                    api_spec = api_specs_by_id.get(spec_id)
                    if api_spec is None:
                        raise FileNotFoundError(f"API spec not found: {spec_id}")
                    result = run_api_spec(
                        spec=api_spec,
                        runs_dir=api_runs_dir_for_data_dir(data_dir),
                    )
                else:
                    spec = specs_by_id.get(spec_id) or specs_by_issue[entry["public_id"]]
                    result = run_spec(
                        spec=spec,
                        runs_dir=runs_dir_for_data_dir(data_dir),
                        cwd=cwd,
                    )
            except Exception as exc:
                failures.append(
                    {"spec_id": spec_id, "kind": kind, "error": f"run raised: {exc}"}
                )
                continue
            if coverage_link_id:
                try:
                    store.update_failure_test_link_run(
                        spec_id=result.spec_id,
                        run_result=result,
                        link_id=coverage_link_id,
                    )
                except Exception:
                    logger.warning(
                        "failed to persist failure_test_link run metadata",
                        extra={"spec_id": result.spec_id, "run_id": result.run_id},
                        exc_info=True,
                    )
            if not result.ok:
                failures.append(
                    {
                        "spec_id": result.spec_id,
                        "kind": kind,
                        "run_id": result.run_id,
                        "exit_code": str(getattr(result, "exit_code", "")),
                        "error": result.error,
                    }
                )
        if not failures:
            store.transition_replay_issue(entry["issue_id"], status="verified")
            verified.append(entry["public_id"])
            continue
        store.transition_replay_issue(entry["issue_id"], status="regressed")
        regressed.append(
            {
                "public_id": entry["public_id"],
                "issue_id": entry["issue_id"],
                "spec_id": failures[0].get("spec_id", ""),
                "run_id": failures[0].get("run_id", ""),
                "error": failures[0].get("error", ""),
                "failures": failures,
            }
        )
    return {"ok": True, "plan": plan, "verified": verified, "regressed": regressed}, 200


def _github_repos_payload(store: Storage) -> dict[str, Any]:
    return {
        "repos": [
            {
                "repo_full_name": repo.repo_full_name,
                "default_branch": repo.default_branch,
                "remote_url": repo.remote_url,
                "local_path": repo.local_path,
                "provider": repo.provider,
                "connected_at": repo.connected_at.isoformat(),
            }
            for repo in store.list_github_repos()
        ]
    }


def _api_suites_payload(data_dir: Path) -> dict[str, Any]:
    suites = []
    for suite in list_api_suites(api_suites_dir_for_data_dir(data_dir)):
        warnings = suite.import_summary.get("quality_warnings")
        if not isinstance(warnings, dict):
            warnings = {}
        warning_count = sum(
            len(value) for value in warnings.values() if isinstance(value, list)
        )
        suites.append(
            {
                "suite_id": suite.suite_id,
                "name": suite.name,
                "source": suite.source,
                "spec_count": len(suite.spec_ids),
                "spec_ids": suite.spec_ids,
                "auth_profile": suite.auth_profile,
                "env_profile": suite.env_profile,
                "filters": suite.filters,
                "import_summary": suite.import_summary,
                "operation_count": len(suite.operations),
                "operations": suite.operations[:25],
                "skipped_count": len(suite.skipped),
                "skipped": suite.skipped[:25],
                "quality_warning_count": warning_count,
                "metadata": suite.metadata,
                "created_at": suite.created_at,
                "updated_at": suite.updated_at,
            }
        )
    return {"suites": suites}


def _json_object_list_payload(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be a JSON list of objects")
    return [dict(item) for item in value]


def _edit_ui_draft_payload(
    *,
    data_dir: Path,
    spec_id: str,
    name: str = "",
    prompt: str = "",
    app_url: str = "",
    steps: Any = None,
    assertions: Any = None,
    review_note: str = "",
    accept: bool = False,
) -> tuple[dict[str, Any], int]:
    clean_spec_id = spec_id.strip()
    if not clean_spec_id:
        return {"ok": False, "error": "spec_id is required"}, 400
    specs_dir = specs_dir_for_data_dir(data_dir)
    try:
        spec = load_spec(specs_dir, clean_spec_id)
    except Exception:
        return {"ok": False, "error": f"spec not found: {clean_spec_id}"}, 404
    if dict(spec.fixtures or {}).get("draft_status") != "draft":
        return {"ok": False, "error": "Spec is not an unaccepted draft."}, 409

    changed_fields: list[str] = []
    edited_name = name.strip()
    if edited_name and edited_name != spec.name:
        spec.name = edited_name
        changed_fields.append("name")
    edited_prompt = prompt.strip()
    if edited_prompt and edited_prompt != spec.prompt:
        spec.prompt = edited_prompt
        changed_fields.append("prompt")
    edited_app_url = app_url.strip()
    if edited_app_url and edited_app_url != spec.app_url:
        spec.app_url = edited_app_url
        changed_fields.append("app_url")
    try:
        if steps is not None:
            spec.exact_steps = _json_object_list_payload(steps, label="steps")
            changed_fields.append("exact_steps")
        if assertions is not None:
            spec.assertions = _json_object_list_payload(assertions, label="assertions")
            changed_fields.append("assertions")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400

    spec.fixtures = dict(spec.fixtures or {})
    notes = [
        str(item).strip()
        for item in list(spec.fixtures.get("review_notes", []) or [])
        if str(item).strip()
    ]
    clean_note = review_note.strip()
    if clean_note:
        notes.append(clean_note)
        spec.fixtures["review_notes"] = notes
        changed_fields.append("review_notes")
    spec.fixtures["reviewed_at"] = now_iso()
    if accept:
        spec.fixtures["draft_status"] = "accepted"
        spec.fixtures.setdefault("accepted_at", now_iso())
        changed_fields.append("draft_status")
    if changed_fields:
        spec.fixtures["last_review_edit"] = {
            "edited_at": now_iso(),
            "fields": sorted(set(changed_fields)),
        }
    spec.updated_at = now_iso()
    try:
        validate_spec(spec)
        save_spec(specs_dir, spec)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 400
    return {
        "ok": True,
        "spec": spec.__dict__,
        "draft_status": spec.fixtures.get("draft_status", ""),
        "accepted": bool(accept),
        "changed_fields": sorted(set(changed_fields)),
        "step_count": len(spec.exact_steps or []),
        "assertion_count": len(spec.assertions or []),
        "review_notes": spec.fixtures.get("review_notes", []),
    }, 200


def _connect_github_repo_payload(
    *,
    store: Storage,
    repo_full_name: str,
    default_branch: str = "main",
    local_path: str = "",
) -> tuple[dict[str, Any], int]:
    repo = repo_full_name.strip()
    branch = default_branch.strip() or "main"
    path_value = local_path.strip()
    provider = "github"
    remote_url = ""
    if path_value:
        repo_path = Path(path_value).expanduser()
        if not repo_path.exists() or not repo_path.is_dir():
            return {
                "ok": False,
                "error": f"Local path is not a directory: {path_value}",
            }, 400
        path_value = str(repo_path)
        if not repo:
            repo = f"local/{slugify(repo_path.name or 'codebase')}"
            provider = "local"
    if not repo:
        return {
            "ok": False,
            "error": "Enter an owner/name repo or a local checkout path.",
        }, 400
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return {"ok": False, "error": "Repo must use owner/name format."}, 400
    if provider == "github":
        remote_url = f"https://github.com/{repo}.git"
    store.upsert_github_repo(
        repo_full_name=repo,
        default_branch=branch,
        remote_url=remote_url,
        local_path=path_value,
        provider=provider,
    )
    return {"ok": True, **_github_repos_payload(store)}, 200


def _create_sdk_key_payload(
    *,
    store: Storage,
    project_name: str = "Default",
    environment_name: str = "production",
    name: str = "Browser SDK",
) -> tuple[dict[str, Any], int]:
    project = project_name.strip() or "Default"
    environment = environment_name.strip() or "production"
    key_name = name.strip() or "Browser SDK"
    try:
        workspace = store.ensure_workspace(
            project_name=project,
            environment_name=environment,
        )
        created = create_sdk_key(
            store,
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
            name=key_name,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 400
    return (
        {
            "ok": True,
            "id": created.id,
            "project_id": workspace.project_id,
            "environment_id": workspace.environment_id,
            "project": project,
            "environment": environment,
            "name": key_name,
            "key": created.key,
            "prefix": created.prefix,
            "last4": created.last4,
            "ingest_path": "/api/sdk/replay",
            "ingest_url": "http://127.0.0.1:8788/api/sdk/replay",
            "sentry_dsn": build_sentry_dsn(
                public_key=created.key,
                base_url="http://127.0.0.1:8788",
                project_id=workspace.project_id,
            ),
        },
        200,
    )


_INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Retrace UI</title>
  <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/rrweb-player@latest/dist/style.css\" />
  <style>
    :root { --bg:#0f172a; --panel:#111827; --panel2:#0b1220; --line:#1f2937; --text:#e5e7eb; --muted:#9ca3af; --acc:#22d3ee; }
    body { margin:0; font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto; background:var(--bg); color:var(--text); }
    .app-shell { display:grid; grid-template-columns: 212px minmax(320px, 380px) minmax(0, 1fr); height:100vh; }
    .nav { border-right:1px solid var(--line); background:#08111f; padding:14px 12px; overflow:auto; }
    .brand { font-size:15px; font-weight:700; margin-bottom:14px; }
    .nav-btn { display:block; width:100%; text-align:left; margin:4px 0; background:transparent; color:var(--text); border:1px solid transparent; border-radius:8px; padding:9px 10px; cursor:pointer; font-size:13px; }
    .nav-btn:hover { background:#111a2b; }
    .nav-btn.active { background:#162033; border-color:#244158; color:#cffafe; }
    .rail { border-right:1px solid var(--line); overflow:auto; background:var(--panel2); }
    .main { overflow:auto; padding:16px; }
    .hdr { padding:12px 14px; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--panel2); z-index:2; }
    .view { display:none; }
    .view.active { display:block; }
    .finding { padding:10px 12px; border-bottom:1px solid #182235; cursor:pointer; }
    .finding:hover { background:#111a2b; }
    .finding.active { background:#162033; border-left:3px solid var(--acc); }
    .issue-row { padding:10px 12px; border-bottom:1px solid #182235; cursor:pointer; }
    .issue-row:hover { background:#111a2b; }
    .issue-row:focus { outline:2px solid var(--acc); outline-offset:-2px; }
    .issue-row.active { background:#162033; border-left:3px solid var(--acc); }
    .sev { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing: .08em; }
    .title { font-size:14px; line-height:1.35; margin-top:4px; }
    .view-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-bottom:12px; }
    .view-head h2 { margin:0; font-size:19px; letter-spacing:0; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
    .metric-grid { display:grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap:12px; margin-bottom:14px; }
    .metric { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
    .metric strong { display:block; font-size:24px; margin-bottom:4px; }
    .detail-grid { display:grid; grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr); gap:14px; align-items:start; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:14px; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
    .card h3 { margin:0 0 8px 0; font-size:13px; color:#93c5fd; text-transform:uppercase; letter-spacing:.08em; }
    .lbl { font-size:12px; color:var(--muted); margin-top:8px; }
    input { width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px; }
    textarea { width:100%; min-height:120px; resize:vertical; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
    ul { margin:0; padding-left:18px; }
    li { margin: 6px 0; font-size:13px; }
    pre { white-space:pre-wrap; font-size:12px; background:#0b1220; border:1px solid #1f2937; padding:10px; border-radius:8px; max-height:360px; overflow:auto; }
    .meta a { color:#67e8f9; text-decoration:none; }
    .meta a:hover { text-decoration:underline; }
    .rr { background:#0b1220; border:1px solid #1f2937; border-radius:10px; padding:8px; }
    .empty { color:var(--muted); font-size:13px; }
    .btn { background:#0b1220; color:#e5e7eb; border:1px solid #374151; border-radius:8px; padding:6px 8px; cursor:pointer; font-size:12px; }
    .timeline { border:1px solid #1f2937; border-radius:8px; overflow:hidden; }
    .timeline-row { display:grid; grid-template-columns:88px 130px 1fr; gap:10px; padding:9px 10px; border-top:1px solid #1f2937; font-size:13px; }
    .timeline-row:first-child { border-top:0; }
    .timeline-row.detector { background:#172033; border-left:3px solid #f59e0b; }
    .timeline-kind { color:#93c5fd; text-transform:uppercase; font-size:11px; letter-spacing:.08em; }
    .timeline-summary { color:var(--muted); margin-top:2px; overflow-wrap:anywhere; }
    .workflow-strip { display:grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap:8px; margin:10px 0 12px 0; }
    .workflow-step { border:1px solid #26364f; border-radius:8px; padding:9px; background:#0b1220; min-height:58px; }
    .workflow-step.complete { border-color:#14532d; background:#0d1f19; }
    .workflow-step.current { border-color:#0e7490; background:#102235; }
    .workflow-step.blocked { border-color:#4b5563; color:#9ca3af; }
    .workflow-step strong { display:block; font-size:12px; margin-bottom:3px; }
    .workflow-step span { display:block; font-size:12px; color:var(--muted); }
    .workflow-action { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
    .readiness-panel { border:1px solid #26364f; border-radius:8px; padding:10px; background:#0b1220; margin:10px 0 12px 0; }
    .readiness-panel .row { display:flex; justify-content:space-between; gap:10px; align-items:center; }
    .recommendation-list { margin-top:8px; }
    .recommendation-list button { margin-right:6px; margin-top:4px; }
    .suite-row { border-top:1px solid #1f2937; padding:10px 0; }
    .suite-row:first-child { border-top:0; padding-top:0; }
    .draft-editor { margin-top:12px; }
    .draft-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .ok { color:#86efac; } .bad { color:#fca5a5; }
    @media (max-width: 980px) {
      .app-shell { grid-template-columns: 1fr; height:auto; min-height:100vh; }
      .nav { position:sticky; top:0; z-index:3; border-right:0; border-bottom:1px solid var(--line); }
      .nav-btn { display:inline-block; width:auto; margin-right:4px; }
      .rail { border-right:0; border-bottom:1px solid var(--line); max-height:42vh; }
      .main { padding:12px; }
      .metric-grid, .detail-grid, .grid, .workflow-strip, .draft-grid { grid-template-columns: 1fr; }
      .timeline-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class=\"app-shell\">
    <nav class=\"nav\">
      <div class=\"brand\">Retrace QA</div>
      <button class=\"nav-btn active\" type=\"button\" data-view=\"dashboard\">Dashboard</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"issues\">Issues</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"replays\">Replays</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"findings\">Findings</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"tests\">Tests</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"runs\">Runs</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"settings\">Settings</button>
    </nav>
    <aside class=\"rail\">
      <div class=\"hdr\"><strong id=\"railTitle\">Issues</strong><div class=\"empty\" id=\"reportMeta\"></div></div>
      <div id=\"issueWorkflowList\"></div>
      <div id=\"findings\" style=\"display:none\"></div>
    </aside>
    <main class=\"main\" id=\"detail\">
      <section class=\"view active\" id=\"view-dashboard\"><div id=\"dashboardView\"></div></section>
      <section class=\"view\" id=\"view-issues\">
        <div class=\"view-head\">
          <div><h2>Issue Detail</h2><div class=\"empty\">Replay-backed failures are the primary workflow surface.</div></div>
          <div class=\"actions\">
            <button class=\"btn\" id=\"importPostHogReplaysBtn\" type=\"button\">Import PostHog Replays</button>
            <button class=\"btn\" id=\"processReplayJobsBtn\" type=\"button\">Process Queued Replays</button>
            <button class=\"btn\" id=\"verifyResolvedBtn\" type=\"button\">Verify Resolved Issues</button>
          </div>
        </div>
        <label class=\"empty\"><input id=\"replayAiAnalysis\" type=\"checkbox\" /> AI replay analysis</label>
        <div class=\"empty\" id=\"replayProcessStatus\"></div>
        <div class=\"empty\" id=\"verifyResolvedStatus\"></div>
        <div id=\"replayIssueDetail\"><div class=\"empty\">Select a replay-backed issue.</div></div>
      </section>
      <section class=\"view\" id=\"view-replays\">
        <div class=\"view-head\"><div><h2>Replays</h2><div class=\"empty\">Recent captured sessions and playback.</div></div></div>
        <div id=\"replaySessionsPanel\"></div>
        <div style=\"height:10px\"></div>
        <div class=\"rr\"><div id=\"firstPartyReplay\"><div class=\"empty\">Select a first-party replay session.</div></div></div>
      </section>
      <section class=\"view\" id=\"view-tests\"><div id=\"tester\"></div></section>
      <section class=\"view\" id=\"view-runs\"><div id=\"runsView\"></div></section>
      <section class=\"view\" id=\"view-settings\"><div class=\"card\" id=\"onboarding\"></div></section>
      <section class=\"view\" id=\"view-findings\"><div id=\"findingDetail\"><div class=\"empty\">Select a finding.</div></div></section>
      <div id=\"replayDashboard\" style=\"display:none\"></div>
    </main>
  </div>
  <script src=\"https://cdn.jsdelivr.net/npm/rrweb-player@latest/dist/index.js\"></script>
  <script>
    let findings = [];
    let active = null;
    let replayState = { issues: [], sessions: [], activeIssueId: null };
    const LLM_DEFAULTS = {
      openai_compatible: { base_url: 'http://localhost:8080/v1', model: 'llama-3.1-8b-instruct' },
      openai: { base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
      anthropic: { base_url: 'https://api.anthropic.com/v1', model: 'claude-3-5-sonnet-latest' },
      openrouter: { base_url: 'https://openrouter.ai/api/v1', model: 'openai/gpt-4o-mini' },
    };
    const CLOUD_PROVIDERS = new Set(['openai', 'anthropic', 'openrouter']);
    const CUSTOM_MODEL = '__custom__';

    function esc(s){ return String(s || \"\").replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c])); }
    function byId(id){ return document.getElementById(id); }

    function copyText(s){ navigator.clipboard.writeText(String(s || \"\")); }
    function copyPrompt(key){ if(active?.prompts?.[key]) copyText(active.prompts[key]); }
    function safeExternalUrl(raw){
      try {
        const url = new URL(String(raw || ''), window.location.origin);
        return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : '';
      } catch(_err) {
        return '';
      }
    }
    function safeHashUrl(raw, allowedPrefix){
      const value = String(raw || '');
      return value.startsWith(allowedPrefix) ? value : '';
    }

    function switchView(view){
      document.querySelectorAll('.view').forEach(el => el.classList.toggle('active', el.id === `view-${view}`));
      document.querySelectorAll('.nav-btn').forEach(el => el.classList.toggle('active', el.dataset.view === view));
      const title = byId('railTitle');
      if(title) title.textContent = view === 'findings' ? 'Report Findings' : 'Issues';
      if(byId('issueWorkflowList')) byId('issueWorkflowList').style.display = view === 'findings' ? 'none' : '';
      if(byId('findings')) byId('findings').style.display = view === 'findings' ? '' : 'none';
    }
    document.querySelectorAll('.nav-btn').forEach(el => el.addEventListener('click', () => switchView(el.dataset.view)));
    window.addEventListener('hashchange', () => applyReplayHash(replayState.issues, replayState.sessions));

    function statusClass(value){
      const v = String(value || '').toLowerCase();
      if(v.includes('pass') || v === 'resolved' || v === 'verified' || v === 'covered_passing') return 'ok';
      if(v.includes('fail') || v.includes('regressed') || v === 'unresolved' || v === 'covered_failing') return 'bad';
      return '';
    }

    function openReplayIssue(issueId){
      const issue = replayState.issues.find(i => i.public_id === issueId);
      if(!issue){ return; }
      const nextHash = `#issue=${encodeURIComponent(issue.public_id)}`;
      if(window.location.hash !== nextHash){
        window.location.hash = nextHash;
        return;
      }
      renderReplayIssueDetail(issue);
      switchView('issues');
    }

    function bindReplayIssueRows(root = document){
      root.querySelectorAll('[data-replay-issue]').forEach(el => {
        el.addEventListener('click', () => openReplayIssue(el.dataset.replayIssue));
        el.addEventListener('keydown', ev => {
          if(ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
          ev.preventDefault();
          openReplayIssue(el.dataset.replayIssue);
        });
      });
    }

    async function refreshTesterAndReplay(issueId = '', processStatus = ''){
      await Promise.all([loadTesterPanel(), loadReplayDashboard(processStatus)]);
      const targetId = issueId || replayState.activeIssueId;
      const refreshed = replayState.issues.find(i => i.public_id === targetId);
      if(refreshed) renderReplayIssueDetail(refreshed);
    }

    function llmKeyLabel(provider){
      if(provider === 'openai') return 'OpenAI API Key';
      if(provider === 'anthropic') return 'Anthropic API Key';
      if(provider === 'openrouter') return 'OpenRouter API Key';
      return 'LLM API Key (optional for local)';
    }

    function syncProviderUI(applyDefaults=false){
      const provider = byId('llmProvider').value || 'openai_compatible';
      const keyLbl = byId('llmKeyLabel');
      const keyReq = byId('llmKeyRequired');
      if(keyLbl) keyLbl.textContent = llmKeyLabel(provider);
      if(keyReq) keyReq.textContent = CLOUD_PROVIDERS.has(provider) ? 'required' : 'optional';
      if(applyDefaults){
        const d = LLM_DEFAULTS[provider] || LLM_DEFAULTS.openai_compatible;
        if(byId('llmBaseUrl')) byId('llmBaseUrl').value = d.base_url;
        if(byId('llmModel')) byId('llmModel').value = d.model;
      }
    }

    async function fetchModels(ev){
      ev.preventDefault();
      const provider = byId('llmProvider').value || 'openai_compatible';
      const body = {
        provider,
        base_url: byId('llmBaseUrl').value,
        api_key: byId('llmApiKey').value,
      };
      const status = byId('llmModelStatus');
      status.textContent = 'Loading models...';
      const res = await fetch('/api/llm/models', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const data = await res.json();
      if(!res.ok || !data.ok){
        status.textContent = data.error || 'Model discovery failed';
        return;
      }
      const models = data.models || [];
      const picker = byId('llmModelPicker');
      if(!models.length){
        status.textContent = 'No models returned. You can still type one manually.';
        picker.style.display = 'none';
        return;
      }
      status.textContent = `Loaded ${models.length} model(s).`;
      picker.innerHTML = models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('') + `<option value="${CUSTOM_MODEL}">Custom...</option>`;
      picker.style.display = 'block';
      const cur = byId('llmModel').value;
      const hasCur = models.includes(cur);
      picker.value = hasCur ? cur : models[0];
      byId('llmModel').value = hasCur ? cur : models[0];
    }

    function onModelPick(){
      const picker = byId('llmModelPicker');
      if(!picker) return;
      if(picker.value === CUSTOM_MODEL){
        return;
      }
      byId('llmModel').value = picker.value;
    }

    async function saveSettings(ev){
      ev.preventDefault();
      const body = {
        posthog_host: byId('phHost').value,
        posthog_project_id: byId('phProject').value,
        posthog_api_key: byId('phKey').value,
        llm_provider: byId('llmProvider').value,
        llm_base_url: byId('llmBaseUrl').value,
        llm_model: byId('llmModel').value,
        llm_api_key: byId('llmApiKey').value,
        tester_app_url: byId('testerAppUrl').value,
        tester_start_command: byId('testerStartCommand').value,
        tester_harness_command: byId('testerHarnessCommand').value,
        tester_max_retries: byId('testerMaxRetries').value,
        tester_auth_required: byId('testerAuthRequired').value,
        tester_auth_mode: byId('testerAuthMode').value,
        tester_auth_login_url: byId('testerAuthLoginUrl').value,
        tester_auth_username: byId('testerAuthUsername').value,
        tester_auth_password: byId('testerAuthPassword').value,
        tester_auth_jwt: byId('testerAuthJwt').value,
        tester_auth_headers: byId('testerAuthHeaders').value,
      };
      const res = await fetch('/api/settings', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const data = await res.json();
      if(!res.ok){ alert(data.error || 'Save failed'); return; }
      await loadOnboarding();
      await loadTesterPanel();
      await bootFindings();
    }

    async function connectGithubRepo(ev){
      ev.preventDefault();
      const status = byId('repoConnectStatus');
      if(status) status.textContent = 'Saving...';
      const res = await fetch('/api/github/repos', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          repo: byId('repoFullName').value,
          branch: byId('repoDefaultBranch').value,
          local_path: byId('repoLocalPath').value,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = data.error || 'Repo save failed';
        return;
      }
      if(status) status.textContent = 'Saved.';
      await loadOnboarding();
    }

    async function createSdkKey(ev){
      ev.preventDefault();
      const status = byId('sdkKeyStatus');
      const result = byId('sdkKeyResult');
      if(status) status.textContent = 'Creating...';
      if(result) result.innerHTML = '';
      const res = await fetch('/api/sdk-keys', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          project: byId('sdkProjectName').value,
          environment: byId('sdkEnvironmentName').value,
          name: byId('sdkKeyName').value,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = data.error || 'SDK key creation failed';
        return;
      }
      if(status) status.textContent = `Created ${data.id} ending in ${data.last4}.`;
      renderSdkKeyResult(data);
    }

    function renderSdkKeyResult(data){
      const root = byId('sdkKeyResult');
      if(!root){ return; }
      const ingestUrl = data.ingest_url || 'http://127.0.0.1:8788/api/sdk/replay';
      const installSnippet = 'npm install @retrace/browser';
      const initSnippet = `import { init } from "@retrace/browser";

const retrace = init({
  apiKey: "${data.key}",
  ingestUrl: "${ingestUrl}",
  privacy: {
    maskAllInputs: true,
    blockSelector: "[data-retrace-block]",
    maskTextSelector: "[data-retrace-mask]",
  },
});`;
      root.innerHTML = `
        <div class="lbl">Browser SDK Key (shown once)</div>
        <pre>${esc(data.key)}</pre>
        <button class="btn" id="copySdkKeyBtn" type="button">Copy Key</button>
        <div class="lbl">Install</div>
        <pre>${esc(installSnippet)}</pre>
        <button class="btn" id="copySdkInstallBtn" type="button">Copy Install</button>
        <div class="lbl">Initialize Capture</div>
        <pre>${esc(initSnippet)}</pre>
        <button class="btn" id="copySdkInitBtn" type="button">Copy Init</button>
        <button class="btn" id="sendSdkSmokeReplayBtn" type="button">Send Test Replay</button>
        <span class="empty" id="sdkSmokeReplayStatus"></span>
        <div class="empty" style="margin-top:8px">Project: <code>${esc(data.project_id)}</code> · Environment: <code>${esc(data.environment_id)}</code></div>
      `;
      byId('copySdkKeyBtn')?.addEventListener('click', () => copyText(data.key));
      byId('copySdkInstallBtn')?.addEventListener('click', () => copyText(installSnippet));
      byId('copySdkInitBtn')?.addEventListener('click', () => copyText(initSnippet));
      byId('sendSdkSmokeReplayBtn')?.addEventListener('click', () => sendSdkSmokeReplay(data.key, ingestUrl));
    }

    async function sendSdkSmokeReplay(apiKey, ingestUrl){
      const status = byId('sdkSmokeReplayStatus');
      if(status) status.textContent = 'Sending...';
      const now = Date.now();
      const sessionId = `ui-smoke-${now}-${Math.random().toString(36).slice(2)}`;
      const payload = {
        sessionId,
        sequence: 0,
        flushType: 'final',
        distinctId: 'retrace-ui-smoke',
        metadata: {
          source: 'retrace-ui',
          smoke_test: true,
        },
        events: [
          {
            type: 4,
            timestamp: now,
            data: { href: window.location.href },
          },
          {
            type: 6,
            timestamp: now + 1,
            data: {
              plugin: 'retrace/console@1',
              payload: {
                level: 'error',
                payload: ['Retrace UI smoke replay'],
                url: window.location.href,
              },
            },
          },
        ],
      };
      try {
        const res = await fetch(ingestUrl, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Retrace-Key': apiKey,
          },
          body: JSON.stringify(payload),
        });
        let data = {};
        try {
          data = await res.json();
        } catch(_err) {
          data = {};
        }
        if(!res.ok || data.accepted !== true){
          if(status) status.textContent = `Failed: ${data.error || data.message || 'ingest rejected'}`;
          return;
        }
        if(status) status.textContent = `Accepted ${data.event_count || payload.events.length} event(s) for ${sessionId}.`;
        await loadReplayDashboard('Test replay accepted. Process queued replays to create a replay-backed issue.');
      } catch(err) {
        if(status) status.textContent = `Failed: ${err?.message || err}`;
      }
    }

    async function loadOnboarding(){
      const [sRes, cRes, rRes] = await Promise.all([
        fetch('/api/settings'),
        fetch('/api/system-checks'),
        fetch('/api/github/repos'),
      ]);
      const settings = await sRes.json();
      const checks = await cRes.json();
      const repoData = await rRes.json();
      const repos = repoData.repos || [];
      const gh = checks.gh || {};
      const ph = checks.posthog || {};
      const llm = checks.llm || {};
      const replayApi = checks.replay_api || {};
      const llmProvider = settings.llm_provider || 'openai_compatible';
      const llmProviderLabel = llmProvider === 'openai' ? 'OpenAI'
        : llmProvider === 'anthropic' ? 'Anthropic'
        : llmProvider === 'openrouter' ? 'OpenRouter'
        : 'OpenAI-compatible';
      const repoRows = repos.map(r => `
        <li><code>${esc(r.repo_full_name)}</code> · provider=<code>${esc(r.provider || 'github')}</code> · branch=<code>${esc(r.default_branch || 'main')}</code>${r.local_path ? ` · path=<code>${esc(r.local_path)}</code>` : ''}</li>
      `).join('');
      byId('onboarding').innerHTML = `
        <h3>Onboarding & Settings</h3>
        <form id=\"settingsForm\">
          <div class=\"lbl\">PostHog Host</div>
          <input id=\"phHost\" value=\"${esc(settings.posthog_host)}\" />
          <div class=\"lbl\">PostHog Project ID</div>
          <input id=\"phProject\" value=\"${esc(settings.posthog_project_id)}\" />
          <div class=\"lbl\">PostHog Personal API Key</div>
          <input id=\"phKey\" value=\"\" placeholder=\"${settings.posthog_api_key_present ? 'Configured (leave blank to keep current)' : 'Enter PostHog key (phx_...)'}\" />
          <div class=\"lbl\">LLM Provider</div>
          <select id=\"llmProvider\" style=\"width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;\">
            <option value=\"openai_compatible\" ${llmProvider === 'openai_compatible' ? 'selected' : ''}>OpenAI-compatible (local/custom)</option>
            <option value=\"openai\" ${llmProvider === 'openai' ? 'selected' : ''}>OpenAI API</option>
            <option value=\"anthropic\" ${llmProvider === 'anthropic' ? 'selected' : ''}>Anthropic API</option>
            <option value=\"openrouter\" ${llmProvider === 'openrouter' ? 'selected' : ''}>OpenRouter API</option>
          </select>
          <div class=\"lbl\">LLM Base URL</div>
          <input id=\"llmBaseUrl\" value=\"${esc(settings.llm_base_url)}\" />
          <div class=\"lbl\">LLM Model</div>
          <input id=\"llmModel\" value=\"${esc(settings.llm_model)}\" />
          <div style=\"margin-top:6px\"><button class=\"btn\" type=\"button\" id=\"fetchModelsBtn\">Fetch Models</button> <span class=\"empty\" id=\"llmModelStatus\"></span></div>
          <select id=\"llmModelPicker\" style=\"display:none; margin-top:8px; width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;\"></select>
          <div class=\"lbl\" id=\"llmKeyLabel\">LLM API Key</div>
          <div class=\"empty\">Key: <span id=\"llmKeyRequired\">optional</span></div>
          <input id=\"llmApiKey\" value=\"\" placeholder=\"${settings.llm_api_key_present ? 'Configured (leave blank to keep current)' : 'Enter LLM API key'}\" />
          <div class=\"lbl\">Tester App URL</div>
          <input id=\"testerAppUrl\" value=\"${esc(settings.tester_app_url || 'http://127.0.0.1:3000')}\" />
          <div class=\"lbl\">Tester Start Command</div>
          <input id=\"testerStartCommand\" value=\"${esc(settings.tester_start_command || '')}\" placeholder=\"npm run dev\" />
          <div class=\"lbl\">Tester Harness Command Template</div>
          <input id=\"testerHarnessCommand\" value=\"${esc(settings.tester_harness_command || 'browser-harness run --url {app_url} --task {prompt_q} --output {run_dir_q}')}\" />
          <div class=\"lbl\">Tester Retry Count</div>
          <input id=\"testerMaxRetries\" type=\"number\" min=\"0\" value=\"${esc(settings.tester_max_retries || 1)}\" />
          <div class=\"lbl\">Tester Auth Required?</div>
          <select id=\"testerAuthRequired\" style=\"width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;\">
            <option value=\"false\" ${settings.tester_auth_required ? '' : 'selected'}>No</option>
            <option value=\"true\" ${settings.tester_auth_required ? 'selected' : ''}>Yes</option>
          </select>
          <div class=\"lbl\">Tester Auth Mode</div>
          <select id=\"testerAuthMode\" style=\"width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;\">
            <option value=\"none\" ${settings.tester_auth_mode === 'none' ? 'selected' : ''}>None</option>
            <option value=\"form\" ${settings.tester_auth_mode === 'form' ? 'selected' : ''}>Form login</option>
            <option value=\"jwt\" ${settings.tester_auth_mode === 'jwt' ? 'selected' : ''}>JWT bearer</option>
            <option value=\"headers\" ${settings.tester_auth_mode === 'headers' ? 'selected' : ''}>Custom headers</option>
          </select>
          <div class=\"lbl\">Tester Auth Login URL</div>
          <input id=\"testerAuthLoginUrl\" value=\"${esc(settings.tester_auth_login_url || '')}\" placeholder=\"http://127.0.0.1:3000/login\" />
          <div class=\"lbl\">Tester Auth Username</div>
          <input id=\"testerAuthUsername\" value=\"${esc(settings.tester_auth_username || '')}\" />
          <div class=\"lbl\">Tester Auth Password</div>
          <input id=\"testerAuthPassword\" value=\"\" placeholder=\"${settings.tester_auth_password_present ? 'Configured (leave blank to keep current)' : 'Optional test password'}\" />
          <div class=\"lbl\">Tester Auth JWT</div>
          <input id=\"testerAuthJwt\" value=\"\" placeholder=\"${settings.tester_auth_jwt_present ? 'Configured (leave blank to keep current)' : 'Optional bearer token'}\" />
          <div class=\"lbl\">Tester Auth Headers (JSON)</div>
          <input id=\"testerAuthHeaders\" value=\"\" placeholder=\"${settings.tester_auth_headers_present ? 'Configured (leave blank to keep current)' : '{\\\"x-test\\\":\\\"value\\\"}'}\" />
          <div style=\"margin-top:10px\"><button class=\"btn\" type=\"submit\">Save Settings</button></div>
        </form>
        <div style=\"margin-top:10px\" class=\"empty\">GitHub CLI: <span class=\"${gh.installed?'ok':'bad'}\">${gh.installed?'installed':'missing'}</span> · auth: <span class=\"${gh.authed?'ok':'bad'}\">${gh.authed?'ok':'not authed'}</span></div>
        <div class=\"lbl\">Connected Code Repository</div>
        ${repoRows ? `<ul>${repoRows}</ul>` : '<div class=\"empty\">No connected repos yet.</div>'}
        <form id=\"repoConnectForm\" style=\"margin-top:8px\">
          <div class=\"grid\">
            <div>
              <div class=\"lbl\">Repo Label</div>
              <input id=\"repoFullName\" value=\"${esc(repos[0]?.provider === 'local' ? '' : (repos[0]?.repo_full_name || ''))}\" placeholder=\"owner/name or leave blank for local path\" />
            </div>
            <div>
              <div class=\"lbl\">Branch</div>
              <input id=\"repoDefaultBranch\" value=\"${esc(repos[0]?.default_branch || 'main')}\" />
            </div>
          </div>
          <div class=\"lbl\">Local Checkout Path</div>
          <input id=\"repoLocalPath\" value=\"${esc(repos[0]?.local_path || '')}\" placeholder=\"/path/to/repo\" />
          <div style=\"margin-top:8px\"><button class=\"btn\" type=\"submit\">Connect Repo</button> <span class=\"empty\" id=\"repoConnectStatus\"></span></div>
        </form>
        <div class=\"lbl\" style=\"margin-top:12px\">Browser Replay Capture Key</div>
        <form id=\"sdkKeyForm\" style=\"margin-top:8px\">
          <div class=\"grid\">
            <div>
              <div class=\"lbl\">Project</div>
              <input id=\"sdkProjectName\" value=\"Default\" />
            </div>
            <div>
              <div class=\"lbl\">Environment</div>
              <input id=\"sdkEnvironmentName\" value=\"production\" />
            </div>
          </div>
          <div class=\"lbl\">Key Name</div>
          <input id=\"sdkKeyName\" value=\"Browser SDK\" />
          <div style=\"margin-top:8px\"><button class=\"btn\" type=\"submit\">Create SDK Key</button> <span class=\"empty\" id=\"sdkKeyStatus\"></span></div>
        </form>
        <div id=\"sdkKeyResult\"></div>
        <div class=\"empty\">PostHog check: <span class=\"${ph.reachable===true?'ok':(ph.reachable===false?'bad':'')}\">${ph.reachable===true?'reachable':(ph.reachable===false?'unreachable':'not configured')}</span> ${esc(ph.detail || '')}</div>
        <div class=\"empty\">LLM check (${esc(llmProviderLabel)}): <span class=\"${llm.reachable===true?'ok':(llm.reachable===false?'bad':'')}\">${llm.reachable===true?'reachable':(llm.reachable===false?'unreachable':'not configured')}</span> ${esc(llm.detail || '')}</div>
        <div class=\"empty\">Replay ingest API: <span class=\"${replayApi.reachable===true?'ok':'bad'}\">${replayApi.reachable===true?'reachable':'unreachable'}</span> at <code>${esc(replayApi.url || 'http://127.0.0.1:8788')}</code> ${esc(replayApi.detail || '')}</div>
        ${replayApi.reachable !== true ? `<div class=\"empty\">Run in terminal: <code>${esc(replayApi.commands?.serve || 'retrace api serve')}</code> <button class=\"btn\" id=\"copyReplayServeBtn\" data-copy-text=\"${esc(replayApi.commands?.serve || 'retrace api serve')}\">Copy</button></div>` : ''}
        ${!gh.installed ? `<div class=\"empty\">Run in terminal: <code>${esc(gh.commands?.install || 'brew install gh')}</code> <button class=\"btn\" id=\"copyGhInstallBtn\" data-copy-text=\"${esc(gh.commands?.install || 'brew install gh')}\">Copy</button></div>` : ''}
        ${gh.installed && !gh.authed ? `<div class=\"empty\">Run in terminal: <code>${esc(gh.commands?.login || 'gh auth login')}</code> <button class=\"btn\" id=\"copyGhLoginBtn\" data-copy-text=\"${esc(gh.commands?.login || 'gh auth login')}\">Copy</button></div>` : ''}
      `;
      byId('llmProvider').addEventListener('change', () => syncProviderUI(true));
      byId('fetchModelsBtn').addEventListener('click', fetchModels);
      byId('llmModelPicker').addEventListener('change', onModelPick);
      syncProviderUI(false);
      byId('settingsForm').addEventListener('submit', saveSettings);
      byId('repoConnectForm').addEventListener('submit', connectGithubRepo);
      byId('sdkKeyForm').addEventListener('submit', createSdkKey);
      byId('copyReplayServeBtn')?.addEventListener('click', ev => copyText(ev.currentTarget.dataset.copyText));
      byId('copyGhInstallBtn')?.addEventListener('click', ev => copyText(ev.currentTarget.dataset.copyText));
      byId('copyGhLoginBtn')?.addEventListener('click', ev => copyText(ev.currentTarget.dataset.copyText));
    }

    async function createTesterSpec(ev){
      ev.preventDefault();
      const body = {
        name: byId('testerName').value,
        mode: byId('testerMode').value,
        prompt: byId('testerPrompt').value,
        app_url: byId('testerSpecAppUrl').value,
      };
      const res = await fetch('/api/tester/specs', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){ alert(data.error || 'Failed to create tester spec'); return; }
      byId('testerPrompt').value = '';
      await loadTesterPanel();
    }

    async function runTesterSpec(){
      const specId = byId('testerSpecSelect').value;
      if(!specId){ return; }
      byId('testerRunStatus').textContent = 'Running...';
      const res = await fetch('/api/tester/run', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          spec_id: specId,
          retries: Number(byId('testerMaxRetries')?.value || 1),
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        const msg = data?.result?.error || data.error || 'Run failed';
        byId('testerRunStatus').textContent = `Failed: ${msg}`;
        await refreshTesterAndReplay();
        return;
      }
      byId('testerRunStatus').textContent = `OK run ${data.result.run_id} (${data.result.status || 'passed'})`;
      await refreshTesterAndReplay();
    }

    async function generateReplayIssueSpec(issue){
      if(!issue){ return; }
      const status = byId('replaySpecStatus');
      if(status) status.textContent = 'Generating...';
      const res = await fetch('/api/replay-issue/spec', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_id: issue.public_id || issue.id,
          project_id: issue.project_id,
          environment_id: issue.environment_id,
          app_url: byId('testerSpecAppUrl')?.value || '',
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = `Failed: ${data.error || 'could not generate spec'}`;
        return;
      }
      if(status) status.textContent = `Created ${data.spec.spec_id} (${data.confidence} confidence)`;
      await refreshTesterAndReplay(issue.public_id);
    }

    function selectedReplayIssueIds(){
      const checked = [...document.querySelectorAll('[data-issue-select]:checked')].map(el => el.value).filter(Boolean);
      if(checked.length) return checked;
      const list = byId('replayIssueList');
      if(!list) return [];
      return [...list.querySelectorAll('[data-replay-issue]')]
        .filter(row => row.style.display !== 'none')
        .map(row => row.dataset.replayIssue)
        .filter(Boolean);
    }

    async function generateGroupedReplayIssueSpecs(){
      const status = byId('groupReplaySpecStatus');
      const issueIds = selectedReplayIssueIds();
      if(status) status.textContent = issueIds.length ? `Generating ${issueIds.length} spec(s)...` : 'Generating specs...';
      const res = await fetch('/api/replay-issues/specs', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_ids: issueIds,
          status: byId('issueStatusFilter')?.value || '',
          project_id: replayState.issues[0]?.project_id || '',
          environment_id: replayState.issues[0]?.environment_id || '',
          app_url: byId('testerSpecAppUrl')?.value || '',
          missing_only: true,
          limit: Math.min(issueIds.length || 25, 100),
        }),
      });
      const data = await res.json();
      const failures = (data.failed || []).map(item => `${item.issue_public_id}: ${item.error}`).join('; ');
      if(!res.ok){
        if(status) status.textContent = failures || data.error || 'Grouped spec generation failed';
        await refreshTesterAndReplay(replayState.activeIssueId);
        return;
      }
      if(status) {
        status.textContent = failures
          ? `Generated ${data.generated || 0}; skipped ${(data.skipped || []).length}; failed ${(data.failed || []).length}: ${failures}`
          : `Generated ${data.generated || 0}; skipped ${(data.skipped || []).length}.`;
      }
      await refreshTesterAndReplay(replayState.activeIssueId);
    }

    async function generateReplayIssueApiSpec(issue){
      if(!issue){ return; }
      const status = byId('replayApiSpecStatus');
      if(status) status.textContent = 'Generating...';
      const res = await fetch('/api/replay-issue/api-spec', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_id: issue.public_id || issue.id,
          project_id: issue.project_id,
          environment_id: issue.environment_id,
          app_url: byId('testerSpecAppUrl')?.value || '',
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = `Failed: ${data.error || 'could not generate API spec'}`;
        return;
      }
      if(status) status.textContent = `Created ${data.spec.spec_id} (${data.spec.method} ${data.spec.url})`;
      await refreshTesterAndReplay(issue.public_id);
    }

    async function runReplayIssueApiSpec(specId, issueId){
      const status = byId('replayApiSpecStatus');
      if(status) status.textContent = `Running ${specId}...`;
      const res = await fetch('/api/replay-issue/api-run', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({spec_id: specId}),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        const msg = data?.result?.error || data.error || 'API run failed';
        if(status) status.textContent = `Failed: ${msg}`;
        await refreshTesterAndReplay(issueId || replayState.activeIssueId);
        return;
      }
      if(status) status.textContent = `API passed: ${data.result.run_id}`;
      await refreshTesterAndReplay(issueId || replayState.activeIssueId);
    }

    async function generateReplayIssueFixPrompts(issue){
      if(!issue){ return; }
      const status = byId('replayFixPromptStatus');
      if(status) status.textContent = 'Generating...';
      const res = await fetch('/api/replay-issue/fix-prompts', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_id: issue.public_id || issue.id,
          project_id: issue.project_id,
          environment_id: issue.environment_id,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = `Failed: ${data.error || 'could not generate prompts'}`;
        return;
      }
      if(status) status.textContent = `Wrote ${data.generated || 0} prompt set(s) for ${data.repo || 'repo'}`;
      await refreshTesterAndReplay(issue.public_id);
      renderReplayFixSuggestions(data);
    }

    async function transitionReplayIssue(issue, statusValue){
      if(!issue){ return; }
      const status = byId('replayLifecycleStatus');
      if(status) status.textContent = 'Saving...';
      const res = await fetch('/api/replay-issue/status', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_id: issue.public_id || issue.id,
          project_id: issue.project_id,
          environment_id: issue.environment_id,
          status: statusValue,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = `Failed: ${data.error || 'could not update issue'}`;
        return;
      }
      const updated = data.issue || issue;
      Object.assign(issue, updated);
      if(status) status.textContent = `Status: ${updated.status}`;
      await loadReplayDashboard(`Updated ${updated.public_id || issue.public_id} to ${updated.status}.`);
      const refreshed = replayState.issues.find(i => i.public_id === (updated.public_id || issue.public_id));
      renderReplayIssueDetail(refreshed || issue);
    }

    function renderReplayFixSuggestions(data){
      const root = byId('replayFixPrompts');
      if(!root){ return; }
      const cands = (data.candidates || []).map(c =>
        `<li><code>${esc(c.file_path)}</code>${c.symbol ? ` · <code>${esc(c.symbol)}</code>` : ''} (score=${esc(c.score)})<br><span class="empty">${esc(c.rationale)}</span></li>`
      ).join('');
      const codex = data.prompts?.codex || '';
      const claude = data.prompts?.claude_code || '';
      root.innerHTML = `
        <div class="grid">
          <div class="card"><h3>Likely Culprits</h3>${cands ? `<ul>${cands}</ul>` : '<div class="empty">No code candidates found. Connect a repo with a local path for file matching.</div>'}</div>
          <div class="card"><h3>Artifacts</h3>
            <div class="empty">Repo: <code>${esc(data.repo || '')}</code></div>
            <div class="empty">Output: <code>${esc(data.out_dir || '')}</code></div>
            <div class="empty">JSON: <code>${esc(data.artifact_json || '')}</code></div>
          </div>
        </div>
        <div style="height:12px"></div>
        <div class="grid">
          <div class="card"><h3>Codex Prompt <button class="btn" id="copyReplayCodexPrompt" type="button">Copy</button></h3><pre>${esc(codex)}</pre></div>
          <div class="card"><h3>Claude Prompt <button class="btn" id="copyReplayClaudePrompt" type="button">Copy</button></h3><pre>${esc(claude)}</pre></div>
        </div>
      `;
      byId('copyReplayCodexPrompt')?.addEventListener('click', () => copyText(codex));
      byId('copyReplayClaudePrompt')?.addEventListener('click', () => copyText(claude));
    }

    async function loadTesterPanel(){
      const [specRes, runsRes, settingsRes, suitesRes] = await Promise.all([
        fetch('/api/tester/specs'),
        fetch('/api/tester/runs'),
        fetch('/api/settings'),
        fetch('/api/api-suites'),
      ]);
      const specData = await specRes.json();
      const runData = await runsRes.json();
      const settings = await settingsRes.json();
      const suiteData = await suitesRes.json();
      const specs = specData.specs || [];
      const runs = runData.runs || [];
      const apiSuites = suiteData.suites || [];
      const specOptions = specs.map(s =>
        `<option value="${esc(s.spec_id)}">${esc(s.name)} (${esc(s.mode)})</option>`
      ).join('');
      const draftSpecs = specs.filter(s => (s.fixtures || {}).draft_status === 'draft');
      const draftOptions = draftSpecs.map(s =>
        `<option value="${esc(s.spec_id)}">${esc(s.name)} · ${esc(s.spec_id)}</option>`
      ).join('');
      const suiteRows = apiSuites.map(s => {
        const summary = s.import_summary || {};
        const warnings = s.quality_warning_count || 0;
        const operations = (s.operations || []).slice(0, 5).map(op => `<li><code>${esc(op.method)}</code> ${esc(op.path || op.url || '')}${op.operation_id ? ` · ${esc(op.operation_id)}` : ''}</li>`).join('');
        return `
          <div class="suite-row">
            <div><strong>${esc(s.name || s.suite_id)}</strong> <code>${esc(s.suite_id)}</code></div>
            <div class="empty">source=<code>${esc(s.source)}</code> · specs=<code>${esc(s.spec_count)}</code> · operations=<code>${esc(s.operation_count)}</code> · skipped=<code>${esc(s.skipped_count)}</code> · warnings=<code class="${warnings ? 'bad' : 'ok'}">${esc(warnings)}</code></div>
            <div class="empty">coverage=<code>${esc(summary.coverage_percent ?? 0)}%</code>${s.auth_profile ? ` · auth=<code>${esc(s.auth_profile)}</code>` : ''}${s.env_profile ? ` · env=<code>${esc(s.env_profile)}</code>` : ''}</div>
            ${operations ? `<ul>${operations}</ul>` : ''}
          </div>
        `;
      }).join('');
      const runRows = runs.map(r =>
        `<li><code>${esc(r.run_id || '')}</code> · ${r.ok ? '<span class="ok">ok</span>' : '<span class="bad">fail</span>'} · <code>${esc(r.status || '')}</code> · attempts=<code>${esc(r.attempts || 1)}</code>${r.failure_classification ? ` · class=<code>${esc(r.failure_classification)}</code>` : ''}${r.flake_reason ? ` · flake=<code>${esc(r.flake_reason)}</code>` : ''} · <code>${esc(r.spec_id || '')}</code><br><span class="empty">${esc(r.run_dir || '')}</span></li>`
      ).join('');
      byId('tester').innerHTML = `
        <div class="view-head"><div><h2>Tests</h2><div class="empty">Create local specs, run saved checks, and verify linked failures.</div></div></div>
        <div class="detail-grid">
          <div class="card">
            <h3>Local UI Tester</h3>
            <form id="testerCreateForm">
              <div class="lbl">Test Name</div>
              <input id="testerName" value="" placeholder="Checkout happy path" />
              <div class="lbl">Mode</div>
              <select id="testerMode" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
                <option value="describe">Describe Test</option>
                <option value="explore_suite">AI Explore Full Suite</option>
              </select>
              <div class="lbl">Prompt / Task</div>
              <input id="testerPrompt" value="" placeholder="Describe a specific test. Leave blank for suite exploration mode." />
              <div class="lbl">App URL (override)</div>
              <input id="testerSpecAppUrl" value="${esc(settings.tester_app_url || 'http://127.0.0.1:3000')}" />
              <div style="margin-top:10px"><button class="btn" type="submit">Save Test Spec</button></div>
            </form>
            <div class="lbl" style="margin-top:12px">Run Saved Spec</div>
            <select id="testerSpecSelect" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
              ${specOptions || '<option value="">No specs yet</option>'}
            </select>
            <div style="margin-top:8px"><button class="btn" id="runTesterBtn" type="button">Run Selected Test</button> <span class="empty" id="testerRunStatus"></span></div>
          </div>
          <div class="card">
            <h3>Linked Failures</h3>
            <div id="linkedFailureTests"><div class="empty">Loading linked failures...</div></div>
          </div>
        </div>
        <div class="card draft-editor">
          <h3>Generated Draft Review</h3>
          ${draftSpecs.length ? `
            <div class="draft-grid">
              <div>
                <div class="lbl">Draft Spec</div>
                <select id="draftSpecSelect" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">${draftOptions}</select>
                <div class="lbl">Name</div>
                <input id="draftName" value="" />
                <div class="lbl">Prompt</div>
                <textarea id="draftPrompt"></textarea>
                <div class="lbl">App URL</div>
                <input id="draftAppUrl" value="" />
                <div class="lbl">Review Note</div>
                <input id="draftReviewNote" value="" placeholder="What changed or what you verified" />
                <div style="margin-top:8px">
                  <button class="btn" id="saveDraftSpecBtn" type="button">Save Draft</button>
                  <button class="btn" id="acceptDraftSpecBtn" type="button">Accept Draft</button>
                  <button class="btn" id="runAcceptedDraftBtn" type="button">Run Accepted</button>
                  <span class="empty" id="draftEditStatus"></span>
                </div>
              </div>
              <div>
                <div class="lbl">Steps JSON</div>
                <textarea id="draftStepsJson"></textarea>
                <div class="lbl">Assertions JSON</div>
                <textarea id="draftAssertionsJson"></textarea>
                <div class="empty" id="draftReviewSummary"></div>
              </div>
            </div>
          ` : '<div class="empty">No generated drafts waiting for review.</div>'}
        </div>
        <div style="height:12px"></div>
        <div class="card">
          <h3>API Suites</h3>
          ${suiteRows || '<div class="empty">No API suites yet. Import an OpenAPI document with <code>retrace tester api-import-openapi</code>.</div>'}
        </div>
      `;
      byId('runsView').innerHTML = `
        <div class="view-head"><div><h2>Runs</h2><div class="empty">Recent local tester results.</div></div></div>
        <div class="card">${runRows ? `<ul>${runRows}</ul>` : '<div class="empty">No runs yet.</div>'}</div>
      `;
      byId('testerCreateForm').addEventListener('submit', createTesterSpec);
      byId('runTesterBtn').addEventListener('click', runTesterSpec);
      if(draftSpecs.length){
        window.retraceDraftSpecs = draftSpecs;
        byId('draftSpecSelect')?.addEventListener('change', renderSelectedDraftEditor);
        byId('saveDraftSpecBtn')?.addEventListener('click', () => saveDraftSpec(false));
        byId('acceptDraftSpecBtn')?.addEventListener('click', () => saveDraftSpec(true));
        byId('runAcceptedDraftBtn')?.addEventListener('click', runAcceptedDraftSpec);
        renderSelectedDraftEditor();
      }
      renderLinkedFailureTests();
    }

    function selectedDraftSpec(){
      const id = byId('draftSpecSelect')?.value || '';
      return (window.retraceDraftSpecs || []).find(s => s.spec_id === id) || null;
    }

    function renderSelectedDraftEditor(){
      const spec = selectedDraftSpec();
      if(!spec){ return; }
      byId('draftName').value = spec.name || '';
      byId('draftPrompt').value = spec.prompt || '';
      byId('draftAppUrl').value = spec.app_url || '';
      byId('draftStepsJson').value = JSON.stringify(spec.exact_steps || [], null, 2);
      byId('draftAssertionsJson').value = JSON.stringify(spec.assertions || [], null, 2);
      const fixtures = spec.fixtures || {};
      const generation = fixtures.generation || {};
      const review = generation.review || {};
      const notes = fixtures.review_notes || [];
      byId('draftReviewSummary').innerHTML = `
        draft=<code>${esc(fixtures.draft_status || '')}</code> · steps=<code>${esc((spec.exact_steps || []).length)}</code> · assertions=<code>${esc((spec.assertions || []).length)}</code>
        ${review.summary ? `<br>${esc(review.summary)}` : ''}
        ${notes.length ? `<br>Notes: ${notes.map(item => `<code>${esc(item)}</code>`).join(' ')}` : ''}
      `;
      byId('draftEditStatus').textContent = '';
    }

    function parseDraftJson(id, label){
      try {
        const value = JSON.parse(byId(id).value || '[]');
        if(!Array.isArray(value) || value.some(item => !item || typeof item !== 'object' || Array.isArray(item))){
          throw new Error(`${label} must be a JSON list of objects`);
        }
        return value;
      } catch(err) {
        throw new Error(`${label}: ${err.message || err}`);
      }
    }

    async function saveDraftSpec(accept=false){
      const spec = selectedDraftSpec();
      const status = byId('draftEditStatus');
      if(!spec || !status){ return; }
      let steps, assertions;
      try {
        steps = parseDraftJson('draftStepsJson', 'Steps');
        assertions = parseDraftJson('draftAssertionsJson', 'Assertions');
      } catch(err) {
        status.textContent = err.message || String(err);
        return;
      }
      status.textContent = accept ? 'Accepting...' : 'Saving...';
      const res = await fetch('/api/tester/draft', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          spec_id: spec.spec_id,
          name: byId('draftName').value,
          prompt: byId('draftPrompt').value,
          app_url: byId('draftAppUrl').value,
          steps,
          assertions,
          review_note: byId('draftReviewNote').value,
          accept,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        status.textContent = data.error || 'Draft update failed';
        return;
      }
      status.textContent = accept
        ? `Accepted ${data.spec.spec_id}`
        : `Saved ${data.changed_fields.join(', ') || 'metadata'}`;
      await loadTesterPanel();
      if(accept){
        const select = byId('testerSpecSelect');
        if(select) select.value = data.spec.spec_id;
      }
    }

    async function runAcceptedDraftSpec(){
      const spec = selectedDraftSpec();
      if(!spec){ return; }
      await saveDraftSpec(true);
      const select = byId('testerSpecSelect');
      if(select) select.value = spec.spec_id;
      await runTesterSpec();
    }

    async function processReplayJobs(){
      const status = byId('replayProcessStatus');
      status.textContent = 'Processing...';
      const ai = !!byId('replayAiAnalysis')?.checked;
      const res = await fetch('/api/replays/process', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({limit: 25, ai})});
      const data = await res.json();
      if(!res.ok || !data.ok){
        status.textContent = data.error || 'Processing failed';
        return;
      }
      await loadReplayDashboard(`Processed ${data.result.jobs_processed} job(s), updated ${data.result.issues_created_or_updated} issue(s)${ai ? ' with AI analysis' : ''}.`);
    }

    async function importPostHogReplays(){
      const status = byId('replayProcessStatus');
      status.textContent = 'Importing PostHog replays...';
      const ai = !!byId('replayAiAnalysis')?.checked;
      const res = await fetch('/api/replays/import-posthog', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({since_hours: 24, max_sessions: 50, process: true, ai}),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        status.textContent = data.error || 'PostHog import failed';
        return;
      }
      const processed = data.processed || {};
      await loadReplayDashboard(`Imported ${data.imported_sessions.length} PostHog replay(s); processed ${processed.jobs_processed || 0} job(s), updated ${processed.issues_created_or_updated || 0} issue(s).`);
    }

    async function verifyResolvedReplayIssues(){
      const status = byId('verifyResolvedStatus');
      if(status) status.textContent = 'Verifying...';
      const res = await fetch('/api/replay-issues/verify-resolved', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({limit: 10}),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = data.error || 'Verification failed';
        return;
      }
      const verified = (data.verified || []).length;
      const regressed = (data.regressed || []).length;
      const planned = (data.plan || []).length;
      const message = `Verified ${verified}/${planned} resolved issue(s); regressed=${regressed}.`;
      await refreshTesterAndReplay('', message);
      if(byId('verifyResolvedStatus')) byId('verifyResolvedStatus').textContent = message;
    }

    async function loadReplayDashboard(processStatus = ''){
      const res = await fetch('/api/replay-dashboard');
      const data = await res.json();
      const issues = data.issues || [];
      const sessions = data.sessions || [];
      replayState.issues = issues;
      replayState.sessions = sessions;
      const issueOptions = [...new Set(issues.map(i => i.status || 'unknown'))].sort()
        .map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
      const sessionOptions = [...new Set(sessions.map(s => s.status || 'unknown'))].sort()
        .map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
      const issueRows = issues.map(i => `
          <div class="issue-row ${replayState.activeIssueId === i.public_id ? 'active' : ''}" role="button" tabindex="0" data-issue-status="${esc(i.status)}" data-replay-issue="${esc(i.public_id)}">
            <input type="checkbox" data-issue-select value="${esc(i.public_id)}" aria-label="Select ${esc(i.public_id)}" style="width:auto; margin-right:6px" />
            <div class="sev">${esc(i.status)} · ${esc(i.severity)} · ${esc(i.confidence || 'medium')} confidence</div>
            <div class="title">${esc(i.title || 'Untitled issue')}</div>
          <div class="empty">${esc(i.public_id)} · sessions=${esc(i.affected_count)} · users=${esc(i.affected_users || 0)} · evidence=${esc((i.timeline || []).length)} · tests=${esc((i.test_links || []).length)} · ${esc(i.analysis_status || 'fallback')}</div>
          </div>`).join('');
      const sessionRows = sessions.map(s => `
        <li data-session-status="${esc(s.status)}"><button class="btn" type="button" data-replay-session="${esc(s.stable_id)}">Play</button>
          <a href="${esc(s.share_url)}"><code>${esc(s.public_id)}</code></a> · ${esc(s.stable_id)}<br>
          <span class="empty">${esc(s.status)} · events=${esc(s.event_count)} · ${esc(s.last_seen_at)} · ${esc(JSON.stringify(s.preview || {}))}</span>
        </li>`).join('');
      const unresolved = issues.filter(i => !['resolved', 'verified', 'ignored'].includes(i.status)).length;
      const covered = issues.filter(i => (i.test_links || []).length > 0).length;
      const high = issues.filter(i => i.severity === 'high' || i.priority === 'high').length;
      byId('issueWorkflowList').innerHTML = `
        <div class="hdr">
          <select id="issueStatusFilter" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
            <option value="">All statuses</option>${issueOptions}
          </select>
          <button class="btn" id="generateGroupedReplaySpecsBtn" type="button" style="margin-top:8px">Generate Tests For Group</button>
          <div class="empty" id="groupReplaySpecStatus"></div>
        </div>
        <div id="replayIssueList">${issueRows || '<div class="empty" style="padding:12px">No replay issues yet.</div>'}</div>
      `;
      byId('dashboardView').innerHTML = `
        <div class="view-head">
          <div><h2>Dashboard</h2><div class="empty">Local-first QA workflow across issues, replays, generated tests, and repair prompts.</div></div>
          <div class="actions"><button class="btn" type="button" data-view-jump="issues">Open Issue Detail</button></div>
        </div>
        <div class="metric-grid">
          <div class="metric"><strong>${esc(issues.length)}</strong><span class="empty">total issues</span></div>
          <div class="metric"><strong>${esc(unresolved)}</strong><span class="empty">active issues</span></div>
          <div class="metric"><strong>${esc(covered)}</strong><span class="empty">with linked tests</span></div>
          <div class="metric"><strong>${esc(high)}</strong><span class="empty">high priority/severity</span></div>
        </div>
        <div class="card"><h3>Next Issues</h3>${issues.slice(0, 6).map(i => `<div class="issue-row" role="button" tabindex="0" data-replay-issue="${esc(i.public_id)}"><div class="sev">${esc(i.status)} · ${esc(i.severity)}</div><div class="title">${esc(i.title || 'Untitled issue')}</div><div class="empty">${esc(i.public_id)} · timeline=${esc((i.timeline || []).length)} · tests=${esc((i.test_links || []).length)}</div></div>`).join('') || '<div class="empty">No issues yet.</div>'}</div>
      `;
      byId('replaySessionsPanel').innerHTML = `
        <div class="card">
          <h3>Recent Sessions</h3>
          <select id="sessionStatusFilter" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
            <option value="">All statuses</option>${sessionOptions}
          </select>
          ${sessionRows ? `<ul id="replaySessionList">${sessionRows}</ul>` : '<div class="empty">No first-party replay sessions yet.</div>'}
        </div>
      `;
      if(byId('replayProcessStatus')) byId('replayProcessStatus').textContent = processStatus;
      byId('importPostHogReplaysBtn').onclick = importPostHogReplays;
      byId('processReplayJobsBtn').onclick = processReplayJobs;
      byId('verifyResolvedBtn').onclick = verifyResolvedReplayIssues;
      document.querySelectorAll('[data-replay-session]').forEach(el => {
        el.onclick = () => loadFirstPartyReplay(el.dataset.replaySession);
      });
      bindReplayIssueRows();
      document.querySelectorAll('[data-issue-select]').forEach(el => {
        el.addEventListener('click', ev => ev.stopPropagation());
        el.addEventListener('keydown', ev => ev.stopPropagation());
      });
      byId('generateGroupedReplaySpecsBtn')?.addEventListener('click', generateGroupedReplayIssueSpecs);
      document.querySelectorAll('[data-view-jump]').forEach(el => el.addEventListener('click', () => switchView(el.dataset.viewJump)));
      byId('issueStatusFilter')?.addEventListener('change', ev => filterReplayRows('replayIssueList', 'issueStatus', ev.target.value));
      byId('sessionStatusFilter')?.addEventListener('change', ev => filterReplayRows('replaySessionList', 'sessionStatus', ev.target.value));
      applyReplayHash(issues, sessions);
      if(!replayState.activeIssueId && issues[0]){
        renderReplayIssueDetail(issues[0]);
      }
      renderLinkedFailureTests();
    }

    function filterReplayRows(listId, dataKey, value){
      const list = byId(listId);
      if(!list) return;
      list.querySelectorAll('li, .issue-row').forEach(row => {
        row.style.display = !value || row.dataset[dataKey] === value ? '' : 'none';
      });
    }

    function renderIssueTimeline(issue){
      const timeline = issue.timeline || [];
      const types = [...new Set(timeline.map(ev => ev.type || 'evidence'))].sort();
      const options = types.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
      const rows = timeline.map(ev => {
        const reasons = ev.reason_codes || [];
        const reasonText = reasons.length ? ` Reasons: ${reasons.map(code => `<code>${esc(code)}</code>`).join(', ')}` : '';
        const confidenceText = ev.confidence ? ` Confidence: <code>${esc(ev.confidence)}</code>` : '';
        return `
          <div class="timeline-row ${ev.detector_hit ? 'detector' : ''}" data-timeline-type="${esc(ev.type || '')}">
            <div><code>${esc(ev.occurred_at_ms || 0)}ms</code></div>
            <div class="timeline-kind">${esc(ev.kind || ev.type || 'evidence')}</div>
            <div>
              <strong>${esc(ev.title || ev.type || 'Evidence')}</strong>
              <div class="timeline-summary">${esc(ev.summary || '')}</div>
              ${ev.detector ? `<div class="empty">Detector: <code>${esc(ev.detector)}</code>${confidenceText}${reasonText}</div>` : ''}
            </div>
          </div>`;
      }).join('');
      return `
        <div class="lbl">Timeline</div>
        <div style="display:flex; gap:8px; align-items:center; margin:6px 0 8px 0">
          <select id="timelineTypeFilter" style="background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:7px;">
            <option value="">All event types</option>${options}
          </select>
          <button class="btn" id="copyEvidenceBundleBtn" type="button">Copy Evidence Bundle</button>
          <span class="empty" id="copyEvidenceBundleStatus"></span>
        </div>
        ${rows ? `<div class="timeline" id="issueTimeline">${rows}</div>` : '<div class="empty">No timeline evidence captured yet.</div>'}
      `;
    }

    function filterIssueTimeline(value){
      byId('issueTimeline')?.querySelectorAll('.timeline-row').forEach(row => {
        row.style.display = !value || row.dataset.timelineType === value ? '' : 'none';
      });
    }

    function copyEvidenceBundle(issue){
      copyText(JSON.stringify({
        public_id: issue.public_id,
        title: issue.title,
        status: issue.status,
        severity: issue.severity,
        summary: issue.summary,
        likely_cause: issue.likely_cause,
        reproduction_steps: issue.reproduction_steps || [],
        timeline: issue.timeline || [],
        evidence: issue.evidence || {},
      }, null, 2));
      const status = byId('copyEvidenceBundleStatus');
      if(status) status.textContent = 'Copied';
    }

    function renderTestLinks(issue){
      const links = issue.test_links || [];
      const rows = links.map(link => `
        <li>
          <code>${esc(link.spec_id)}</code>${link.spec_name ? ` · ${esc(link.spec_name)}` : ''}
          <br><span class="empty">coverage=<code class="${statusClass(link.coverage_state)}">${esc(link.coverage_state)}</code>${link.latest_run_status ? ` · latest=<code class="${statusClass(link.latest_run_status)}">${esc(link.latest_run_status)}</code>` : ''}${link.latest_run_classification ? ` · class=<code>${esc(link.latest_run_classification)}</code>` : ''}</span>
          ${link.spec_path ? `<br><span class="empty">${esc(link.spec_path)}</span>` : ''}
        </li>
      `).join('');
      return rows ? `<ul>${rows}</ul>` : '<div class="empty">No linked regression tests yet.</div>';
    }

    function renderApiRegressionPanel(issue){
      const calls = issue.api_calls || [];
      const apiLinks = (issue.test_links || []).filter(link => (link.source || '').includes('api'));
      const callRows = calls.map(call => `
        <li>
          <code>${esc(call.method || 'GET')}</code> ${esc(call.url || '')}
          <br><span class="empty">status=<code>${esc(call.status || '')}</code> · detector=<code>${esc(call.detector || '')}</code>${call.confidence ? ` · ${esc(call.confidence)} confidence` : ''}</span>
        </li>
      `).join('');
      const linkRows = apiLinks.map(link => `
        <li>
          <button class="btn" type="button" data-run-api-spec="${esc(link.spec_id)}" data-issue-id="${esc(issue.public_id)}">Run</button>
          <code>${esc(link.spec_id)}</code>${link.spec_name ? ` · ${esc(link.spec_name)}` : ''}
          <br><span class="empty">coverage=<code class="${statusClass(link.coverage_state)}">${esc(link.coverage_state)}</code>${link.latest_run_status ? ` · latest=<code class="${statusClass(link.latest_run_status)}">${esc(link.latest_run_status)}</code>` : ''}</span>
          ${link.spec_path ? `<br><span class="empty">${esc(link.spec_path)}</span>` : ''}
        </li>
      `).join('');
      return `
        <div class="lbl">Triggered API Calls</div>
        ${callRows ? `<ul>${callRows}</ul>` : '<div class="empty">No failed API call evidence on this issue.</div>'}
        <div style="height:8px"></div>
        <button class="btn" id="generateReplayApiSpecBtn" type="button" ${calls.length ? '' : 'disabled'}>Generate API Regression</button>
        <span class="empty" id="replayApiSpecStatus"></span>
        <div style="height:8px"></div>
        ${linkRows ? `<ul>${linkRows}</ul>` : '<div class="empty">No linked API regression tests yet.</div>'}
      `;
    }

    function renderIssueWorkflow(issue){
      const workflow = issue.workflow || {};
      const stages = workflow.stage_states || {};
      const counts = workflow.counts || {};
      const stageLabels = [
        ['evidence', 'Evidence', `${counts.timeline || 0} item(s)`],
        ['reproduction', 'Reproduce', `${counts.replays || 0} replay(s)`],
        ['test', 'Test', `${counts.tests || 0} linked`],
        ['repair', 'Repair', `${counts.repair_tasks || 0} task(s)`],
        ['verification', 'Verify', workflow.coverage_state || 'not_covered'],
      ];
      const action = workflow.primary_action || 'none';
      const button = action !== 'none'
        ? `<button class="btn" type="button" data-workflow-action="${esc(action)}">${esc(workflow.primary_label || 'Continue')}</button>`
        : '';
      return `
        <div class="workflow-strip">
          ${stageLabels.map(([key, label, detail]) => `<div class="workflow-step ${esc(stages[key] || 'current')}"><strong>${esc(label)}</strong><span>${esc(detail)}</span></div>`).join('')}
        </div>
        <div class="workflow-action">
          ${button}
          <span class="empty">Next: ${esc(workflow.primary_label || 'Review issue')}</span>
        </div>
      `;
    }

    function renderIssueReadiness(issue){
      const workflow = issue.workflow || {};
      const blockers = workflow.blockers || [];
      const actions = workflow.recommended_actions || [];
      const blockerRows = blockers.map(item => `<li>${esc(item)}</li>`).join('');
      const actionRows = actions.map(item => `
        <div>
          <button class="btn" type="button" data-workflow-action="${esc(item.action)}">${esc(item.label || item.action)}</button>
          <span class="empty">${esc(item.reason || '')}</span>
        </div>
      `).join('');
      return `
        <div class="readiness-panel">
          <div class="row">
            <div><strong>QA Loop Status</strong><div class="empty">Capture → test → repair → verify across replay, UI, and API evidence.</div></div>
            <code class="${workflow.readiness === 'verified' ? 'ok' : (blockers.length ? 'bad' : '')}">${esc(workflow.readiness || 'unknown')}</code>
          </div>
          ${blockerRows ? `<div class="lbl">Blockers</div><ul>${blockerRows}</ul>` : '<div class="empty" style="margin-top:8px">No blocking evidence gaps detected.</div>'}
          ${actionRows ? `<div class="lbl">Recommended Actions</div><div class="recommendation-list">${actionRows}</div>` : ''}
        </div>
      `;
    }

    function handleIssueWorkflowAction(issue, action){
      if(action === 'generate_replay_spec') return generateReplayIssueSpec(issue);
      if(action === 'generate_api_regression') return generateReplayIssueApiSpec(issue);
      if(action === 'generate_repair') return generateReplayIssueFixPrompts(issue);
      if(action === 'verify_resolved') return verifyResolvedReplayIssues();
      if(action === 'review_timeline'){
        byId('issueTimeline')?.scrollIntoView({behavior:'smooth', block:'start'});
        return;
      }
      if(action === 'run_tests'){
        switchView('tests');
        return;
      }
    }

    function renderRepairTask(issue){
      const task = issue.repair_task || null;
      if(!task){
        return '<div class="empty">No repair task yet. Generate fix prompts to package evidence, likely files, validation commands, and agent-ready prompts.</div>';
      }
      const files = (task.likely_files || []).map(file => `<li><code>${esc(file)}</code></li>`).join('');
      const commands = (task.validation_commands || []).map(command => `<li><code>${esc(command)}</code></li>`).join('');
      const artifacts = (task.prompt_artifacts || []).map(artifact => {
        const label = artifact.label || artifact.path || artifact.type || 'artifact';
        return `<li>${esc(label)}</li>`;
      }).join('');
      const prUrl = safeExternalUrl(task.pr_url);
      return `
        <div class="empty"><code>${esc(task.public_id || task.id)}</code> · ${esc(task.status || 'open')} · ${esc(task.title || 'Repair task')}</div>
        ${files ? `<div class="lbl">Likely Files</div><ul>${files}</ul>` : ''}
        ${commands ? `<div class="lbl">Validation Commands</div><ul>${commands}</ul>` : ''}
        ${artifacts ? `<div class="lbl">Prompt Artifacts</div><ul>${artifacts}</ul>` : ''}
        ${task.risk_notes ? `<div class="lbl">Risk Notes</div><div>${esc(task.risk_notes)}</div>` : ''}
        ${prUrl ? `<div class="lbl">PR</div><a href="${esc(prUrl)}" target="_blank" rel="noopener noreferrer">${esc(prUrl)}</a>` : ''}
      `;
    }

    function renderExternalLinks(issue){
      const links = [];
      const externalTicketUrl = safeExternalUrl(issue.external_ticket_url);
      if(externalTicketUrl) links.push(`<a href="${esc(externalTicketUrl)}" target="_blank" rel="noopener noreferrer">${esc(issue.external_ticket_id || 'External ticket')}</a>`);
      const issueUrl = safeHashUrl(issue.share_url, '#issue=');
      if(issueUrl) links.push(`<a href="${esc(issueUrl)}">Issue permalink</a>`);
      for(const session of issue.sessions || []){
        const replay = replayState.sessions.find(s => s.stable_id === session.session_id || s.public_id === session.public_id) || {};
        const replayId = replay.public_id || session.public_id || session.session_id;
        const playerId = replay.stable_id || session.stable_id || session.session_id;
        if(replayId) links.push(`<a href="#replay=${encodeURIComponent(replayId)}" data-replay-session="${esc(playerId)}">Replay ${esc(replayId)}</a>`);
      }
      return links.length ? `<ul>${links.map(link => `<li>${link}</li>`).join('')}</ul>` : `<div class="empty">${esc(issue.external_ticket_state || 'No external links yet.')}</div>`;
    }

    function renderLinkedFailureTests(){
      const root = byId('linkedFailureTests');
      if(!root){ return; }
      const rows = [];
      for(const issue of replayState.issues || []){
        for(const link of issue.test_links || []){
          rows.push(`
            <li>
              <button class="btn" type="button" data-replay-issue="${esc(issue.public_id)}">Open</button>
              <code>${esc(link.spec_id)}</code> · <span class="${statusClass(link.coverage_state)}">${esc(link.coverage_state)}</span>
              <br><span class="empty">${esc(issue.public_id)} · ${esc(issue.title || 'Replay issue')}${link.latest_run_status ? ` · latest=${esc(link.latest_run_status)}` : ''}</span>
            </li>
          `);
        }
      }
      root.innerHTML = rows.length ? `<ul>${rows.join('')}</ul>` : '<div class="empty">No linked failures yet. Generate a regression spec from an issue to create coverage.</div>';
      bindReplayIssueRows(root);
    }

    function renderReplayIssueDetail(issue){
      const root = byId('replayIssueDetail');
      if(!root || !issue){ return; }
      replayState.activeIssueId = issue.public_id;
      document.querySelectorAll('[data-replay-issue]').forEach(el => {
        el.classList.toggle('active', el.dataset.replayIssue === issue.public_id);
      });
      const steps = (issue.reproduction_steps || []).map(s => `<li>${esc(s)}</li>`).join('');
      const sessions = (issue.sessions || []).map(s => {
        const replay = replayState.sessions.find(session => session.stable_id === s.session_id || session.public_id === s.public_id) || {};
        const playerId = replay.stable_id || s.stable_id || s.session_id;
        const replayId = replay.public_id || s.public_id || s.session_id;
        return `<li><button class="btn" type="button" data-replay-session="${esc(playerId)}">Play</button> <a href="#replay=${esc(replayId)}"><code>${esc(replayId)}</code></a> · ${esc(s.role)}</li>`;
      }).join('');
      root.innerHTML = `
        <div class="view-head">
          <div>
            <h2>${esc(issue.public_id)} · ${esc(issue.title || 'Replay issue')}</h2>
            <div class="empty">${esc(issue.status)} · ${esc(issue.severity)} · ${esc(issue.confidence || 'medium')} confidence · affected=${esc(issue.affected_count)} · users=${esc(issue.affected_users)}</div>
          </div>
          <div class="actions">
            <button class="btn" id="resolveReplayIssueBtn" type="button">Mark Resolved</button>
            <button class="btn" id="unresolveReplayIssueBtn" type="button">Mark Unresolved</button>
            <button class="btn" id="ignoreReplayIssueBtn" type="button">Ignore Fingerprint</button>
          </div>
        </div>
        <div class="empty" id="replayLifecycleStatus"></div>
        ${renderIssueWorkflow(issue)}
        ${renderIssueReadiness(issue)}
        <div class="detail-grid">
          <div>
            <div class="card">
              <h3>Failure Narrative</h3>
              <div class="lbl">Analysis</div><div>${esc(issue.analysis_status || 'fallback')}${issue.analysis_model ? ` · ${esc(issue.analysis_model)}` : ''}${issue.analysis_error ? ` · ${esc(issue.analysis_error)}` : ''}</div>
              <div class="lbl">Summary</div><div>${esc(issue.summary || '')}</div>
              <div class="lbl">Likely Cause</div><div>${esc(issue.likely_cause || '')}</div>
              <div class="lbl">Reproduction Steps</div>${steps ? `<ul>${steps}</ul>` : '<div class="empty">No steps generated yet.</div>'}
            </div>
            <div style="height:12px"></div>
            <div class="card">${renderIssueTimeline(issue)}</div>
            <div style="height:12px"></div>
            <div class="card">
              <h3>Repair Task</h3>
              <button class="btn" id="generateReplayFixPromptsBtn" type="button">Generate Fix Prompts</button>
              <span class="empty" id="replayFixPromptStatus"></span>
              <div style="height:10px"></div>
              ${renderRepairTask(issue)}
              <div id="replayFixPrompts"></div>
            </div>
          </div>
          <div>
            <div class="card"><h3>Replay</h3>${sessions ? `<ul>${sessions}</ul>` : '<div class="empty">No linked sessions.</div>'}</div>
            <div style="height:12px"></div>
            <div class="card">
              <h3>Generated Test</h3>
              <button class="btn" id="generateReplaySpecBtn" type="button">Generate Regression Spec</button>
              <span class="empty" id="replaySpecStatus"></span>
              <div style="height:8px"></div>
              ${renderTestLinks(issue)}
            </div>
            <div style="height:12px"></div>
            <div class="card"><h3>API Regression</h3>${renderApiRegressionPanel(issue)}</div>
            <div style="height:12px"></div>
            <div class="card"><h3>External Links</h3>${renderExternalLinks(issue)}</div>
            <div style="height:12px"></div>
            <div class="card"><h3>Signals</h3><pre>${esc(JSON.stringify(issue.signal_summary || {}, null, 2))}</pre></div>
          </div>
        </div>
      `;
      root.querySelectorAll('[data-replay-session]').forEach(el => {
        el.addEventListener('click', () => loadFirstPartyReplay(el.dataset.replaySession));
      });
      byId('resolveReplayIssueBtn')?.addEventListener('click', () => transitionReplayIssue(issue, 'resolved'));
      byId('unresolveReplayIssueBtn')?.addEventListener('click', () => transitionReplayIssue(issue, 'unresolved'));
      byId('ignoreReplayIssueBtn')?.addEventListener('click', () => transitionReplayIssue(issue, 'ignored'));
      byId('generateReplaySpecBtn')?.addEventListener('click', () => generateReplayIssueSpec(issue));
      byId('generateReplayApiSpecBtn')?.addEventListener('click', () => generateReplayIssueApiSpec(issue));
      byId('generateReplayFixPromptsBtn')?.addEventListener('click', () => generateReplayIssueFixPrompts(issue));
      root.querySelectorAll('[data-workflow-action]').forEach(el => {
        el.addEventListener('click', () => handleIssueWorkflowAction(issue, el.dataset.workflowAction));
      });
      root.querySelectorAll('[data-run-api-spec]').forEach(el => {
        el.addEventListener('click', () => runReplayIssueApiSpec(el.dataset.runApiSpec, el.dataset.issueId));
      });
      byId('timelineTypeFilter')?.addEventListener('change', ev => filterIssueTimeline(ev.target.value));
      byId('copyEvidenceBundleBtn')?.addEventListener('click', () => copyEvidenceBundle(issue));
    }

    function applyReplayHash(issues, sessions){
      const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
      const issueId = hash.get('issue');
      const replayId = hash.get('replay');
      if(issueId){
        renderReplayIssueDetail(issues.find(i => i.public_id === issueId));
        switchView('issues');
      }
      if(replayId){
        const session = sessions.find(s => s.public_id === replayId);
        if(session) loadFirstPartyReplay(session.stable_id);
        switchView('replays');
      }
    }

    async function loadFirstPartyReplay(sessionId){
      const root = byId('firstPartyReplay');
      root.innerHTML = '<div class="empty">Loading replay...</div>';
      const res = await fetch(`/api/replay-session/${encodeURIComponent(sessionId)}/events`);
      const data = await res.json();
      if(!res.ok || !data.events || !data.events.length){
        root.innerHTML = `<div class="empty">${esc(data.error || 'No events found.')}</div>`;
        return;
      }
      root.innerHTML = '';
      new rrwebPlayer({ target: root, props: { events: data.events, width: 980, height: 560, autoPlay: false }});
    }

    function renderList() {
      const root = byId('findings');
      root.innerHTML = findings.map(f => `
        <div class=\"finding ${active && active.id===f.id?'active':''}\" data-id=\"${f.id}\">
          <div class=\"sev\">${esc(f.severity)} · ${esc(f.category)}</div>
          <div class=\"title\">${esc(f.title)}</div>
        </div>`).join('');
      root.querySelectorAll('.finding').forEach(el => {
        el.addEventListener('click', () => {
          active = findings.find(f => f.id === el.dataset.id);
          renderList();
          renderDetail();
        });
      });
    }

    async function loadReplay(sessionId){
      const rr = byId('rr');
      rr.innerHTML = '<div class=\"empty\">Loading replay...</div>';
      try {
        const res = await fetch(`/api/session/${sessionId}/events`);
        const data = await res.json();
        rr.innerHTML = '';
        if(!data.events || !data.events.length){ rr.innerHTML='<div class=\"empty\">No events found.</div>'; return; }
        new rrwebPlayer({ target: rr, props: { events: data.events, width: 980, height: 560, autoPlay: false }});
      } catch(_e){
        rr.innerHTML = '<div class=\"empty\">Replay failed to load.</div>';
      }
    }

    function renderDetail(){
      const root = byId('findingDetail');
      if(!active){ root.innerHTML = '<div class=\"empty\">Select a finding.</div>'; return; }
      const cands = (active.candidates||[]).map(c => `<li><code>${esc(c.file_path)}</code> (score=${c.score})<br><span class=\"empty\">${esc(c.rationale)}</span></li>`).join('');
      const codex = active.prompts?.codex || '';
      const claude = active.prompts?.claude_code || '';
      const errIssues = (active.error_issue_ids||[]).join(', ') || '—';
      const traceIds = (active.trace_ids||[]).join(', ') || '—';
      const issueCount = (active.error_issue_ids||[]).filter(Boolean).length;
      const traceCount = (active.trace_ids||[]).filter(Boolean).length;
      const hasStack = Boolean((active.top_stack_frame || '').trim());
      const hasErrorLink = Boolean(active.error_tracking_url);
      const hasLogsLink = Boolean(active.logs_url);
      const hasCorrelation = issueCount > 0 || traceCount > 0 || hasStack || hasErrorLink || hasLogsLink;
      const errWindow = (active.first_error_ts_ms || active.last_error_ts_ms)
        ? `${active.first_error_ts_ms} → ${active.last_error_ts_ms}`
        : '—';
      const regressionState = active.regression_state || 'new';
      const regressionCount = active.regression_occurrence_count || 1;
      const correlationStatusHtml = hasCorrelation
        ? `<div class=\"empty\">Live correlation data found.</div>
           <div class=\"empty\">Issues: <code>${issueCount}</code> · Traces: <code>${traceCount}</code> · Stack frame: <code>${hasStack ? 'yes' : 'no'}</code></div>
           <div class=\"empty\" style=\"margin-top:4px\">Links: ${hasErrorLink ? 'Error Tracking' : '—'} ${hasLogsLink ? 'Logs' : ''}</div>`
        : `<div class=\"empty\">No correlated error/log/trace evidence yet for this finding.</div>`;
      root.innerHTML = `
        <div class=\"meta card\"><h3>${esc(active.title)}</h3><div class=\"empty\">${esc(active.severity)} · ${esc(active.category)}</div><div style=\"margin-top:8px\"><a href=\"${esc(active.session_url)}\" target=\"_blank\">Open PostHog replay</a></div></div>
        <div style=\"height:10px\"></div>
        <div class=\"rr\"><div id=\"rr\"></div></div>
        <div style=\"height:12px\"></div>
        <div class=\"grid\">
          <div class=\"card\"><h3>Likely Culprits</h3>${cands ? `<ul>${cands}</ul>` : '<div class=\"empty\">No candidates generated.</div>'}</div>
          <div class=\"card\"><h3>Evidence</h3><pre>${esc(active.evidence_text)}</pre></div>
        </div>
        <div style=\"height:12px\"></div>
        <div class=\"grid\">
          <div class=\"card\">
            <h3>Observability Links</h3>
            <div class=\"empty\">Distinct ID: <code>${esc(active.distinct_id || '—')}</code></div>
            <div class=\"empty\">Error issues: <code>${esc(errIssues)}</code></div>
            <div class=\"empty\">Trace IDs: <code>${esc(traceIds)}</code></div>
            <div class=\"empty\">Top stack frame: <code>${esc(active.top_stack_frame || '—')}</code></div>
            <div class=\"empty\">Error window (ms): <code>${esc(errWindow)}</code></div>
            <div class=\"empty\">Regression: <code>${esc(regressionState)}</code> · seen <code>${esc(regressionCount)}</code> time(s)</div>
            <div style=\"margin-top:8px\">${active.error_tracking_url ? `<a href=\"${esc(active.error_tracking_url)}\" target=\"_blank\">Open Error Tracking</a>` : '<span class=\"empty\">Error Tracking link unavailable</span>'}</div>
            <div style=\"margin-top:4px\">${active.logs_url ? `<a href=\"${esc(active.logs_url)}\" target=\"_blank\">Open Logs</a>` : '<span class=\"empty\">Logs link unavailable</span>'}</div>
          </div>
          <div class=\"card\"><h3>Correlation Status</h3>${correlationStatusHtml}</div>
        </div>
        <div style=\"height:12px\"></div>
        <div class=\"grid\">
          <div class=\"card\"><h3>Codex Prompt <button class=\"btn\" id=\"copyFindingCodexPrompt\" type=\"button\">Copy</button></h3><pre>${esc(codex)}</pre></div>
          <div class=\"card\"><h3>Claude Prompt <button class=\"btn\" id=\"copyFindingClaudePrompt\" type=\"button\">Copy</button></h3><pre>${esc(claude)}</pre></div>
        </div>`;
      byId('copyFindingCodexPrompt')?.addEventListener('click', () => copyPrompt('codex'));
      byId('copyFindingClaudePrompt')?.addEventListener('click', () => copyPrompt('claude_code'));
      loadReplay(active.session_id);
    }

    async function bootFindings(){
      const res = await fetch('/api/findings');
      const data = await res.json();
      findings = data.findings || [];
      byId('reportMeta').textContent = data.report_path || 'No report found';
      active = findings[0] || null;
      renderList();
      renderDetail();
    }

    async function boot(){
      await loadOnboarding();
      await loadTesterPanel();
      await loadReplayDashboard();
      await bootFindings();
    }
    boot();
  </script>
</body>
</html>
"""


@click.command("ui")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8787, show_default=True, type=int)
@click.option(
    "--repo",
    "repo_full_name",
    default=None,
    help="Optional connected repo full name filter.",
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

            if path == "/api/findings":
                rp = _latest_report(output_dir)
                findings = _to_findings_payload(
                    store=store,
                    report_path=rp,
                    repo_full_name=repo_full_name,
                )
                self._json({"report_path": str(rp) if rp else "", "findings": findings})
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
