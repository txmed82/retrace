"""P3.4 — smoke tests for the loadtest harness.

We don't actually pump real traffic from CI (would hang on a port
that isn't listening); we verify the script's pure pieces (scenario
builder, percentile math, weighted scenario pick, no-scenario
error path) so the next "ship" of the harness doesn't ship a
broken build.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest


def _import_loadtest():
    """The script lives outside the package; import via spec."""
    if "loadtest" in sys.modules:
        return sys.modules["loadtest"]
    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "scripts" / "loadtest.py"
    spec = importlib.util.spec_from_file_location("loadtest", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["loadtest"] = module
    spec.loader.exec_module(module)
    return module


def test_percentile_math():
    loadtest = _import_loadtest()
    pct = loadtest._pct
    values = sorted([10.0, 20.0, 30.0, 40.0, 50.0])
    assert pct(values, 50) in (20.0, 30.0)  # nearest-rank can land either side
    assert pct(values, 100) == 50.0
    assert pct(values, 0) == 10.0
    assert pct([], 50) == 0.0


def test_replay_request_builder():
    loadtest = _import_loadtest()
    url, headers, body = loadtest._build_replay_request(
        "http://127.0.0.1:8788", "rtpk_test"
    )
    assert url.endswith("/api/sdk/replay")
    assert headers["X-Retrace-Key"] == "rtpk_test"
    assert "events" in body
    assert "session_id" in body


def test_sentry_request_builder():
    loadtest = _import_loadtest()
    url, headers, body = loadtest._build_sentry_request(
        "http://127.0.0.1:8788", "rtpk_test"
    )
    assert "/api/sentry/rtpk_test/envelope" in url
    assert headers["X-Sentry-Auth"].startswith("Sentry sentry_key=")
    # Envelope body is a 3-line newline-separated payload.
    assert isinstance(body, bytes)
    assert b'"type": "event"' in body


def test_otel_request_builder():
    loadtest = _import_loadtest()
    url, headers, body = loadtest._build_otel_request(
        "http://127.0.0.1:8788", "rt_token", "env_prod"
    )
    assert "/api/otel/v1/logs" in url
    assert "environment_id=env_prod" in url
    assert headers["Authorization"] == "Bearer rt_token"
    assert body["logs"][0]["severity"] == "INFO"


def test_no_credentials_yields_no_scenarios():
    loadtest = _import_loadtest()
    args = argparse.Namespace(
        base_url="http://127.0.0.1:8788",
        scenario="mixed",
        sdk_key="",
        service_token="",
        environment_id="",
    )
    scenarios = loadtest._build_scenarios(args)
    assert scenarios == []


def test_scenario_filter_excludes_otel_when_no_token():
    loadtest = _import_loadtest()
    args = argparse.Namespace(
        base_url="http://127.0.0.1:8788",
        scenario="mixed",
        sdk_key="rtpk_x",
        service_token="",  # no OTel
        environment_id="",
    )
    names = {s.name for s in loadtest._build_scenarios(args)}
    assert names == {"replay", "sentry"}


def test_run_loadtest_raises_when_no_scenarios(monkeypatch):
    """A misconfigured run that produced zero scenarios must bail
    fast with a clear message — not spin a thread pool with
    nothing to do."""
    loadtest = _import_loadtest()
    args = argparse.Namespace(
        base_url="http://127.0.0.1:8788",
        scenario="mixed",
        sdk_key="",
        service_token="",
        environment_id="",
        rps=1,
        duration=1.0,
        concurrency=1,
    )
    with pytest.raises(SystemExit):
        loadtest.run_loadtest(args)


def test_weighted_pick_deterministic_when_only_one_scenario():
    loadtest = _import_loadtest()
    scenarios = [
        loadtest.ScenarioConfig(name="replay", builder=lambda: (None, None, None)),
    ]
    assert loadtest._weighted_pick(scenarios) is scenarios[0]


def test_scenario_report_summary_shape():
    loadtest = _import_loadtest()
    report = loadtest.ScenarioReport(scenario="replay")
    report.add(
        loadtest.RequestSample(scenario="replay", duration_s=0.05, status=200)
    )
    report.add(
        loadtest.RequestSample(scenario="replay", duration_s=0.10, status=200)
    )
    report.add(
        loadtest.RequestSample(scenario="replay", duration_s=0.20, status=500)
    )
    summary = report.summary()
    assert summary["scenario"] == "replay"
    assert summary["requests"] == 3
    assert summary["errors"] == 1
    assert summary["p50_ms"] > 0
    assert 0 < summary["error_rate"] <= 1
