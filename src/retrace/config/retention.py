# STAGE: core — Data retention TTL configuration.
from __future__ import annotations

from pydantic import BaseModel, Field


class RetentionConfig(BaseModel):
    """TTLs for the `retrace data retention apply` sweep.

    Defaults match `retention.RetentionPolicy` defaults; the policy
    dataclass is the source of truth at runtime, this Pydantic
    model is just the typed handle on `config.yaml`.

    `ge=1` (greater-than-or-equal) guards at the schema level — a
    user typo of `0` or a negative value gets caught at config-load
    time with a clean Pydantic error instead of being silently
    coerced by the downstream `max(1, ...)` in `_retention_interval`.
    """

    failures_days: int = Field(default=90, ge=1)
    evidence_days: int = Field(default=90, ge=1)
    source_maps_days: int = Field(default=30, ge=1)
    rate_limit_hours: int = Field(default=48, ge=1)
    replay_batches_days: int = Field(default=30, ge=1)
    otel_events_days: int = Field(default=30, ge=1)
    run_artifact_days: int = Field(default=30, ge=1)
