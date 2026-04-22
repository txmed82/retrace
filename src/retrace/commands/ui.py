from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import click
import httpx
import yaml

from retrace.reports.parser import parse_report_findings
from retrace.storage import Storage


def _default_config() -> dict[str, Any]:
    return {
        "posthog": {
            "host": "https://us.i.posthog.com",
            "project_id": "",
        },
        "llm": {
            "base_url": "http://localhost:8080/v1",
            "model": "llama-3.1-8b-instruct",
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
    files = sorted(report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
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
        return {"configured": False, "reachable": None, "detail": "Missing host/project/API key."}
    url = f"{host.rstrip('/')}/api/projects/{project_id}/"
    try:
        with httpx.Client(timeout=8) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code // 100 == 2:
            return {"configured": True, "reachable": True, "detail": f"OK ({r.status_code})"}
        return {"configured": True, "reachable": False, "detail": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"configured": True, "reachable": False, "detail": str(exc)}


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
            for c in store.list_code_candidates(finding_id=row.id, repo_id=chosen_repo.id):
                rationale = ""
                try:
                    rationale = (json.loads(str(c["rationale_json"])) or {}).get("rationale", "")
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

    function esc(s){ return String(s || \"\").replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c])); }
    function byId(id){ return document.getElementById(id); }

    function copyText(s){ navigator.clipboard.writeText(String(s || \"\")); }
    function copyPrompt(key){ if(active?.prompts?.[key]) copyText(active.prompts[key]); }

    async function saveSettings(ev){
      ev.preventDefault();
      const body = {
        posthog_host: byId('phHost').value,
        posthog_project_id: byId('phProject').value,
        posthog_api_key: byId('phKey').value,
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
      byId('onboarding').innerHTML = `
        <h3>Onboarding & Settings</h3>
        <form id=\"settingsForm\">
          <div class=\"lbl\">PostHog Host</div>
          <input id=\"phHost\" value=\"${esc(settings.posthog_host)}\" />
          <div class=\"lbl\">PostHog Project ID</div>
          <input id=\"phProject\" value=\"${esc(settings.posthog_project_id)}\" />
          <div class=\"lbl\">PostHog Personal API Key</div>
          <input id=\"phKey\" value=\"${esc(settings.posthog_api_key)}\" />
          <div style=\"margin-top:10px\"><button class=\"btn\" type=\"submit\">Save Settings</button></div>
        </form>
        <div style=\"margin-top:10px\" class=\"empty\">GitHub CLI: <span class=\"${gh.installed?'ok':'bad'}\">${gh.installed?'installed':'missing'}</span> · auth: <span class=\"${gh.authed?'ok':'bad'}\">${gh.authed?'ok':'not authed'}</span></div>
        <div class=\"empty\">PostHog check: <span class=\"${ph.reachable===true?'ok':(ph.reachable===false?'bad':'')}\">${ph.reachable===true?'reachable':(ph.reachable===false?'unreachable':'not configured')}</span> ${esc(ph.detail || '')}</div>
        ${!gh.installed ? `<div class=\"empty\">Run in terminal: <code>${esc(gh.commands?.install || 'brew install gh')}</code> <button class=\"btn\" onclick=\"copyText('${esc(gh.commands?.install || 'brew install gh')}')\">Copy</button></div>` : ''}
        ${gh.installed && !gh.authed ? `<div class=\"empty\">Run in terminal: <code>${esc(gh.commands?.login || 'gh auth login')}</code> <button class=\"btn\" onclick=\"copyText('${esc(gh.commands?.login || 'gh auth login')}')\">Copy</button></div>` : ''}
      `;
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
@click.option("--repo", "repo_full_name", default=None, help="Optional connected repo full name filter.")
def ui_command(config_path: Path, host: str, port: int, repo_full_name: Optional[str]) -> None:
    """Run local browser UI for onboarding + findings + rrweb replay."""

    env_path = config_path.parent / ".env"

    cfg_dict = _read_config(config_path)
    data_dir = Path(str(((cfg_dict.get("run") or {}).get("data_dir") or "./data")))
    output_dir = Path(str(((cfg_dict.get("run") or {}).get("output_dir") or "./reports")))

    store = Storage(data_dir / "retrace.db")
    store.init_schema()

    def current_settings() -> dict[str, str]:
        cfg = _read_config(config_path)
        env = _read_env(env_path)
        return {
            "posthog_host": str(((cfg.get("posthog") or {}).get("host") or "https://us.i.posthog.com")),
            "posthog_project_id": str(((cfg.get("posthog") or {}).get("project_id") or "")),
            "posthog_api_key": env.get("RETRACE_POSTHOG_API_KEY", ""),
        }

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
                s = current_settings()
                self._json(
                    {
                        "gh": _gh_checks(),
                        "posthog": _posthog_check(
                            s["posthog_host"],
                            s["posthog_project_id"],
                            s["posthog_api_key"],
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
                    if sessions_dir not in resolved.parents and resolved != sessions_dir:
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
                host_v = str(body.get("posthog_host", "")).strip() or "https://us.i.posthog.com"
                project_v = str(body.get("posthog_project_id", "")).strip()
                key_v = str(body.get("posthog_api_key", "")).strip()

                cfg = _read_config(config_path)
                cfg.setdefault("posthog", {})["host"] = host_v
                cfg.setdefault("posthog", {})["project_id"] = project_v
                _write_config(config_path, cfg)

                env = _read_env(env_path)
                env["RETRACE_POSTHOG_API_KEY"] = key_v
                env.setdefault("RETRACE_LLM_API_KEY", "")
                _write_env(env_path, env)

                self._json({"ok": True, "settings": current_settings()})
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