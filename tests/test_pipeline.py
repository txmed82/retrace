import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from retrace.config import (
    DetectorsConfig,
    LLMConfig,
    PostHogConfig,
    RetraceConfig,
    RunConfig,
)
from retrace.pipeline import run_pipeline
from retrace.storage import Storage

# Trigger detector self-registration on import.
import retrace.detectors.console_error  # noqa: F401
import retrace.detectors.network_5xx  # noqa: F401
import retrace.detectors.rage_click  # noqa: F401


def _make_cfg(tmp_path: Path) -> RetraceConfig:
    return RetraceConfig(
        posthog=PostHogConfig(host="https://ph", project_id="42", api_key="phx"),
        llm=LLMConfig(base_url="http://llm/v1", model="m", api_key=None),
        run=RunConfig(
            lookback_hours=6,
            max_sessions_per_run=10,
            output_dir=tmp_path / "reports",
            data_dir=tmp_path / "data",
        ),
        detectors=DetectorsConfig(console_error=True, network_5xx=True, rage_click=True),
    )


def test_run_pipeline_end_to_end_with_fake_llm_and_ingester(tmp_path: Path):
    cfg = _make_cfg(tmp_path)

    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    (tmp_path / "data" / "sessions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "sessions" / "sess-1.json").write_text(
        json.dumps(
            [
                {"type": 4, "timestamp": 0, "data": {"href": "https://x/checkout"}},
                {
                    "type": 6,
                    "timestamp": 500,
                    "data": {
                        "plugin": "rrweb/console@1",
                        "payload": {"level": "error", "payload": ["TypeError boom"]},
                    },
                },
            ]
        )
    )

    ingester = MagicMock()
    ingester.fetch_since.return_value = ["sess-1"]
    ingester.load_events.return_value = json.loads(
        (tmp_path / "data" / "sessions" / "sess-1.json").read_text()
    )

    llm_client = MagicMock()
    llm_client.chat_json.return_value = {
        "title": "Checkout crashes",
        "severity": "critical",
        "category": "functional_error",
        "what_happened": "TypeError shown after open.",
        "likely_cause": "Null ref.",
        "reproduction_steps": ["open /checkout"],
        "confidence": "high",
    }

    summary = run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
    )

    assert summary.sessions_scanned == 1
    assert summary.sessions_flagged == 1

    reports = list((tmp_path / "reports").glob("*.md"))
    assert len(reports) == 1
    text = reports[0].read_text()
    assert "Checkout crashes" in text
    assert "Critical" in text


def test_run_pipeline_skips_sessions_with_no_signals(tmp_path: Path):
    cfg = _make_cfg(tmp_path)

    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    ingester = MagicMock()
    ingester.fetch_since.return_value = ["sess-clean"]
    ingester.load_events.return_value = [
        {"type": 4, "timestamp": 0, "data": {"href": "https://x/home"}}
    ]

    llm_client = MagicMock()

    summary = run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
    )

    assert summary.sessions_scanned == 1
    assert summary.sessions_flagged == 0
    llm_client.chat_json.assert_not_called()
