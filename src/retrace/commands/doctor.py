from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
import httpx

from retrace.config import load_config
from retrace.errors import format_user_error
from retrace.llm.client import build_llm_http_request


_OK = "ok"
_FAIL = "fail"
_WARN = "warn"


def _ok(name: str, detail: str) -> tuple[str, str, str]:
    return name, _OK, detail


def _fail(name: str, detail: str) -> tuple[str, str, str]:
    return name, _FAIL, detail


def _warn(name: str, detail: str) -> tuple[str, str, str]:
    return name, _WARN, detail


@click.command("doctor")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def doctor_command(config_path: Path) -> None:
    """Validate config + connectivity. Exits non-zero on any failure."""
    cfg = load_config(config_path)
    checks: list[tuple[str, str, str]] = []

    try:
        url = f"{cfg.posthog.host.rstrip('/')}/api/projects/{cfg.posthog.project_id}/"
        with httpx.Client(timeout=15) as c:
            resp = c.get(
                url, headers={"Authorization": f"Bearer {cfg.posthog.api_key}"}
            )
            resp.raise_for_status()
        checks.append(_ok("PostHog", f"reached {url}"))
    except Exception as exc:
        checks.append(_fail("PostHog", format_user_error(exc)))

    try:
        url, headers, body = build_llm_http_request(
            provider=cfg.llm.provider,
            base_url=cfg.llm.base_url,
            model=cfg.llm.model,
            api_key=cfg.llm.api_key,
            system="You are a test assistant.",
            user="reply with ping",
            temperature=0.0,
            response_json=False,
            max_tokens=8,
        )
        with httpx.Client(timeout=30) as c:
            resp = c.post(url, headers=headers, json=body)
            resp.raise_for_status()
        checks.append(_ok("LLM", f"{cfg.llm.provider} reached {url}"))
    except Exception as exc:
        checks.append(_fail("LLM", format_user_error(exc)))

    try:
        cfg.run.output_dir.mkdir(parents=True, exist_ok=True)
        test_path = cfg.run.output_dir / ".retrace_doctor_test"
        test_path.write_text("ok")
        test_path.unlink()
        checks.append(_ok("Output dir writable", str(cfg.run.output_dir)))
    except Exception as exc:
        checks.append(_fail("Output dir writable", str(exc)))

    if cfg.linear.enabled:
        try:
            with httpx.Client(timeout=15) as c:
                resp = c.post(
                    cfg.linear.endpoint,
                    headers={
                        "Authorization": cfg.linear.api_key,
                        "Content-Type": "application/json",
                    },
                    json={"query": "{ viewer { id name } }"},
                )
                resp.raise_for_status()
                payload = resp.json()
            if payload.get("errors"):
                raise RuntimeError(str(payload["errors"]))
            viewer = (payload.get("data") or {}).get("viewer") or {}
            checks.append(_ok("Linear sink", f"viewer={viewer.get('name', '?')}"))
        except Exception as exc:
            checks.append(_fail("Linear sink", format_user_error(exc)))

    if cfg.github_sink.enabled:
        try:
            with httpx.Client(timeout=15) as c:
                resp = c.get(
                    f"{cfg.github_sink.base_url.rstrip('/')}/user",
                    headers={
                        "Authorization": f"Bearer {cfg.github_sink.api_key}",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "Accept": "application/vnd.github+json",
                    },
                )
                resp.raise_for_status()
                user = resp.json()
            checks.append(_ok("GitHub sink", f"login={user.get('login', '?')}"))
        except Exception as exc:
            checks.append(_fail("GitHub sink", format_user_error(exc)))

    if cfg.notifications.enabled:
        if cfg.notifications.webhook_url.strip():
            try:
                with httpx.Client(timeout=10) as c:
                    resp = c.head(
                        cfg.notifications.webhook_url, follow_redirects=True
                    )
                detail = (
                    f"HEAD {cfg.notifications.webhook_url} -> {resp.status_code}"
                )
                if 200 <= resp.status_code < 400:
                    checks.append(_ok("Notifications: webhook", detail))
                elif resp.status_code == 405:
                    checks.append(
                        _warn(
                            "Notifications: webhook",
                            detail
                            + " (HEAD not allowed; endpoint may still accept POST).",
                        )
                    )
                else:
                    checks.append(_fail("Notifications: webhook", detail))
            except Exception as exc:
                checks.append(_fail("Notifications: webhook", format_user_error(exc)))
        if cfg.notifications.slack_webhook_url.strip():
            url = cfg.notifications.slack_webhook_url
            if url.startswith("https://hooks.slack.com/"):
                checks.append(
                    _ok(
                        "Notifications: slack",
                        "URL configured (Slack rejects HEAD; relying on shape check).",
                    )
                )
            else:
                checks.append(
                    _fail(
                        "Notifications: slack",
                        f"unexpected URL {url!r}; expected https://hooks.slack.com/...",
                    )
                )

    needs_browser_runtime = _spec_needs_browser_runtime(cfg)
    try:
        import importlib

        importlib.import_module("playwright.sync_api")
        checks.append(_ok("Playwright runtime", "playwright.sync_api importable"))
    except ImportError:
        msg = (
            "playwright extra not installed; install with `pip install retrace[browser]`"
        )
        if needs_browser_runtime:
            checks.append(
                _fail(
                    "Playwright runtime",
                    msg
                    + " — required for at least one configured spec (native browser actions or explore engine).",
                )
            )
        else:
            checks.append(
                _warn(
                    "Playwright runtime",
                    msg
                    + " (no browser specs configured yet, so this only blocks explore/native browser runs).",
                )
            )
    except Exception as exc:
        # Playwright present but unusable (broken native libs, missing
        # browsers, etc.) — surface the failure rather than crashing doctor.
        msg = f"playwright present but failed to import: {exc}"
        if needs_browser_runtime:
            checks.append(_fail("Playwright runtime", msg))
        else:
            checks.append(_warn("Playwright runtime", msg))

    # ----- Per-pillar QA-incident pipeline checks -----
    # The four-pillar promise: replay / UI test / API test / error monitor
    # / PR review all feed a single qa_incidents queue. Surface where each
    # pillar stands so a fresh install knows which inputs are still
    # zeroed out.
    try:
        from retrace.storage import Storage

        store = Storage(cfg.run.data_dir / "retrace.db")
        store.init_schema()
        checks.append(_ok("Local store", f"opened {cfg.run.data_dir / 'retrace.db'}"))

        qa_rows = store.list_qa_incidents(limit=500)
        if qa_rows:
            by_source: dict[str, int] = {}
            for row in qa_rows:
                kind = str(row["primary_source_kind"] or "unknown")
                by_source[kind] = by_source.get(kind, 0) + 1
            summary = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items()))
            checks.append(_ok("QA incidents", f"{len(qa_rows)} row(s): {summary}"))
        else:
            checks.append(
                _warn(
                    "QA incidents",
                    "no incidents yet — run `retrace demo all` to seed every pillar.",
                )
            )

        # Pillar 1: replay capture. The SDK key is the entry signal.
        sdk_keys = _safe_call(lambda: store.list_sdk_keys())
        if sdk_keys:
            checks.append(
                _ok(
                    "Replay capture",
                    f"{len(sdk_keys)} SDK key(s); paste `<script>` snippet from `retrace quickstart`.",
                )
            )
        else:
            checks.append(
                _warn(
                    "Replay capture",
                    "no SDK keys — run `retrace quickstart` or `retrace api create-sdk-key`.",
                )
            )

        # Pillar 2: UI testing. At least one saved spec means the surface is alive.
        try:
            from retrace.tester import list_specs as _list_ui_specs
            from retrace.tester import specs_dir_for_data_dir as _ui_specs_dir

            ui_specs = _list_ui_specs(_ui_specs_dir(cfg.run.data_dir))
            checks.append(
                _ok("UI testing", f"{len(ui_specs)} tester spec(s)")
                if ui_specs
                else _warn(
                    "UI testing",
                    "no tester specs — try `retrace tester create` or `retrace demo seed`.",
                )
            )
        except Exception as exc:
            checks.append(_warn("UI testing", f"could not list specs: {exc}"))

        # Pillar 3: API testing. Mirror the same shape.
        try:
            from retrace.api_testing import api_specs_dir_for_data_dir, list_api_specs

            api_specs = list_api_specs(api_specs_dir_for_data_dir(cfg.run.data_dir))
            checks.append(
                _ok("API testing", f"{len(api_specs)} API spec(s)")
                if api_specs
                else _warn(
                    "API testing",
                    "no API specs — `retrace tester api-create` or `tester api-import-openapi`.",
                )
            )
        except Exception as exc:
            checks.append(_warn("API testing", f"could not list specs: {exc}"))

        # Pillar 4: error monitoring. Any monitor-source failure (Sentry
        # compat, OTel, generic webhook) lands in `failures` with
        # source_type in `monitor_incident`, `app_error`, etc.
        try:
            monitor_count = sum(
                1
                for row in qa_rows
                if str(row["primary_source_kind"] or "") == "error_monitor"
            )
            if monitor_count:
                checks.append(
                    _ok("Error monitoring", f"{monitor_count} monitor-derived incident(s)")
                )
            else:
                checks.append(
                    _warn(
                        "Error monitoring",
                        "no Sentry/OTel/monitor incidents — see `retrace api onboard-hosted` for a DSN.",
                    )
                )
        except Exception as exc:
            checks.append(_warn("Error monitoring", f"could not inspect: {exc}"))

        # Pillar 5: connected repo for fix-PR + PR review.
        repos = _safe_call(lambda: store.list_github_repos())
        if repos:
            names = ", ".join(getattr(r, "repo_full_name", "?") for r in repos[:3])
            extra = "" if len(repos) <= 3 else f" (+{len(repos) - 3} more)"
            checks.append(_ok("Connected repos", f"{names}{extra}"))
        else:
            checks.append(
                _warn(
                    "Connected repos",
                    "no repos connected — `retrace github connect --repo org/name --local-path …`.",
                )
            )

        # `gh` is required for the fix-PR step. Surface it explicitly.
        import shutil

        gh_path = shutil.which("gh")
        if gh_path:
            checks.append(_ok("gh CLI", f"found at {gh_path} (required for `qa fix --open-pr`)"))
        else:
            checks.append(
                _warn(
                    "gh CLI",
                    "not installed — fix-PRs will produce a prompt-only branch. Install: https://cli.github.com",
                )
            )

    except Exception as exc:
        checks.append(_fail("Pipeline pillars", format_user_error(exc)))

    any_fail = False
    for name, status, detail in checks:
        label = {"ok": "OK", "fail": "FAIL", "warn": "WARN"}[status]
        click.echo(f"  [{label}] {name}: {detail}")
        if status == _FAIL:
            any_fail = True

    if any_fail:
        sys.exit(1)


