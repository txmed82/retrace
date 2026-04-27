# Retrace Full-Service Architecture

This document pins the first cloud/OSS boundary for the hosted and self-hosted
roadmap.

## Product Boundary

Retrace OSS must remain a complete local or self-hosted product for:

- Browser replay capture and ingest
- Replay storage and playback
- Signal detection and issue clustering
- AI bug summaries and reproduction steps
- AI UI test specs and local test execution
- MCP access to local findings, replay issues, and test runs

Retrace Cloud can add managed operations around that core:

- Managed multi-tenant hosting
- Billing, plans, quotas, and overages
- Managed object storage and background workers
- Organization administration and support tooling
- Hosted notifications, integrations, and onboarding

Hosted-only code should depend on OSS core packages. OSS core packages should
not depend on billing, customer support, or managed observability providers.

## Runtime Shape

The current Python CLI remains the smallest install path. The next service
shape is:

- API: first-party replay ingest and read APIs
- Workers: replay finalization, signal detection, AI analysis, previews, tests
- UI: session list, replay player, issues, AI test runs
- Browser SDK: capture and send replay batches
- Storage: SQLite/filesystem for local use, database/object storage for cloud

Self-host deployments can run these in one Docker Compose stack. Cloud
deployments can split API, workers, UI, database, object storage, queues, and
browser runners independently.

## Data Boundary

All user data is scoped by:

- Organization
- Project
- Environment

Browser SDK keys are public, project/environment-scoped, and write-only. Secret
service tokens are separate and are required for read APIs, MCP access, and
administration.

## Compatibility Path

Existing PostHog-only installations map to one local organization, one default
project, and one production environment. Current `data/sessions/*.json` replay
storage can coexist with first-party replay batches while ingestion migrates.

The first implemented vertical slice is intentionally narrow:

- `retrace api create-sdk-key`
- `retrace api serve`
- `POST /api/sdk/replay`
- `@retrace/browser` SDK batch posting

This creates a working path for first-party replay capture without requiring the
hosted cloud stack to exist yet.
