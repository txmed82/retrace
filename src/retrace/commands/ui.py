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
    ok_url, safe_host, err, pinned_ip = _validate_base_url(host)
    if not ok_url:
        return {"configured": True, "reachable": False, "detail": err}

    url = f"{safe_host.rstrip('/')}/api/projects/{project_id}/"
    try:
        # Create transport that uses pinned IP to prevent TOCTOU/DNS rebinding
        parsed = urlparse(safe_host)
        transport = _create_pinned_transport(
            pinned_ip, parsed.hostname or "", parsed.scheme or ""
        )
        with httpx.Client(timeout=8, transport=transport) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code // 100 == 2:
            return {
                "configured": True,
                "reachable": True,
                "detail": f"OK ({r.status_code})",
            }
        return {
            "configured": True,
            "reachable": False,
            "detail": f"HTTP {r.status_code}",
        }
    except Exception as exc:
        return {"configured": True, "reachable": False, "detail": str(exc)}


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _validate_base_url(base_url: str) -> tuple[bool, str, str, str]:
    """Validate outbound model-provider URLs to reduce SSRF risk.

    Returns: (ok, normalized_url, error_message, pinned_ip)
    The pinned_ip is the first acceptable IP address resolved during validation,
    which must be used for the actual HTTP request to prevent DNS rebinding attacks.
    """
    raw = base_url.strip()
    if not raw:
        return False, "", "Base URL is required.", ""

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return False, "", "Base URL must use http or https.", ""
    if not parsed.hostname:
        return False, "", "Base URL must include a hostname.", ""

    if parsed.query or parsed.fragment:
        return False, "", "Base URL must not include query parameters or fragments.", ""

    default_port = 443 if scheme == "https" else 80
    port = parsed.port or default_port
    allow_internal = _truthy_env("RETRACE_ALLOW_INTERNAL_URLS")
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, "", f"Base URL hostname resolution failed: {exc}", ""

    pinned_ip = ""
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
                return (
                    False,
                    "",
                    f"Base URL resolves to a non-public address ({ip}). "
                    "Set RETRACE_ALLOW_INTERNAL_URLS=true to override.",
                    "",
                )
            # Pin the first acceptable IP
            if not pinned_ip:
                pinned_ip = sockaddr[0]
    else:
        # If internal URLs are allowed, pin the first resolved IP
        for _, _, _, _, sockaddr in infos:
            if sockaddr:
                pinned_ip = sockaddr[0]
                break

    if not pinned_ip:
        return False, "", "No acceptable IP addresses found for base URL.", ""

    normalized = f"{scheme}://{parsed.netloc}{parsed.path or ''}".rstrip("/")
    return True, normalized, "", pinned_ip


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
    ok_url, safe_base_url, err, pinned_ip = _validate_base_url(base_url)
    if not ok_url:
        return {"configured": True, "reachable": False, "detail": err}
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
        # Create transport that uses pinned IP to prevent TOCTOU/DNS rebinding
        parsed = urlparse(safe_base_url)
        transport = _create_pinned_transport(
            pinned_ip, parsed.hostname or "", parsed.scheme or ""
        )
        with httpx.Client(timeout=12, transport=transport) as c:
            r = c.post(url, headers=headers, json=body)
        if r.status_code // 100 == 2:
            return {
                "configured": True,
                "reachable": True,
                "detail": f"OK ({r.status_code})",
            }
        return {
            "configured": True,
            "reachable": False,
            "detail": f"HTTP {r.status_code}",
        }
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
    ok_url, safe_base_url, err, pinned_ip = _validate_base_url(base_url)
    if not ok_url:
        return {"ok": False, "error": err}
    try:
        # Create transport that uses pinned IP to prevent TOCTOU/DNS rebinding
        parsed = urlparse(safe_base_url)
        transport = _create_pinned_transport(
            pinned_ip, parsed.hostname or "", parsed.scheme or ""
        )

        # We can't pass transport to fetch_llm_models without modifying it,
        # so we inline the model fetching logic here with our secure transport
        from retrace.llm.client import _build_headers, _extract_model_ids

        headers = _build_headers(provider=p, api_key=api_key.strip() or None)
        url = f"{safe_base_url.rstrip('/')}/models"

        with httpx.Client(timeout=10, transport=transport) as c:
            resp = c.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        models = _extract_model_ids(payload)
        return {"ok": True, "models": models}
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
                "candidates": candidates,
                "prompts": prompts,
            }
        )
    return out


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
      };
      const res = await fetch('/api/settings', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const data = await res.json();
      if(!res.ok){ alert(data.error || 'Save failed'); return; }
      await loadOnboarding();
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
                _write_config(config_path, cfg)

                # Empty secret fields mean "keep existing" to avoid accidental secret clearing.
                if key_v:
                    env["RETRACE_POSTHOG_API_KEY"] = key_v
                if llm_key_v:
                    env["RETRACE_LLM_API_KEY"] = llm_key_v
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