def _safe_call(fn: Any) -> Any:
    """Run a doctor probe and swallow exceptions so one bad subsystem
    doesn't mask the rest of the report."""
    try:
        return fn()
    except Exception:
        return None


def _spec_needs_browser_runtime(cfg: Any) -> bool:
    """Decide whether Playwright is a hard requirement for this config.

    Today: scan saved tester specs under data/ui-tests/specs/ and return
    True if any spec uses execution_engine in {explore, native} with browser
    actions, or has a browser_runtime override.  This stays best-effort —
    if loading specs raises, we conservatively return False so doctor stays
    a green-light tool for users who haven't built specs yet.
    """
    try:
        from retrace.tester import list_specs, specs_dir_for_data_dir

        specs_dir = specs_dir_for_data_dir(cfg.run.data_dir)
        for spec in list_specs(specs_dir):
            if spec.execution_engine == "explore":
                return True
            runtime = str(
                spec.browser_settings.get("runtime")
                or spec.browser_settings.get("browser_runtime")
                or ""
            ).strip().lower()
            if runtime == "playwright":
                return True
            if spec.execution_engine == "native":
                browser_actions = {
                    "click",
                    "type",
                    "keypress",
                    "wait",
                    "wait_for",
                    "hover",
                    "upload",
                    "drag",
                    "drop",
                    "select",
                    "scroll",
                }
                for step in spec.exact_steps:
                    action = str(step.get("action") or step.get("type") or "").lower()
                    if action in browser_actions:
                        return True
    except Exception:
        return False
    return False
