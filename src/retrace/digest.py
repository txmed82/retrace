"""Daily/weekly digest of replay issue activity.

Generates a markdown rollup that a developer can read in 30 seconds:
new issues, regressed issues, resolved issues, and the top-impact bugs by
affected_count over a configurable lookback window.

The same module is also the source of truth for the "since" cursor: callers
that want only what's *new since the last digest* pass a starting timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from retrace.storage import Storage


@dataclass
class DigestIssueRow:
    public_id: str
    title: str
    severity: str
    status: str
    affected_count: int
    affected_users: int
    updated_at: str
    summary: str = ""

    @property
    def is_resolved(self) -> bool:
        return self.status == "resolved"

    @property
    def is_regressed(self) -> bool:
        return self.status == "regressed"

    @property
    def is_open(self) -> bool:
        return self.status in {"new", "ongoing", "regressed", "ticket_created"}


@dataclass
class DigestPayload:
    project_id: str
    environment_id: str
    window_start: str
    window_end: str
    new_issues: list[DigestIssueRow] = field(default_factory=list)
    regressed_issues: list[DigestIssueRow] = field(default_factory=list)
    resolved_issues: list[DigestIssueRow] = field(default_factory=list)
    top_impact_open: list[DigestIssueRow] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.new_issues
            or self.regressed_issues
            or self.resolved_issues
            or self.top_impact_open
        )


def _row_to_digest(row: Any) -> DigestIssueRow:
    return DigestIssueRow(
        public_id=str(row["public_id"]),
        title=str(row["title"] or "Replay issue"),
        severity=str(row["severity"] or ""),
        status=str(row["status"] or ""),
        affected_count=int(row["affected_count"] or 0),
        affected_users=int(row["affected_users"] or 0),
        updated_at=str(row["updated_at"] or ""),
        summary=str(row["summary"] or ""),
    )


def _within_window(updated_at: str, window_start: datetime) -> bool:
    """Decide whether an issue's `updated_at` falls inside the digest window.

    Storage writes timestamps via `datetime.now(timezone.utc).isoformat()`, so
    in practice every value here is tz-aware UTC.  We still keep a defensive
    fallback that treats naive timestamps as UTC; if a future backend ever
    starts writing local-time strings, those rows will be off by the local
    offset and need a timezone-aware migration before the window math is
    trustworthy.
    """
    if not updated_at:
        return False
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= window_start


def build_digest(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    lookback_hours: int = 24,
    top_impact_limit: int = 5,
    now: datetime | None = None,
) -> DigestPayload:
    now_dt = now or datetime.now(timezone.utc)
    window_start = now_dt - timedelta(hours=max(1, int(lookback_hours)))

    rows = store.list_replay_issues(
        project_id=project_id, environment_id=environment_id
    )
    digest_rows = [_row_to_digest(r) for r in rows]

    in_window = [r for r in digest_rows if _within_window(r.updated_at, window_start)]
    new_issues = [r for r in in_window if r.status == "new"]
    regressed_issues = [r for r in in_window if r.is_regressed]
    resolved_issues = [r for r in in_window if r.is_resolved]

    open_issues = [r for r in digest_rows if r.is_open]
    open_issues.sort(
        key=lambda r: (-r.affected_count, -r.affected_users, r.public_id)
    )

    return DigestPayload(
        project_id=project_id,
        environment_id=environment_id,
        window_start=window_start.isoformat(),
        window_end=now_dt.isoformat(),
        new_issues=_sort_by_impact(new_issues),
        regressed_issues=_sort_by_impact(regressed_issues),
        resolved_issues=_sort_by_impact(resolved_issues),
        top_impact_open=open_issues[: max(1, int(top_impact_limit))],
    )


def _sort_by_impact(rows: Iterable[DigestIssueRow]) -> list[DigestIssueRow]:
    return sorted(
        rows,
        key=lambda r: (-r.affected_count, -r.affected_users, r.public_id),
    )


def render_digest_markdown(digest: DigestPayload) -> str:
    lines: list[str] = []
    lines.append(
        f"# Retrace digest — {digest.project_id} / {digest.environment_id}"
    )
    lines.append(
        f"_Window: `{digest.window_start}` → `{digest.window_end}`_"
    )
    lines.append("")
    if digest.is_empty:
        lines.append("No replay activity in this window.")
        lines.append("")
        return "\n".join(lines)

    if digest.new_issues:
        lines.append(f"## :rotating_light: New issues ({len(digest.new_issues)})")
        lines.extend(_render_rows(digest.new_issues))
        lines.append("")

    if digest.regressed_issues:
        lines.append(f"## :warning: Regressed ({len(digest.regressed_issues)})")
        lines.extend(_render_rows(digest.regressed_issues))
        lines.append("")

    if digest.resolved_issues:
        lines.append(f"## :white_check_mark: Resolved ({len(digest.resolved_issues)})")
        lines.extend(_render_rows(digest.resolved_issues))
        lines.append("")

    if digest.top_impact_open:
        lines.append(
            f"## :fire: Top open issues by impact (top {len(digest.top_impact_open)})"
        )
        lines.extend(_render_rows(digest.top_impact_open, include_status=True))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_rows(
    rows: Iterable[DigestIssueRow], *, include_status: bool = False
) -> list[str]:
    out: list[str] = []
    for r in rows:
        suffix = ""
        if include_status:
            suffix = f" _[{r.status}]_"
        out.append(
            f"- `{r.public_id}` **{r.title}** — "
            f"{r.severity or 'unknown'}, "
            f"{r.affected_count} session(s), "
            f"{r.affected_users} user(s){suffix}"
        )
    return out


def write_digest_report(
    *,
    digest: DigestPayload,
    reports_dir: Path,
    timestamp: datetime | None = None,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now(timezone.utc)
    filename = f"digest-{ts.strftime('%Y%m%d-%H%M%S')}.md"
    path = reports_dir / filename
    path.write_text(render_digest_markdown(digest))
    return path
