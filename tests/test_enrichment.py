from datetime import datetime, timezone
from pathlib import Path

from retrace.config import (
    ClusterConfig,
    DetectorsConfig,
    LLMConfig,
    PostHogConfig,
    RetraceConfig,
    RunConfig,
)
from retrace.detectors.base import Signal
from retrace.enrichment import CorrelationEnricher
from retrace.sinks.base import Finding
from retrace.storage import SessionMeta, Storage


def _cfg(*, api_key: str = "phx_test") -> RetraceConfig:
    return RetraceConfig(
        posthog=PostHogConfig(host="https://us.i.posthog.com", project_id="42", api_key=api_key),
        llm=LLMConfig(provider="openai_compatible", base_url="http://localhost:8080/v1", model="x"),
        run=RunConfig(output_dir=Path("./reports"), data_dir=Path("./data")),
        detectors=DetectorsConfig(),
        cluster=ClusterConfig(),
    )


def _finding() -> Finding:
    return Finding(
        session_id="sess-1",
        session_url="https://us.i.posthog.com/project/42/replay/sess-1",
        title="t",
        severity="high",
        category="functional_error",
        what_happened="x",
        likely_cause="y",
    )


def _signals() -> list[Signal]:
    return [
        Signal(
            session_id="sess-1",
            detector="console_error",
            timestamp_ms=1_700_000_000_000,
            url="https://example.com",
            details={},
        )
    ]


def test_enricher_maps_public_ingest_to_private_query_host(tmp_path: Path):
    store = Storage(tmp_path / "r.db")
    store.init_schema()
    enricher = CorrelationEnricher(_cfg(), store)
    assert enricher.query_host == "https://us.posthog.com"


def test_enricher_best_effort_without_api_key(tmp_path: Path):
    store = Storage(tmp_path / "r.db")
    store.init_schema()
    store.upsert_session(
        SessionMeta(
            id="sess-1",
            project_id="42",
            started_at=datetime(2026, 4, 23, 0, 0, tzinfo=timezone.utc),
            duration_ms=1_000,
            distinct_id="u-1",
            event_count=10,
        )
    )
    enricher = CorrelationEnricher(_cfg(api_key=""), store)
    out = enricher.enrich(_finding(), _signals())
    assert out.distinct_id == "u-1"
    assert out.error_issue_ids == []
    assert out.trace_ids == []
    assert "/error_tracking?" in out.error_tracking_url
    assert "/logs?" in out.logs_url


def test_enricher_extracts_issue_and_trace_ids_from_query_rows(tmp_path: Path):
    store = Storage(tmp_path / "r.db")
    store.init_schema()
    store.upsert_session(
        SessionMeta(
            id="sess-1",
            project_id="42",
            started_at=datetime(2026, 4, 23, 0, 0, tzinfo=timezone.utc),
            duration_ms=1_000,
            distinct_id="u-1",
            event_count=10,
        )
    )

    class FakeEnricher(CorrelationEnricher):
        def _query_hogql_rows(self, *, query: str, name: str):  # type: ignore[override]
            if "exceptions" in name:
                return [
                    {
                        "timestamp": "2026-04-23T00:00:00Z",
                        "issue_id": "iss-1",
                        "trace_id": "tr-1",
                        "exception_list": [
                            {
                                "stacktrace": {
                                    "frames": [
                                        {
                                            "filename": "app.py",
                                            "function": "explode",
                                            "lineno": 11,
                                            "colno": 2,
                                        }
                                    ]
                                }
                            }
                        ],
                    }
                ]
            return [{"timestamp": "2026-04-23T00:00:01Z", "trace_id": "tr-2"}]

    enricher = FakeEnricher(_cfg(), store)
    out = enricher.enrich(_finding(), _signals())
    assert out.error_issue_ids == ["iss-1"]
    assert out.trace_ids == ["tr-1", "tr-2"]
    assert "explode" in out.top_stack_frame
    assert out.first_error_ts_ms > 0
    assert out.last_error_ts_ms >= out.first_error_ts_ms
