"""Auto-reproduction: Incident -> TesterSpec -> tester run.

This module is the first half of the killer-demo flow:

    user bug  ->  AUTO-GENERATED TEST  ->  AI fix PR

Given an Incident (from any source), we translate its canonical reproduction
recipe into a `TesterSpec`, hand it to the existing tester runner, and update
the incident with the outcome. Callers should not need to know whether the
underlying engine is Browser Harness, native Playwright, or anything else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from retrace.qa_incidents import Incident, reproduction_prompt_for_incident
from retrace.storage import Storage
from retrace.tester import (
    DEFAULT_APP_URL,
    DEFAULT_HARNESS_COMMAND,
    TesterRunResult,
    TesterSpec,
    create_spec,
    run_spec,
    runs_dir_for_data_dir,
    specs_dir_for_data_dir,
)


log = logging.getLogger(__name__)


@dataclass
class ReproOutcome:
    """Result of attempting to reproduce an incident with a generated test."""

    incident_id: str
    spec_id: str
    run_id: str
    confirmed: bool
    status: str           # "confirmed" | "not_confirmed" | "error"
    exit_code: int
    run_dir: str
    summary: str
    harness_log_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Spec generation.
# ---------------------------------------------------------------------------


def _exact_steps_from_incident(inc: Incident) -> list[dict[str, Any]]:
    """Convert reproduction steps to the tester `exact_steps` shape.

    The native runner expects dicts with at least `action` and selectors.
    We only emit steps that have enough structure for a deterministic run;
    everything else is left for the natural-language prompt.
    """
    out: list[dict[str, Any]] = []
    for step in inc.reproduction:
        action = (step.action or "").lower()
        target = dict(step.target or {})
        if action == "navigate" and (step.url or target.get("url")):
            out.append({
                "action": "navigate",
                "url": step.url or target.get("url", ""),
                "description": step.description or "",
            })
        elif action == "click" and (target.get("selector") or target.get("text") or target.get("aria_label")):
            out.append({
                "action": "click",
                "target": target,
                "description": step.description or "",
            })
        elif action == "input" and target.get("selector"):
            out.append({
                "action": "input",
                "target": target,
                "value": step.value if step.value and step.value != "<masked>" else "",
                "description": step.description or "",
            })
        elif action == "assert":
            out.append({
                "action": "assert",
                "target": target,
                "description": step.description or "",
            })
    return out


def generate_spec_for_incident(
    *,
    inc: Incident,
    specs_dir: Path,
    app_url: str = "",
    harness_command: str = "",
    execution_engine: str = "harness",
) -> TesterSpec:
    """Build a saved TesterSpec that reproduces the given incident.

    The spec is intentionally named after the incident's public id so it's
    easy to find later (`data/ui-tests/specs/repro-INC-XXXX-*.json`).
    """
    target_app_url = (app_url or inc.app_url or inc.evidence.primary_url or DEFAULT_APP_URL).strip()
    prompt = reproduction_prompt_for_incident(inc)
    exact_steps = _exact_steps_from_incident(inc)

    name = f"repro-{inc.public_id}-{inc.title[:48]}".strip()
    spec = create_spec(
        specs_dir=specs_dir,
        name=name,
        prompt=prompt,
        app_url=target_app_url,
        start_command="",
        harness_command=harness_command or DEFAULT_HARNESS_COMMAND,
        mode="describe",
        execution_engine=execution_engine,
        exact_steps=exact_steps,
        fixtures={"retrace": {"incident_id": inc.id, "incident_public_id": inc.public_id}},
    )
    return spec


# ---------------------------------------------------------------------------
# End-to-end orchestration.
# ---------------------------------------------------------------------------


def _classify_outcome(run: TesterRunResult, exact_steps_count: int) -> tuple[bool, str, str]:
    """Decide whether the run confirmed the incident as still broken.

    We deliberately keep three outcomes, not two:
      - "confirmed":     a failed assertion (or, when the spec has exact
                         steps to drive, a non-zero runner exit) proves the
                         bug still surfaces.
      - "error":         the harness/runner itself crashed and we can't tell
                         whether the bug reproduces. We must NOT advance
                         into fix generation on this state.
      - "not_confirmed": clean run; bug didn't surface.
    """
    failed_assertions = [
        a for a in (run.assertion_results or [])
        if isinstance(a, dict) and not a.get("ok", True)
    ]
    if failed_assertions:
        first = failed_assertions[0]
        summary = (
            f"assertion `{first.get('assertion_type', '?')}` failed: "
            f"{first.get('message', '')}".strip()
        )
        return True, "confirmed", summary

    if run.exit_code != 0:
        # With exact steps we trust the runner to fail meaningfully on the
        # bug. Without them, a non-zero exit is more likely a harness or
        # bootstrap failure than a reproduction.
        if exact_steps_count > 0:
            msg = run.error or f"exit code {run.exit_code}"
            return True, "confirmed", msg
        msg = run.error or f"runner exited with code {run.exit_code}"
        return False, "error", msg

    if run.error:
        return False, "error", run.error

    return False, "not_confirmed", "test ran clean; bug did not surface"


def reproduce_incident(
    *,
    store: Storage,
    data_dir: Path,
    incident_id: str,
    app_url: str = "",
    harness_command: str = "",
    execution_engine: str = "harness",
) -> ReproOutcome:
    """Top-level: pick an incident, generate a spec, run it, update state."""

    row = store.get_qa_incident(incident_id)
    if row is None:
        raise ValueError(f"incident not found: {incident_id}")
    inc = Incident.from_row(row)

    store.update_qa_incident_state(inc.public_id, repro_status="running", status="reproducing")

    specs_dir = specs_dir_for_data_dir(data_dir)
    runs_dir = runs_dir_for_data_dir(data_dir)

    try:
        spec = generate_spec_for_incident(
            inc=inc,
            specs_dir=specs_dir,
            app_url=app_url,
            harness_command=harness_command,
            execution_engine=execution_engine,
        )
    except Exception as exc:
        store.update_qa_incident_state(
            inc.public_id,
            repro_status="error",
            status="open",
            repro_summary=f"spec generation failed: {exc}",
        )
        raise

    try:
        run_result = run_spec(spec=spec, runs_dir=runs_dir)
    except Exception as exc:
        log.warning("repro run errored: %s", exc)
        store.update_qa_incident_state(
            inc.public_id,
            repro_status="error",
            repro_spec_id=spec.spec_id,
            status="open",
            repro_summary=f"runner errored: {exc}",
        )
        raise

    confirmed, status, summary = _classify_outcome(run_result, len(spec.exact_steps))

    if status == "confirmed":
        new_incident_status = "reproduced"
    elif status == "error":
        # Leave the incident open; we couldn't determine reproduction.
        new_incident_status = "open"
    else:
        new_incident_status = "not_reproduced"
    store.update_qa_incident_state(
        inc.public_id,
        status=new_incident_status,
        repro_status=status,
        repro_spec_id=spec.spec_id,
        repro_run_id=run_result.run_id,
        repro_summary=summary,
    )

    return ReproOutcome(
        incident_id=inc.public_id,
        spec_id=spec.spec_id,
        run_id=run_result.run_id,
        confirmed=confirmed,
        status=status,
        exit_code=int(run_result.exit_code),
        run_dir=str(run_result.run_dir),
        summary=summary,
        harness_log_path=str(run_result.harness_log_path),
    )
