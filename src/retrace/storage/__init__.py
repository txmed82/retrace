"""Retrace storage package — modularized from monolithic storage.py.

Modules:
- helpers.py  – constants, enums, and helper functions
- schema.py   – SQL DDL (SCHEMA string)
- models.py   – dataclass row/entity types
- blob.py     – ReplayBlobStore protocol + LocalReplayBlobStore
- core.py     – Storage class (main database access layer)
"""

from __future__ import annotations

# ── Constants ────────────────────────────────────────────────────────────────
from .helpers import (
    APP_ERROR_FAILURE_STATUS_BY_INCIDENT_STATUS,
    APP_ERROR_INCIDENT_STATUSES,
    FAILURE_TEST_COVERAGE_STATES,
    GITHUB_REVIEW_RUN_STATUSES,
    INGEST_RATE_LIMIT_MAX_IDENTITIES_PER_BUCKET,
    INGEST_RATE_LIMIT_RETENTION_SECONDS,
)

# ── Schema ───────────────────────────────────────────────────────────────────
from .schema import SCHEMA

# ── Dataclass models ─────────────────────────────────────────────────────────
from .models import (  # noqa: F401
    AlertRouteRow,
    AppErrorAlertRuleRow,
    AppErrorRetentionPruneResult,
    DeployMarkerRow,
    EvidenceRow,
    FailureRow,
    FailureTestLinkRow,
    FixPromptRow,
    GitHubRepoRow,
    GitHubReviewRunRow,
    IncidentLifecycleEventRow,
    IncidentRow,
    OtelEventRow,
    ProcessingJobUpdateResult,
    RateLimitDecision,
    RepairTaskRow,
    ReplayBatchResult,
    ReplayIssueUpsertResult,
    ReplayPlayback,
    ReportFindingRow,
    RunRow,
    SDKKeyRow,
    ServiceTokenRow,
    SessionMeta,
    SignalDefinitionRow,
    SourceMapRow,
    WorkspaceIds,
)

# ── Blob store ───────────────────────────────────────────────────────────────
from .blob import LocalReplayBlobStore, ReplayBlobStore

# ── Storage class (main database access layer) ───────────────────────────────
from .core import Storage

# ── Public API surface ───────────────────────────────────────────────────────
__all__ = [
    # Constants
    "APP_ERROR_FAILURE_STATUS_BY_INCIDENT_STATUS",
    "APP_ERROR_INCIDENT_STATUSES",
    "FAILURE_TEST_COVERAGE_STATES",
    "GITHUB_REVIEW_RUN_STATUSES",
    "INGEST_RATE_LIMIT_MAX_IDENTITIES_PER_BUCKET",
    "INGEST_RATE_LIMIT_RETENTION_SECONDS",
    # Schema
    "SCHEMA",
    # Models
    "AlertRouteRow",
    "AppErrorAlertRuleRow",
    "AppErrorRetentionPruneResult",
    "DeployMarkerRow",
    "EvidenceRow",
    "FailureRow",
    "FailureTestLinkRow",
    "FixPromptRow",
    "GitHubRepoRow",
    "GitHubReviewRunRow",
    "IncidentLifecycleEventRow",
    "IncidentRow",
    "OtelEventRow",
    "ProcessingJobUpdateResult",
    "RateLimitDecision",
    "RepairTaskRow",
    "ReplayBatchResult",
    "ReplayIssueUpsertResult",
    "ReplayPlayback",
    "ReportFindingRow",
    "RunRow",
    "SDKKeyRow",
    "ServiceTokenRow",
    "SessionMeta",
    "SignalDefinitionRow",
    "SourceMapRow",
    "WorkspaceIds",
    # Blob store
    "LocalReplayBlobStore",
    "ReplayBlobStore",
    # Storage
    "Storage",
]
