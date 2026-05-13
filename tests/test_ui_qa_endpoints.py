"""Integration test for the new `/api/qa-incidents` UI endpoints.

We boot the server in a background thread because the Handler is
defined inside `ui_command()` and isn't directly testable in isolation.
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from retrace.commands import ui as ui_module
from retrace.qa_incidents import (
    Incident,
    IncidentEvidence,
    ReproductionStep,
    make_fingerprint,
    make_public_id,
    utc_now_iso,
)
from retrace.storage import Storage


_CONFIG = """posthog:
  host: https://us.i.posthog.com
  project_id: demo
llm:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: x
run:
  data_dir: {data_dir}
  output_dir: {output_dir}
"""


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for(url: str, *, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status < 500:
                    return
        except Exception as exc:
            last_err = exc
            time.sleep(0.1)
    raise RuntimeError(f"server did not come up: {last_err}")


@pytest.fixture()
def running_ui(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG.format(
        data_dir=str(tmp_path / "data"),
        output_dir=str(tmp_path / "reports"),
    ))
    (tmp_path / ".env").write_text("RETRACE_POSTHOG_API_KEY=\n")

    # Seed one qa_incident so the endpoint has something to return.
    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()
    inc = Incident(
        id=secrets.token_hex(16),
        public_id=make_public_id(),
        project_id="local",
        environment_id="production",
        fingerprint=make_fingerprint(["ui-endpoint-test"]),
        title="Login fails under load",
        summary="POST /api/login returns 500 for some users.",
        suspected_cause="connection pool exhaustion",
        severity="high",
        confidence="high",
        status="open",
        primary_source_kind="error_monitor",
        sources=[],
        reproduction=[
            ReproductionStep(0, "navigate", "Open /login", url="http://app/login"),
            ReproductionStep(1, "click", "Submit"),
        ],
        expected_outcome="200",
        actual_outcome="500",
        app_url="http://app",
        evidence=IncidentEvidence(top_stack_frame="loginHandler at auth.ts:42"),
        affected_count=4,
        affected_users=3,
        first_seen_ms=0,
        last_seen_ms=0,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    store.upsert_qa_incident(inc.to_row())

    port = _free_port()
    server_thread = threading.Thread(
        target=lambda: ui_module.ui_command.callback(
            config_path=cfg,
            host="127.0.0.1",
            port=port,
            repo_full_name=None,
        ),
        daemon=True,
    )
    server_thread.start()
    base = f"http://127.0.0.1:{port}"
    _wait_for(f"{base}/api/qa-incidents")
    yield base, inc.public_id
    # Daemon thread exits when the test process tears down.


def test_qa_incidents_list_returns_unified_queue(running_ui):
    base, public_id = running_ui
    with urllib.request.urlopen(f"{base}/api/qa-incidents") as resp:
        body = json.loads(resp.read())
    assert body["count"] == 1
    inc = body["incidents"][0]
    assert inc["public_id"] == public_id
    assert inc["primary_source_kind"] == "error_monitor"
    assert inc["title"] == "Login fails under load"


def test_qa_incidents_list_filters_by_source(running_ui):
    base, _ = running_ui
    # Seeded incident is error_monitor; filter for replay returns nothing.
    with urllib.request.urlopen(f"{base}/api/qa-incidents?source=replay") as resp:
        body = json.loads(resp.read())
    assert body["count"] == 0


def test_qa_incident_detail_endpoint(running_ui):
    base, public_id = running_ui
    with urllib.request.urlopen(f"{base}/api/qa-incidents/{public_id}") as resp:
        body = json.loads(resp.read())
    inc = body["incident"]
    assert inc["public_id"] == public_id
    assert inc["title"] == "Login fails under load"
    # The detail endpoint returns the full row including the JSON-encoded
    # reproduction/evidence columns so the frontend can parse them.
    assert "reproduction_json" in inc


def test_qa_incident_detail_unknown_id_returns_404(running_ui):
    base, _ = running_ui
    req = urllib.request.Request(f"{base}/api/qa-incidents/INC-NOPE99")
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
        return
    pytest.fail("expected 404 for unknown public id")


def test_qa_incident_detail_rejects_invalid_id(running_ui):
    base, _ = running_ui
    req = urllib.request.Request(f"{base}/api/qa-incidents/not-an-inc-id")
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        return
    pytest.fail("expected 400 for invalid id")
