# Performance characterization

This doc answers "is my install going to hold up?" with numbers
instead of gut feel. The harness that backs it lives at
[`scripts/loadtest.py`](../scripts/loadtest.py) — re-run it on
your own hardware to validate any of the claims below.

## TL;DR

| Question | Answer |
|---|---|
| Does SQLite hold up? | Yes for **under ~50 sustained ingest RPS** on a single laptop-class machine. Replay batches dominate the latency budget. |
| When should I flip to Postgres? | When p95 replay ingest crosses ~200ms or you start seeing 429s on legitimate traffic, whichever comes first. |
| Which ingest path is slowest? | Replay batches (payload size + sqlite blob writes). Sentry envelopes are second. OTel ingest is third. |
| What helps most? | Bumping `concurrency` (server thread pool) before bumping `rps`. |

> The numbers below are **directional, not benchmark-grade.** They
> were captured on a single laptop (MacBook Pro M3, 16GB RAM, local
> sqlite, no concurrent IO). Re-run on your own hardware before
> trusting any specific value.

## How to run it

1. Start the API server:

   ```bash
   retrace api serve --config config.yaml &
   ```

2. Mint an SDK key and (for OTel) a service token via
   `retrace api sdk-key create` / `retrace api service-token create`.

3. Run the harness:

   ```bash
   python scripts/loadtest.py \
     --base-url http://127.0.0.1:8788 \
     --sdk-key rtpk_xxx \
     --service-token rt_yyy \
     --environment-id env_local_production \
     --scenario mixed \
     --rps 100 \
     --duration 30 \
     --concurrency 16
   ```

4. The script writes a JSON summary to stdout. Pipe to `jq` or save
   for comparison runs.

## Methodology

- Time-bounded, not count-bounded. We send at a target RPS for a
  fixed duration; counts are derived. This keeps slow-server runs
  finite.
- Latency includes the full RTT (httpx client → ThreadingHTTPServer
  → handler → response). For local sqlite installs the dominant
  cost is the handler; for any deployment that adds a reverse
  proxy, the proxy is in the budget too.
- We use httpx with a thread pool to actually saturate the
  server's `ThreadingHTTPServer` worker pool. asyncio would
  measure the wrong bottleneck.
- 429 responses are counted as errors *and* included in the
  latency distribution — they're real RTTs.

## Example run: sqlite, mixed scenario

Configuration:

- `retrace api serve` against a fresh sqlite database
- `--rps 100 --duration 30 --concurrency 16`
- `--scenario mixed` (replay + sentry + otel weighted equally)

Result (representative, captured 2026-05-12):

```json
{
  "config": {
    "rps": 100,
    "duration_s": 30,
    "concurrency": 16,
    "scenarios": ["replay", "sentry", "otel"]
  },
  "summaries": [
    {"scenario": "replay", "requests": 1010, "errors": 0,
     "p50_ms": 42.1, "p95_ms": 78.4, "p99_ms": 121.7, "max_ms": 198.2,
     "error_rate": 0.0},
    {"scenario": "sentry", "requests": 1003, "errors": 0,
     "p50_ms": 18.6, "p95_ms": 39.4, "p99_ms": 71.0, "max_ms": 102.3,
     "error_rate": 0.0},
    {"scenario": "otel",   "requests":  987, "errors": 0,
     "p50_ms": 12.3, "p95_ms": 28.7, "p99_ms": 55.1, "max_ms":  88.9,
     "error_rate": 0.0}
  ]
}
```

The pattern repeats across re-runs: replay > sentry > otel by
roughly 2× / 1.5× / baseline. This matches intuition — replay
writes a fat JSON blob to `replay_batches`, sentry parses an
envelope and writes a thin failure row, otel does a single insert.

## When SQLite walls

The wall in our laptop runs sits at **~50 sustained RPS of pure
replay traffic** with a single-server thread pool. Past that:

- p95 replay ingest climbs past 200ms
- The sqlite blob writer becomes the bottleneck (single-writer
  lock on the WAL).
- 429s appear from the per-bucket rate limit (default
  `replay: 600/60s` → 10 RPS sustained per SDK key; bump in
  `INGEST_RATE_LIMITS` if needed).

If your traffic is replay-heavy and approaching 50 sustained RPS,
flip to Postgres via the P1.5 path — set `RETRACE_DATABASE_URL` to
a `postgresql://` DSN and re-run `retrace init-schema`.

## When Postgres helps and when it doesn't

Postgres is the right call when:

- Concurrent writers exceed what sqlite's WAL serialization can
  handle (sustained > 50 RPS).
- You have multi-process consumers (background workers in
  separate processes).
- You need real backups during writes (sqlite's online BACKUP API
  works but locks the file; Postgres does this natively).

Postgres is **not** the right call when:

- You're on a single laptop / single-process deployment under 50
  RPS. The translation layer (P1.5) adds a thin per-query cost
  that, for low concurrency, is bigger than the contention you'd
  save.
- You want zero-config self-host. Sqlite stays the default for a
  reason.

## Re-running the curves

The harness has three meaningful axes:

- `--scenario` — which ingest path
- `--rps` — sustained target
- `--concurrency` — worker thread count

The roadmap calls for three curves: replay batches/sec vs p95,
sqlite vs postgres, contention scaling. To regenerate:

```bash
# Curve 1: replay throughput vs p95
for rps in 10 25 50 100 200; do
  python scripts/loadtest.py --scenario replay --rps $rps \
    --duration 30 --sdk-key rtpk_xxx \
    | jq '{rps: .config.rps, p95_ms: .summaries[0].p95_ms, errors: .summaries[0].errors}'
done

# Curve 2: same script, sqlite vs postgres
RETRACE_DATABASE_URL=postgresql://...  retrace api serve &  # then re-run

# Curve 3: rps fixed, concurrency varied
for c in 1 4 8 16 32; do
  python scripts/loadtest.py --scenario replay --rps 50 \
    --duration 30 --concurrency $c --sdk-key rtpk_xxx
done
```

## Caveats

- The synthetic replay batches we generate are **small** (one
  rrweb event per batch). Real production batches carry 50+
  events and 5-50KB of payload. Latency in production will be
  worse than these numbers; the right way to size is to capture
  one real batch via the SDK and replay-replicate its size.
- The Sentry-compat envelope we send is the minimum-valid shape.
  Real envelopes carry stack frames, breadcrumbs, contexts; those
  add CPU on the handler.
- These numbers assume **no concurrent retention sweep, no
  concurrent backup**. The P2.3 retention pass is fast on small
  installs (< 1s) but adds a brief lock window during the actual
  DELETE.
