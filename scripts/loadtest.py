#!/usr/bin/env python3
"""P3.4 — synthetic ingest load test.

Pumps fake replay batches / Sentry envelopes / OTel events at a
running `retrace api serve` and reports per-scenario latency
percentiles + error rate.

This is the harness that backs `docs/perf.md`. Use it to answer:

  - "Is SQLite still fine at my traffic level, or do I need to
    flip the Postgres switch?"
  - "Which ingest path is the slowest one on my install?"
  - "How does p95 latency degrade as concurrency rises?"

Design notes:

  - Single-file, stdlib + `httpx` only. No new prod dependency.
  - Thread pool, not asyncio — keeps the script simple and
    actually pumps the server's `ThreadingHTTPServer` worker pool
    fairly. The bottleneck we want to measure is server-side, not
    asyncio scheduling on the client.
  - `--duration` time-bounded; we don't try to send a fixed count
    because the slow scenarios would never finish in CI.
  - Output is JSON to stdout (easy to grep / diff) plus a human
    summary to stderr. The roadmap's "publish three curves" is
    delivered by running this in three configurations and
    pasting the JSON into `docs/perf.md`.

Usage:

  $ retrace api serve --config config.yaml &
  $ python scripts/loadtest.py \\
      --base-url http://127.0.0.1:8788 \\
      --sdk-key rtpk_xxx \\
      --service-token rt_yyy \\
      --scenario mixed \\
      --rps 100 --duration 30 --concurrency 16

  $ python scripts/loadtest.py --help
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


@dataclass
class ScenarioConfig:
    name: str
    builder: Callable[[], tuple[str, dict, dict | bytes]]
    target_share: float = 1.0


@dataclass
class RequestSample:
    scenario: str
    duration_s: float
    status: int
    error: str = ""


@dataclass
class ScenarioReport:
    scenario: str
    requests: int = 0
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def add(self, sample: RequestSample) -> None:
        self.requests += 1
        if sample.error or sample.status >= 400 and sample.status != 429:
            self.errors += 1
        # Include 429s in the latency distribution — they're real
        # measured RTTs, just not "successful" requests.
        self.latencies_ms.append(sample.duration_s * 1000.0)

    def summary(self) -> dict[str, Any]:
        if not self.latencies_ms:
            return {
                "scenario": self.scenario,
                "requests": self.requests,
                "errors": self.errors,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
                "max_ms": 0.0,
                "error_rate": 0.0,
            }
        sorted_l = sorted(self.latencies_ms)
        return {
            "scenario": self.scenario,
            "requests": self.requests,
            "errors": self.errors,
            "p50_ms": round(_pct(sorted_l, 50), 2),
            "p95_ms": round(_pct(sorted_l, 95), 2),
            "p99_ms": round(_pct(sorted_l, 99), 2),
            "max_ms": round(sorted_l[-1], 2),
            "error_rate": round(self.errors / self.requests, 4),
        }


def _pct(sorted_values: list[float], pct: int) -> float:
    if not sorted_values:
        return 0.0
    if pct >= 100:
        return sorted_values[-1]
    if pct <= 0:
        return sorted_values[0]
    # Nearest-rank — good enough for an ops dashboard, doesn't
    # need scipy.
    k = max(0, min(len(sorted_values) - 1, int(round((pct / 100.0) * len(sorted_values))) - 1))
    return sorted_values[k]


def _build_replay_request(base_url: str, sdk_key: str) -> tuple[str, dict, dict]:
    body = {
        "session_id": f"sess-{uuid.uuid4().hex}",
        "sequence": int(time.time() * 1000) % 100_000,
        "flush_type": "normal",
        "distinct_id": "loadtest",
        "metadata": {},
        "events": [
            {"type": 0, "timestamp": int(time.time() * 1000), "data": {"loadtest": True}}
        ],
    }
    headers = {"X-Retrace-Key": sdk_key, "Content-Type": "application/json"}
    url = f"{base_url.rstrip('/')}/api/sdk/replay"
    return url, headers, body


def _build_sentry_request(base_url: str, sdk_key: str) -> tuple[str, dict, bytes]:
    # Sentry-compat envelope shape — minimal valid envelope so the
    # handler accepts it.
    event_id = uuid.uuid4().hex
    envelope_header = json.dumps({"event_id": event_id, "sent_at": _now_iso()})
    item_header = json.dumps({"type": "event"})
    item_body = json.dumps(
        {
            "event_id": event_id,
            "level": "error",
            "platform": "python",
            "message": "loadtest synthetic",
            "exception": {
                "values": [
                    {
                        "type": "LoadTestError",
                        "value": "synthetic",
                        "stacktrace": {"frames": []},
                    }
                ]
            },
        }
    )
    body = ("\n".join([envelope_header, item_header, item_body])).encode("utf-8")
    headers = {
        "X-Sentry-Auth": f"Sentry sentry_key={sdk_key}",
        "Content-Type": "application/x-sentry-envelope",
    }
    url = f"{base_url.rstrip('/')}/api/sentry/{sdk_key}/envelope"
    return url, headers, body


def _build_otel_request(base_url: str, service_token: str, environment_id: str) -> tuple[str, dict, dict]:
    body = {
        "logs": [
            {
                "trace_id": uuid.uuid4().hex,
                "span_id": uuid.uuid4().hex[:16],
                "timestamp_ms": int(time.time() * 1000),
                "severity": "INFO",
                "message": "loadtest synthetic",
            }
        ]
    }
    headers = {
        "Authorization": f"Bearer {service_token}",
        "Content-Type": "application/json",
    }
    url = f"{base_url.rstrip('/')}/api/otel/v1/logs?environment_id={environment_id}"
    return url, headers, body


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run_one(
    client: httpx.Client,
    scenario: ScenarioConfig,
) -> RequestSample:
    url, headers, body = scenario.builder()
    started = time.perf_counter()
    try:
        if isinstance(body, (bytes, bytearray)):
            response = client.post(url, headers=headers, content=body, timeout=30.0)
        else:
            response = client.post(url, headers=headers, json=body, timeout=30.0)
    except httpx.HTTPError as exc:
        return RequestSample(
            scenario=scenario.name,
            duration_s=time.perf_counter() - started,
            status=0,
            error=type(exc).__name__,
        )
    return RequestSample(
        scenario=scenario.name,
        duration_s=time.perf_counter() - started,
        status=response.status_code,
    )


def run_loadtest(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = _build_scenarios(args)
    if not scenarios:
        raise SystemExit(
            "no scenarios configured; pass at least one of --sdk-key / --service-token"
        )
    reports = {s.name: ScenarioReport(scenario=s.name) for s in scenarios}
    deadline = time.perf_counter() + max(1.0, float(args.duration))
    spacing_s = 1.0 / max(1, int(args.rps))
    with (
        httpx.Client() as client,
        ThreadPoolExecutor(max_workers=max(1, int(args.concurrency))) as pool,
    ):
        futures = []
        next_send = time.perf_counter()
        while time.perf_counter() < deadline:
            # Pick a scenario by weighted share.
            chosen = _weighted_pick(scenarios)
            futures.append(pool.submit(_run_one, client, chosen))
            next_send += spacing_s
            sleep = next_send - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
        # Drain remaining futures.
        for fut in as_completed(futures):
            sample = fut.result()
            reports[sample.scenario].add(sample)
    return {
        "config": {
            "base_url": args.base_url,
            "rps": args.rps,
            "duration_s": args.duration,
            "concurrency": args.concurrency,
            "scenarios": [s.name for s in scenarios],
        },
        "summaries": [r.summary() for r in reports.values()],
    }


def _build_scenarios(args: argparse.Namespace) -> list[ScenarioConfig]:
    out: list[ScenarioConfig] = []
    scenario = (args.scenario or "mixed").strip().lower()
    include_replay = scenario in {"replay", "mixed"} and args.sdk_key
    include_sentry = scenario in {"sentry", "mixed"} and args.sdk_key
    include_otel = scenario in {"otel", "mixed"} and args.service_token and args.environment_id
    if include_replay:
        out.append(
            ScenarioConfig(
                name="replay",
                builder=lambda: _build_replay_request(args.base_url, args.sdk_key),
            )
        )
    if include_sentry:
        out.append(
            ScenarioConfig(
                name="sentry",
                builder=lambda: _build_sentry_request(args.base_url, args.sdk_key),
            )
        )
    if include_otel:
        out.append(
            ScenarioConfig(
                name="otel",
                builder=lambda: _build_otel_request(
                    args.base_url, args.service_token, args.environment_id
                ),
            )
        )
    return out


def _weighted_pick(scenarios: list[ScenarioConfig]) -> ScenarioConfig:
    total = sum(s.target_share for s in scenarios) or 1.0
    roll = random.random() * total
    cumulative = 0.0
    for s in scenarios:
        cumulative += s.target_share
        if roll <= cumulative:
            return s
    return scenarios[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrace ingest load test (P3.4).")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8788",
        help="Target retrace api serve URL.",
    )
    parser.add_argument(
        "--scenario",
        default="mixed",
        choices=["replay", "sentry", "otel", "mixed"],
    )
    parser.add_argument("--sdk-key", default="", help="SDK key for replay / sentry ingest.")
    parser.add_argument(
        "--service-token",
        default="",
        help="Service token for OTel ingest (scope: otel:write or ingest).",
    )
    parser.add_argument(
        "--environment-id",
        default="",
        help="Environment id for OTel ingest scope.",
    )
    parser.add_argument(
        "--rps",
        type=int,
        default=50,
        help="Target requests per second.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Run duration in seconds.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Worker thread count.",
    )
    args = parser.parse_args(argv)
    report = run_loadtest(args)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
