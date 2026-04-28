from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import socket
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import click
import httpx
import yaml

from retrace.llm.client import build_llm_http_request
from retrace.reports.parser import parse_report_findings
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
                "role": str(session["role"]),
                "first_seen_ms": int(session["first_seen_ms"]),
                "last_seen_ms": int(session["last_seen_ms"]),
            }
        )
    for row in issue_rows:
        issues.append(
            {
                "id": str(row["id"]),
                "public_id": str(row["public_id"]),
                "project_id": str(row["project_id"]),
                "environment_id": str(row["environment_id"]),
                "status": str(row["status"]),
                "priority": str(row["priority"]),
                "severity": str(row["severity"]),
                "title": str(row["title"]),
                "summary": str(row["summary"]),
                "likely_cause": str(row["likely_cause"]),
                "reproduction_steps": _json_field(
                    row, "reproduction_steps_json", []
                ),
                "signal_summary": _json_field(row, "signal_summary_json", {}),
                "evidence": _json_field(row, "evidence_json", {}),
                "affected_count": int(row["affected_count"]),
                "affected_users": int(row["affected_users"]),
                "representative_session_id": str(row["representative_session_id"]),
                "external_ticket_state": str(row["external_ticket_state"]),
                "external_ticket_url": str(row["external_ticket_url"]),
                "external_ticket_id": str(row["external_ticket_id"]),
                "first_seen_ms": int(row["first_seen_ms"]),
                "last_seen_ms": int(row["last_seen_ms"]),
                "updated_at": str(row["updated_at"]),
                "sessions": issue_sessions_by_id.get(str(row["id"]), []),
                "share_url": f"#issue={str(row['public_id'])}",
            }
        )
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


_INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Retrace UI</title>
  <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/rrweb-player@latest/dist/style.css\" />
  <style>
    :root { --bg:#0f172a; --panel:#111827; --text:#e5e7eb; --muted:#9ca3af; --acc:#22d3ee; }
    body { margin:0; font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto; background:var(--bg); color:var(--text); }
    .wrap { display:grid; grid-template-columns: 340px 1fr; height:100vh; }
    .left { border-right:1px solid #1f2937; overflow:auto; background:#0b1220; }
    .right { overflow:auto; padding:16px; }
    .hdr { padding:12px 14px; border-bottom:1px solid #1f2937; position:sticky; top:0; background:#0b1220; z-index:2; }
    .finding { padding:10px 12px; border-bottom:1px solid #182235; cursor:pointer; }
    .finding:hover { background:#111a2b; }
    .finding.active { background:#162033; border-left:3px solid var(--acc); }
    .sev { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing: .08em; }
    .title { font-size:14px; line-height:1.35; margin-top:4px; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:14px; }
    .card { background:var(--panel); border:1px solid #1f2937; border-radius:10px; padding:12px; }
    .card h3 { margin:0 0 8px 0; font-size:13px; color:#93c5fd; text-transform:uppercase; letter-spacing:.08em; }
    .lbl { font-size:12px; color:var(--muted); margin-top:8px; }
    input { width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px; }
    ul { margin:0; padding-left:18px; }
    li { margin: 6px 0; font-size:13px; }
    pre { white-space:pre-wrap; font-size:12px; background:#0b1220; border:1px solid #1f2937; padding:10px; border-radius:8px; max-height:360px; overflow:auto; }
    .meta a { color:#67e8f9; text-decoration:none; }
    .meta a:hover { text-decoration:underline; }
    .rr { background:#0b1220; border:1px solid #1f2937; border-radius:10px; padding:8px; }
    .empty { color:var(--muted); font-size:13px; }
    .btn { background:#0b1220; color:#e5e7eb; border:1px solid #374151; border-radius:8px; padding:6px 8px; cursor:pointer; font-size:12px; }
    .ok { color:#86efac; } .bad { color:#fca5a5; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"left\">
      <div class=\"hdr\"><strong>Retrace Findings</strong><div class=\"empty\" id=\"reportMeta\"></div></div>
      <div id=\"findings\"></div>
    </div>
    <div class=\"right\" id=\"detail\">
      <div class=\"card\" id=\"onboarding\"></div>
      <div style=\"height:12px\"></div>
      <div class=\"card\" id=\"tester\"></div>
      <div style=\"height:12px\"></div>
      <div class=\"card\" id=\"replayDashboard\"></div>
      <div style=\"height:12px\"></div>
      <div id=\"findingDetail\"><div class=\"empty\">Select a finding.</div></div>
    </div>
  </div>
  <script src=\"https://cdn.jsdelivr.net/npm/rrweb-player@latest/dist/index.js\"></script>
  <script>
    let findings = [];
    let active = null;
    const LLM_DEFAULTS = {
      openai_compatible: { base_url: 'http://localhost:8080/v1', model: 'llama-3.1-8b-instruct' },
      openai: { base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
      anthropic: { base_url: 'https://api.anthropic.com/v1', model: 'claude-3-5-sonnet-latest' },
      openrouter: { base_url: 'https://openrouter.ai/api/v1', model: 'openai/gpt-4o-mini' },
    };
    const CLOUD_PROVIDERS = new Set(['openai', 'anthropic', 'openrouter']);
    const CUSTOM_MODEL = '__custom__';

    function esc(s){ return String(s || \"\").replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c])); }
    function byId(id){ return document.getElementById(id); }

    function copyText(s){ navigator.clipboard.writeText(String(s || \"\")); }
    function copyPrompt(key){ if(active?.prompts?.[key]) copyText(active.prompts[key]); }

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

    async function loadOnboarding(){
      const [sRes, cRes] = await Promise.all([fetch('/api/settings'), fetch('/api/system-checks')]);
      const settings = await sRes.json();
      const checks = await cRes.json();
      const gh = checks.gh || {};
      const ph = checks.posthog || {};
      const llm = checks.llm || {};
      const llmProvider = settings.llm_provider || 'openai_compatible';
      const llmProviderLabel = llmProvider === 'openai' ? 'OpenAI'
        : llmProvider === 'anthropic' ? 'Anthropic'
        : llmProvider === 'openrouter' ? 'OpenRouter'
        : 'OpenAI-compatible';
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
        <div class=\"empty\">PostHog check: <span class=\"${ph.reachable===true?'ok':(ph.reachable===false?'bad':'')}\">${ph.reachable===true?'reachable':(ph.reachable===false?'unreachable':'not configured')}</span> ${esc(ph.detail || '')}</div>
        <div class=\"empty\">LLM check (${esc(llmProviderLabel)}): <span class=\"${llm.reachable===true?'ok':(llm.reachable===false?'bad':'')}\">${llm.reachable===true?'reachable':(llm.reachable===false?'unreachable':'not configured')}</span> ${esc(llm.detail || '')}</div>
        ${!gh.installed ? `<div class=\"empty\">Run in terminal: <code>${esc(gh.commands?.install || 'brew install gh')}</code> <button class=\"btn\" onclick=\"copyText('${esc(gh.commands?.install || 'brew install gh')}')\">Copy</button></div>` : ''}
        ${gh.installed && !gh.authed ? `<div class=\"empty\">Run in terminal: <code>${esc(gh.commands?.login || 'gh auth login')}</code> <button class=\"btn\" onclick=\"copyText('${esc(gh.commands?.login || 'gh auth login')}')\">Copy</button></div>` : ''}
      `;
      byId('llmProvider').addEventListener('change', () => syncProviderUI(true));
      byId('fetchModelsBtn').addEventListener('click', fetchModels);
      byId('llmModelPicker').addEventListener('change', onModelPick);
      syncProviderUI(false);
      byId('settingsForm').addEventListener('submit', saveSettings);
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
        await loadTesterPanel();
        return;
      }
      byId('testerRunStatus').textContent = `OK run ${data.result.run_id} (${data.result.status || 'passed'})`;
      await loadTesterPanel();
    }

    async function loadTesterPanel(){
      const [specRes, runsRes, settingsRes] = await Promise.all([
        fetch('/api/tester/specs'),
        fetch('/api/tester/runs'),
        fetch('/api/settings'),
      ]);
      const specData = await specRes.json();
      const runData = await runsRes.json();
      const settings = await settingsRes.json();
      const specs = specData.specs || [];
      const runs = runData.runs || [];
      const specOptions = specs.map(s =>
        `<option value="${esc(s.spec_id)}">${esc(s.name)} (${esc(s.mode)})</option>`
      ).join('');
      const runRows = runs.map(r =>
        `<li><code>${esc(r.run_id || '')}</code> · ${r.ok ? '<span class="ok">ok</span>' : '<span class="bad">fail</span>'} · <code>${esc(r.status || '')}</code> · attempts=<code>${esc(r.attempts || 1)}</code>${r.flake_reason ? ` · flake=<code>${esc(r.flake_reason)}</code>` : ''} · <code>${esc(r.spec_id || '')}</code><br><span class="empty">${esc(r.run_dir || '')}</span></li>`
      ).join('');
      byId('tester').innerHTML = `
        <h3>Local UI Tester (Describe + Suite Explore)</h3>
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
        <div style="height:10px"></div>
        <div class="lbl">Run Saved Spec</div>
        <select id="testerSpecSelect" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
          ${specOptions || '<option value="">No specs yet</option>'}
        </select>
        <div style="margin-top:8px"><button class="btn" id="runTesterBtn" type="button">Run Selected Test</button> <span class="empty" id="testerRunStatus"></span></div>
        <div class="lbl" style="margin-top:10px">Recent Runs</div>
        ${runRows ? `<ul>${runRows}</ul>` : '<div class="empty">No runs yet.</div>'}
      `;
      byId('testerCreateForm').addEventListener('submit', createTesterSpec);
      byId('runTesterBtn').addEventListener('click', runTesterSpec);
    }

    async function processReplayJobs(){
      const status = byId('replayProcessStatus');
      status.textContent = 'Processing...';
      const res = await fetch('/api/replays/process', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({limit: 25})});
      const data = await res.json();
      if(!res.ok || !data.ok){
        status.textContent = data.error || 'Processing failed';
        return;
      }
      await loadReplayDashboard(`Processed ${data.result.jobs_processed} job(s), updated ${data.result.issues_created_or_updated} issue(s).`);
    }

    async function loadReplayDashboard(processStatus = ''){
      const res = await fetch('/api/replay-dashboard');
      const data = await res.json();
      const issues = data.issues || [];
      const sessions = data.sessions || [];
      const issueOptions = [...new Set(issues.map(i => i.status || 'unknown'))].sort()
        .map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
      const sessionOptions = [...new Set(sessions.map(s => s.status || 'unknown'))].sort()
        .map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
      const issueRows = issues.map(i => `
        <li data-issue-status="${esc(i.status)}"><button class="btn" type="button" data-replay-issue="${esc(i.public_id)}">Inspect</button>
          <a href="${esc(i.share_url)}"><code>${esc(i.public_id)}</code></a> · <strong>${esc(i.title || 'Untitled issue')}</strong><br>
          <span class="empty">${esc(i.status)} · ${esc(i.severity)} · affected=${esc(i.affected_count)} · users=${esc(i.affected_users)} · ticket=${esc(i.external_ticket_state || 'none')}</span>
        </li>`).join('');
      const sessionRows = sessions.map(s => `
        <li data-session-status="${esc(s.status)}"><button class="btn" type="button" data-replay-session="${esc(s.stable_id)}">Play</button>
          <a href="${esc(s.share_url)}"><code>${esc(s.public_id)}</code></a> · ${esc(s.stable_id)}<br>
          <span class="empty">${esc(s.status)} · events=${esc(s.event_count)} · ${esc(s.last_seen_at)} · ${esc(JSON.stringify(s.preview || {}))}</span>
        </li>`).join('');
      byId('replayDashboard').innerHTML = `
        <h3>Replay Dashboard</h3>
        <div><button class="btn" id="processReplayJobsBtn" type="button">Process Queued Replays</button> <span class="empty" id="replayProcessStatus">${esc(processStatus)}</span></div>
        <div class="grid" style="margin-top:10px">
          <div><div class="lbl">Replay-backed Issues</div>
            <select id="issueStatusFilter" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
              <option value="">All statuses</option>${issueOptions}
            </select>
            ${issueRows ? `<ul id="replayIssueList">${issueRows}</ul>` : '<div class="empty">No replay issues yet.</div>'}
          </div>
          <div><div class="lbl">Recent Sessions</div>
            <select id="sessionStatusFilter" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
              <option value="">All statuses</option>${sessionOptions}
            </select>
            ${sessionRows ? `<ul id="replaySessionList">${sessionRows}</ul>` : '<div class="empty">No first-party replay sessions yet.</div>'}
          </div>
        </div>
        <div style="height:10px"></div>
        <div id="replayIssueDetail"><div class="empty">Select a replay-backed issue.</div></div>
        <div style="height:10px"></div>
        <div class="rr"><div id="firstPartyReplay"><div class="empty">Select a first-party replay session.</div></div></div>
      `;
      byId('processReplayJobsBtn').addEventListener('click', processReplayJobs);
      byId('replayDashboard').querySelectorAll('[data-replay-session]').forEach(el => {
        el.addEventListener('click', () => loadFirstPartyReplay(el.dataset.replaySession));
      });
      byId('replayDashboard').querySelectorAll('[data-replay-issue]').forEach(el => {
        el.addEventListener('click', () => renderReplayIssueDetail(issues.find(i => i.public_id === el.dataset.replayIssue)));
      });
      byId('issueStatusFilter')?.addEventListener('change', ev => filterReplayRows('replayIssueList', 'issueStatus', ev.target.value));
      byId('sessionStatusFilter')?.addEventListener('change', ev => filterReplayRows('replaySessionList', 'sessionStatus', ev.target.value));
      applyReplayHash(issues, sessions);
    }

    function filterReplayRows(listId, dataKey, value){
      const list = byId(listId);
      if(!list) return;
      list.querySelectorAll('li').forEach(row => {
        row.style.display = !value || row.dataset[dataKey] === value ? '' : 'none';
      });
    }

    function renderReplayIssueDetail(issue){
      const root = byId('replayIssueDetail');
      if(!root || !issue){ return; }
      const steps = (issue.reproduction_steps || []).map(s => `<li>${esc(s)}</li>`).join('');
      const sessions = (issue.sessions || []).map(s =>
        `<li><button class="btn" type="button" data-replay-session="${esc(s.session_id)}">Play</button> <code>${esc(s.session_id)}</code> · ${esc(s.role)}</li>`
      ).join('');
      root.innerHTML = `
        <h3>${esc(issue.public_id)} · ${esc(issue.title || 'Replay issue')}</h3>
        <div class="empty">${esc(issue.status)} · ${esc(issue.severity)} · affected=${esc(issue.affected_count)} · users=${esc(issue.affected_users)}</div>
        <div class="lbl">Summary</div><div>${esc(issue.summary || '')}</div>
        <div class="lbl">Likely Cause</div><div>${esc(issue.likely_cause || '')}</div>
        <div class="lbl">Reproduction Steps</div>${steps ? `<ul>${steps}</ul>` : '<div class="empty">No steps generated yet.</div>'}
        <div class="lbl">Sessions</div>${sessions ? `<ul>${sessions}</ul>` : '<div class="empty">No linked sessions.</div>'}
        <div class="lbl">External Ticket</div>
        <div class="empty">${issue.external_ticket_url ? `<a href="${esc(issue.external_ticket_url)}" target="_blank">${esc(issue.external_ticket_id || issue.external_ticket_url)}</a>` : esc(issue.external_ticket_state || 'none')}</div>
        <div class="lbl">Signals</div><pre>${esc(JSON.stringify(issue.signal_summary || {}, null, 2))}</pre>
      `;
      root.querySelectorAll('[data-replay-session]').forEach(el => {
        el.addEventListener('click', () => loadFirstPartyReplay(el.dataset.replaySession));
      });
    }

    function applyReplayHash(issues, sessions){
      const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
      const issueId = hash.get('issue');
      const replayId = hash.get('replay');
      if(issueId){
        renderReplayIssueDetail(issues.find(i => i.public_id === issueId));
      }
      if(replayId){
        const session = sessions.find(s => s.public_id === replayId);
        if(session) loadFirstPartyReplay(session.stable_id);
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
          <div class=\"card\"><h3>Codex Prompt <button class=\"btn\" onclick=\"copyPrompt('codex')\">Copy</button></h3><pre>${esc(codex)}</pre></div>
          <div class=\"card\"><h3>Claude Prompt <button class=\"btn\" onclick=\"copyPrompt('claude_code')\">Copy</button></h3><pre>${esc(claude)}</pre></div>
        </div>`;
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
                        auth_required=bool(settings["tester_auth_required"]),
                        auth_mode=str(settings["tester_auth_mode"] or "none"),
                        auth_login_url=str(settings["tester_auth_login_url"] or ""),
                        auth_username=str(settings["tester_auth_username"] or ""),
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
                status = 200 if result.ok else 400
                self._json({"ok": result.ok, "result": result.__dict__}, status=status)
                return

            if path == "/api/replays/process":
                from retrace.replay_core import process_queued_replay_jobs

                body = self._read_json_body()
                try:
                    limit_v = max(1, min(int(body.get("limit") or 25), 100))
                except (TypeError, ValueError):
                    limit_v = 25
                result = process_queued_replay_jobs(store=store, limit=limit_v)
                self._json(
                    {
                        "ok": True,
                        "result": {
                            "jobs_seen": result.jobs_seen,
                            "jobs_processed": result.jobs_processed,
                            "jobs_failed": result.jobs_failed,
                            "sessions_processed": result.sessions_processed,
                            "issues_created_or_updated": result.issues_created_or_updated,
                        },
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
