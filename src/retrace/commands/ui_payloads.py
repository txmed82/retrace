"""Payload-building helper functions for the Retrace UI server."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import platform
import socket
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import yaml

from retrace.api_suites import api_suites_dir_for_data_dir, list_api_suites, load_api_suite
from retrace.api_testing import (
    api_runs_dir_for_data_dir,
    api_specs_dir_for_data_dir,
    list_api_specs,
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
    list_specs,
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


def _issue_evidence_stitching_payload(issue: dict[str, Any]) -> dict[str, Any]:
    timeline = issue.get("timeline") if isinstance(issue.get("timeline"), list) else []
    api_calls = issue.get("api_calls") if isinstance(issue.get("api_calls"), list) else []
    test_links = issue.get("test_links") if isinstance(issue.get("test_links"), list) else []
    repair_task = issue.get("repair_task") if isinstance(issue.get("repair_task"), dict) else None
    api_links = [
        link
        for link in test_links
        if "api" in str(link.get("source") or "").lower()
        or "/api-tests/" in str(link.get("spec_path") or "")
    ]
    trace_ids: set[str] = set()
    source_map_states: list[dict[str, Any]] = []
    for call in api_calls:
        trace = call.get("trace") if isinstance(call, dict) else {}
        if isinstance(trace, dict):
            for value in trace.values():
                if isinstance(value, str) and value.strip():
                    trace_ids.add(value.strip())
                elif isinstance(value, list):
                    trace_ids.update(str(item).strip() for item in value if str(item).strip())
    for event in timeline:
        payload = event.get("payload") if isinstance(event, dict) else {}
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        for key in ("trace_id", "trace_ids"):
            value = payload.get(key, metadata.get(key))
            if isinstance(value, str) and value.strip():
                trace_ids.add(value.strip())
            elif isinstance(value, list):
                trace_ids.update(str(item).strip() for item in value if str(item).strip())
        evidence_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        frames = evidence_payload.get("stack_frames") if isinstance(evidence_payload, dict) else []
        if not isinstance(frames, list):
            frames = []
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            source_map_states.append(
                {
                    "filename": str(frame.get("filename") or frame.get("source") or ""),
                    "source_mapped": bool(frame.get("source_mapped")),
                    "reason": str(frame.get("source_map_reason") or ""),
                    "status": str(frame.get("source_map_status") or ""),
                }
            )
    stages = [
        {
            "id": "frontend_replay",
            "label": "Frontend replay",
            "status": "complete" if timeline else "missing",
            "detail": f"{len(timeline)} timeline event(s), {len(issue.get('sessions') or [])} replay(s)",
        },
        {
            "id": "network_api",
            "label": "Network/API evidence",
            "status": "complete" if api_calls else "missing",
            "detail": f"{len(api_calls)} failed API call(s), {len(api_links)} linked API regression(s)",
        },
        {
            "id": "backend_trace",
            "label": "Backend trace/log bridge",
            "status": "complete" if trace_ids else "missing",
            "detail": f"{len(trace_ids)} trace id(s)",
        },
        {
            "id": "source_maps",
            "label": "Source map context",
            "status": "complete"
            if any(item.get("source_mapped") for item in source_map_states)
            else ("partial" if source_map_states else "missing"),
            "detail": f"{len(source_map_states)} stack frame mapping result(s)",
        },
        {
            "id": "repair_context",
            "label": "Repair context",
            "status": "complete" if repair_task else "missing",
            "detail": str((repair_task or {}).get("public_id") or (repair_task or {}).get("id") or ""),
        },
    ]
    return {
        "status": "complete"
        if all(stage["status"] == "complete" for stage in stages)
        else "partial",
        "stages": stages,
        "trace_ids": sorted(trace_ids),
        "api_regression_spec_ids": [str(link.get("spec_id") or "") for link in api_links],
        "source_map_frames": source_map_states[:10],
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
        payload["evidence_stitching"] = _issue_evidence_stitching_payload(payload)
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


def _api_specs_payload(data_dir: Path) -> dict[str, Any]:
    specs = []
    for spec in list_api_specs(api_specs_dir_for_data_dir(data_dir)):
        fixtures = dict(spec.fixtures or {})
        specs.append(
            {
                "spec_id": spec.spec_id,
                "name": spec.name,
                "method": spec.method,
                "url": spec.url,
                "auth_profile": spec.auth_profile,
                "env_profile": spec.env_profile,
                "expected_status": spec.expected_status,
                "request_count": len(spec.steps) if spec.steps else 1,
                "json_assertion_count": len(spec.json_assertions),
                "schema_assertion_count": len(spec.schema_assertions),
                "source": str(fixtures.get("source") or ""),
                "issue_public_id": str(fixtures.get("issue_public_id") or ""),
                "operation_id": str(fixtures.get("operation_id") or ""),
                "openapi_path": str(fixtures.get("openapi_path") or ""),
                "created_at": spec.created_at,
                "updated_at": spec.updated_at,
            }
        )
    return {"specs": specs}


def _hosted_onboarding_readiness_payload(
    *,
    store: Storage,
    data_dir: Path,
    settings: dict[str, Any],
    checks: dict[str, Any],
) -> dict[str, Any]:
    sdk_keys = store.list_sdk_keys(include_revoked=False, limit=10)
    replay_sessions = store.list_recent_replay_sessions(limit=10)
    replay_issues = store.list_recent_replay_issues(limit=10)
    fallback_workspace = store.ensure_workspace(project_name="Default")
    if replay_issues:
        project_id = str(replay_issues[0]["project_id"])
        environment_id = str(replay_issues[0]["environment_id"])
    elif sdk_keys:
        project_id = sdk_keys[0].project_id
        environment_id = sdk_keys[0].environment_id
    else:
        project_id = fallback_workspace.project_id
        environment_id = fallback_workspace.environment_id
    ui_specs = list_specs(specs_dir_for_data_dir(data_dir))
    api_specs = list_api_specs(api_specs_dir_for_data_dir(data_dir))
    api_suites = list_api_suites(api_suites_dir_for_data_dir(data_dir))
    source_maps = store.list_recent_source_maps(
        project_id=project_id,
        environment_id=environment_id,
        limit=10,
    )
    alert_rules = store.list_app_error_alert_rules(
        project_id=project_id,
        environment_id=environment_id,
        limit=10,
    )
    test_links = store.list_all_failure_test_links()
    repair_tasks = store.list_repair_tasks(limit=10)
    steps = [
        {
            "id": "settings",
            "label": "Configure hosted settings",
            "status": "complete"
            if settings.get("tester_app_url") and checks.get("replay_api", {}).get("reachable") is True
            else "current",
            "detail": f"Replay API: {checks.get('replay_api', {}).get('detail') or 'not checked'}",
            "action": "Save settings and run retrace api serve",
        },
        {
            "id": "capture_key",
            "label": "Create browser capture key",
            "status": "complete" if sdk_keys else "current",
            "detail": f"{len(sdk_keys)} active browser SDK key(s)",
            "action": "Create SDK Key",
        },
        {
            "id": "capture_smoke",
            "label": "Verify replay capture",
            "status": "complete" if replay_sessions or replay_issues else "blocked",
            "detail": f"{len(replay_sessions)} recent first-party replay session(s)",
            "action": "Send a smoke replay from the instrumented app",
        },
        {
            "id": "issue_grouping",
            "label": "Process captured errors into issues",
            "status": "complete" if replay_issues else "blocked",
            "detail": f"{len(replay_issues)} recent replay issue(s)",
            "action": "Process Queued Replays",
        },
        {
            "id": "ui_tests",
            "label": "Generate and review UI regressions",
            "status": "complete" if ui_specs and test_links else "current",
            "detail": f"{len(ui_specs)} UI spec(s), {sum(1 for spec in ui_specs if dict(spec.fixtures or {}).get('draft_status') == 'draft')} draft(s)",
            "action": "Generate regression tests from issues",
        },
        {
            "id": "api_tests",
            "label": "Import or generate API coverage",
            "status": "complete" if api_suites or api_specs else "current",
            "detail": f"{len(api_suites)} API suite(s), {len(api_specs)} API spec(s)",
            "action": "Import OpenAPI or generate API regression",
        },
        {
            "id": "monitoring",
            "label": "Harden monitoring",
            "status": "complete" if source_maps and alert_rules else "current",
            "detail": f"{len(source_maps)} source map upload(s), {len(alert_rules)} alert rule(s)",
            "action": "Upload source maps and create alert rules",
        },
        {
            "id": "repair_loop",
            "label": "Create repair-ready context",
            "status": "complete" if repair_tasks else "blocked",
            "detail": f"{len(repair_tasks)} repair task(s)",
            "action": "Generate fix prompts from a failing issue",
        },
    ]
    complete = sum(1 for step in steps if step["status"] == "complete")
    return {
        "workspace": {
            "project_id": project_id,
            "environment_id": environment_id,
        },
        "ready": complete == len(steps),
        "complete": complete,
        "total": len(steps),
        "steps": steps,
        "counts": {
            "sdk_keys": len(sdk_keys),
            "replay_sessions": len(replay_sessions),
            "replay_issues": len(replay_issues),
            "ui_specs": len(ui_specs),
            "api_specs": len(api_specs),
            "api_suites": len(api_suites),
            "source_maps": len(source_maps),
            "alert_rules": len(alert_rules),
            "test_links": len(test_links),
            "repair_tasks": len(repair_tasks),
        },
    }


def _run_api_spec_payload(*, data_dir: Path, spec_id: str) -> tuple[dict[str, Any], int]:
    clean_spec_id = spec_id.strip()
    if not clean_spec_id:
        return {"ok": False, "error": "spec_id is required"}, 400
    try:
        spec = load_api_spec(api_specs_dir_for_data_dir(data_dir), clean_spec_id)
    except Exception:
        return {"ok": False, "error": f"API spec not found: {clean_spec_id}"}, 404
    result = run_api_spec(
        spec=spec,
        runs_dir=api_runs_dir_for_data_dir(data_dir),
    )
    return {"ok": result.ok, "result": result.__dict__}, 200 if result.ok else 400


def _run_api_suite_payload(*, data_dir: Path, suite_id: str) -> tuple[dict[str, Any], int]:
    clean_suite_id = suite_id.strip()
    if not clean_suite_id:
        return {"ok": False, "error": "suite_id is required"}, 400
    try:
        suite = load_api_suite(api_suites_dir_for_data_dir(data_dir), clean_suite_id)
    except Exception:
        return {"ok": False, "error": f"API suite not found: {clean_suite_id}"}, 404
    results: list[dict[str, Any]] = []
    for spec_id in suite.spec_ids:
        try:
            spec = load_api_spec(api_specs_dir_for_data_dir(data_dir), spec_id)
            result = run_api_spec(
                spec=spec,
                runs_dir=api_runs_dir_for_data_dir(data_dir),
            )
            results.append(
                {
                    "spec_id": spec.spec_id,
                    "name": spec.name,
                    "method": spec.method,
                    "url": spec.url,
                    "ok": result.ok,
                    "status": result.status,
                    "status_code": result.status_code,
                    "elapsed_ms": result.elapsed_ms,
                    "run_id": result.run_id,
                    "failure_classification": result.failure_classification,
                    "error": result.error,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "spec_id": str(spec_id),
                    "name": "",
                    "method": "",
                    "url": "",
                    "ok": False,
                    "status": "failed",
                    "status_code": 0,
                    "elapsed_ms": 0,
                    "run_id": "",
                    "failure_classification": "suite_error",
                    "error": str(exc),
                }
            )
    passed = sum(1 for item in results if bool(item.get("ok")))
    failed = len(results) - passed
    return {
        "ok": failed == 0,
        "suite_id": suite.suite_id,
        "name": suite.name,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "results": results,
    }, 200 if failed == 0 else 400


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
