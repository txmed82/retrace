from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
import httpx

from retrace.config import load_config
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
        checks.append(_fail("PostHog", str(exc)))

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
        checks.append(_fail("LLM", str(exc)))

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
            checks.append(_fail("Linear sink", str(exc)))

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
            checks.append(_fail("GitHub sink", str(exc)))

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
                checks.append(_fail("Notifications: webhook", str(exc)))
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

    any_fail = False
    for name, status, detail in checks:
        label = {"ok": "OK", "fail": "FAIL", "warn": "WARN"}[status]
        click.echo(f"  [{label}] {name}: {detail}")
        if status == _FAIL:
            any_fail = True

    if any_fail:
        sys.exit(1)


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