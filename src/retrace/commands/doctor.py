from __future__ import annotations

import sys
from pathlib import Path

import click
import httpx

from retrace.config import load_config


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
    checks: list[tuple[str, bool, str]] = []

    try:
        url = f"{cfg.posthog.host.rstrip('/')}/api/projects/{cfg.posthog.project_id}/"
        with httpx.Client(timeout=15) as c:
            resp = c.get(url, headers={"Authorization": f"Bearer {cfg.posthog.api_key}"})
            resp.raise_for_status()
        checks.append(("PostHog", True, f"reached {url}"))
    except Exception as exc:
        checks.append(("PostHog", False, str(exc)))

    try:
        url = f"{cfg.llm.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if cfg.llm.api_key:
            headers["Authorization"] = f"Bearer {cfg.llm.api_key}"
        with httpx.Client(timeout=30) as c:
            resp = c.post(
                url,
                headers=headers,
                json={
                    "model": cfg.llm.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 4,
                },
            )
            resp.raise_for_status()
        checks.append(("LLM", True, f"reached {url}"))
    except Exception as exc:
        checks.append(("LLM", False, str(exc)))

    try:
        cfg.run.output_dir.mkdir(parents=True, exist_ok=True)
        test_path = cfg.run.output_dir / ".retrace_doctor_test"
        test_path.write_text("ok")
        test_path.unlink()
        checks.append(("Output dir writable", True, str(cfg.run.output_dir)))
    except Exception as exc:
        checks.append(("Output dir writable", False, str(exc)))

    all_ok = True
    for name, ok, detail in checks:
        status = "OK" if ok else "FAIL"
        click.echo(f"  [{status}] {name}: {detail}")
        if not ok:
            all_ok = False

    if not all_ok:
        sys.exit(1)
