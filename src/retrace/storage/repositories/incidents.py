from __future__ import annotations
import sqlite3
import json
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING
from retrace.evidence import PROMPT_SAFE_REDACTION_STATES
from ..helpers import (
    _SEVERITY_ORDER, _rollup_severity, _string_values,
    _normalize_app_error_incident_status, _id, _public_id, _dt,
    _safe_json_obj, _merge_string_lists, _parse_string_list_json,
    _parse_dict_list_json, APP_ERROR_FAILURE_STATUS_BY_INCIDENT_STATUS
)
from ..models import (
    FailureRow, EvidenceRow, IncidentRow, IncidentLifecycleEventRow,
    AppErrorAlertRuleRow, AlertRouteRow, DeployMarkerRow, SourceMapRow
)
from .base import BaseRepository

if TYPE_CHECKING:
    from ..core import Storage

class IncidentRepository(BaseRepository):
    def upsert_incident(
        self,
        *,
        project_id: str,
        environment_id: str,
        group_key: str,
        title: str,
        summary: str = "",
        severity: str = "medium",
        status: str = "open",
        metadata: Optional[dict[str, Any]] = None,
        reopen_resolved: bool = False,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        incident_id = _id("inc")
        public_id = _public_id("inc", project_id, environment_id, group_key)
        try:
            metadata_json = json.dumps(metadata or {}, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("incident metadata must be JSON-serializable") from exc
        clean_status = _normalize_app_error_incident_status(status)
        with self._conn() as conn:
            existing = conn.execute(
                """
                SELECT id, status
                FROM incidents
                WHERE project_id = ? AND environment_id = ? AND group_key = ?
                """,
                (project_id, environment_id, group_key),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO incidents
                (id, public_id, project_id, environment_id, group_key, title, summary,
                 severity, status, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, environment_id, group_key) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    severity = excluded.severity,
                    status = CASE
                        WHEN incidents.status IN ('resolved', 'ignored') AND ? = 1
                            THEN excluded.status
                        ELSE incidents.status
                    END,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    incident_id,
                    public_id,
                    project_id,
                    environment_id,
                    group_key,
                    title,
                    summary,
                    severity,
                    clean_status,
                    metadata_json,
                    now,
                    now,
                    1 if reopen_resolved else 0,
                ),
            )
            row = conn.execute(
                """
                SELECT id
                FROM incidents
                WHERE project_id = ? AND environment_id = ? AND group_key = ?
                """,
                (project_id, environment_id, group_key),
            ).fetchone()
            assert row is not None
            if (
                existing is not None
                and str(existing["status"] or "") in {"resolved", "ignored"}
                and clean_status == "open"
                and reopen_resolved
            ):
                self._append_incident_lifecycle_event(
                    conn,
                    incident_id=str(row["id"]),
                    project_id=project_id,
                    environment_id=environment_id,
                    from_status=str(existing["status"] or ""),
                    to_status="open",
                    actor_type="system",
                    actor_id="monitoring_ingest",
                    reason="new matching app-error failure reopened the incident",
                    metadata={"trigger": "ingest_regression"},
                    created_at=now,
                )
                conn.execute(
                    """
                    UPDATE failures
                    SET status = 'new',
                        updated_at = ?
                    WHERE id IN (
                        SELECT failure_id
                        FROM incident_failures
                        WHERE incident_id = ?
                    )
                      AND source_type = 'monitor_incident'
                      AND status IN ('resolved', 'ignored')
                    """,
                    (now, str(row["id"])),
                )
            return str(row["id"])

    def link_failure_to_incident(self, *, incident_id: str, failure_id: str) -> None:
        """Strictly attach a failure to an incident without changing existing ownership."""
        with self._conn() as conn:
            incident = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
            if incident is None:
                raise ValueError(f"unknown incident_id: {incident_id}")
            failure = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM failures
                WHERE id = ? OR public_id = ?
                """,
                (failure_id, failure_id),
            ).fetchone()
            if failure is None:
                raise ValueError(f"unknown failure_id: {failure_id}")
            if (
                str(incident["project_id"]) != str(failure["project_id"])
                or str(incident["environment_id"]) != str(failure["environment_id"])
            ):
                raise ValueError("incident and failure must belong to the same workspace")
            existing_link = conn.execute(
                """
                SELECT incident_id
                FROM incident_failures
                WHERE failure_id = ?
                """,
                (str(failure["id"]),),
            ).fetchone()
            if existing_link is not None:
                linked_incident_id = str(existing_link["incident_id"])
                if linked_incident_id != str(incident["id"]):
                    raise ValueError("failure is already linked to another incident")
                self._refresh_incident_rollup(
                    conn,
                    incident_id=linked_incident_id,
                )
                return
            conn.execute(
                """
                INSERT OR IGNORE INTO incident_failures
                (incident_id, failure_id)
                VALUES (?, ?)
                """,
                (str(incident["id"]), str(failure["id"])),
            )
            self._refresh_incident_rollup(conn, incident_id=str(incident["id"]))

    def move_failure_to_incident(self, *, incident_id: str, failure_id: str) -> None:
        """Attach a failure to this incident, replacing prior incident membership."""
        with self._conn() as conn:
            incident = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
            if incident is None:
                raise ValueError(f"unknown incident_id: {incident_id}")
            failure = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM failures
                WHERE id = ? OR public_id = ?
                """,
                (failure_id, failure_id),
            ).fetchone()
            if failure is None:
                raise ValueError(f"unknown failure_id: {failure_id}")
            if (
                str(incident["project_id"]) != str(failure["project_id"])
                or str(incident["environment_id"]) != str(failure["environment_id"])
            ):
                raise ValueError("incident and failure must belong to the same workspace")
            old_links = conn.execute(
                """
                SELECT incident_id
                FROM incident_failures
                WHERE failure_id = ?
                """,
                (str(failure["id"]),),
            ).fetchall()
            conn.execute(
                """
                DELETE FROM incident_failures
                WHERE failure_id = ?
                """,
                (str(failure["id"]),),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO incident_failures
                (incident_id, failure_id)
                VALUES (?, ?)
                """,
                (str(incident["id"]), str(failure["id"])),
            )
            for row in old_links:
                self._refresh_incident_rollup(
                    conn,
                    incident_id=str(row["incident_id"]),
                )
            self._refresh_incident_rollup(conn, incident_id=str(incident["id"]))

    def get_incident(self, incident_id: str) -> Optional[IncidentRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
        return self._incident_from_row(row) if row is not None else None

    def find_incident_by_group(
        self,
        *,
        project_id: str,
        environment_id: str,
        group_key: str,
    ) -> Optional[IncidentRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM incidents
                WHERE project_id = ? AND environment_id = ? AND group_key = ?
                """,
                (project_id, environment_id, group_key),
            ).fetchone()
        return self._incident_from_row(row) if row is not None else None

    def list_incidents(
        self,
        *,
        project_id: str,
        environment_id: str,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[IncidentRow]:
        params: list[object] = [project_id, environment_id]
        where = "project_id = ? AND environment_id = ?"
        if status is not None:
            where += " AND status = ?"
            params.append(status)
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM incidents
                WHERE {where}
                ORDER BY updated_at DESC, public_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def list_incident_failures(self, *, incident_id: str) -> list[FailureRow]:
        with self._conn() as conn:
            incident = conn.execute(
                """
                SELECT id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
            if incident is None:
                return []
            rows = conn.execute(
                """
                SELECT f.*
                FROM incident_failures inf
                JOIN failures f ON f.id = inf.failure_id
                WHERE inf.incident_id = ?
                ORDER BY f.updated_at DESC, f.public_id
                """,
                (str(incident["id"]),),
            ).fetchall()
        return [self._failure_from_row(row) for row in rows]

    def list_incident_failures_for_incidents(
        self,
        *,
        incident_ids: list[str],
    ) -> dict[str, list[FailureRow]]:
        clean_ids = [str(item).strip() for item in incident_ids if str(item).strip()]
        if not clean_ids:
            return {}
        placeholders = ",".join("?" for _ in clean_ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT inf.incident_id AS linked_incident_id, f.*
                FROM incident_failures inf
                JOIN failures f ON f.id = inf.failure_id
                WHERE inf.incident_id IN ({placeholders})
                ORDER BY f.updated_at DESC, f.public_id
                """,
                clean_ids,
            ).fetchall()
        grouped: dict[str, list[FailureRow]] = {incident_id: [] for incident_id in clean_ids}
        for row in rows:
            grouped.setdefault(str(row["linked_incident_id"]), []).append(
                self._failure_from_row(row)
            )
        return grouped

    def list_incident_evidence(
        self,
        *,
        incident_id: str,
        include_sensitive: bool = True,
    ) -> list[EvidenceRow]:
        resolved_incident_id = self._resolve_incident_id(incident_id)
        if not resolved_incident_id:
            return []
        where = "inf.incident_id = ?"
        params: list[object] = [resolved_incident_id]
        if not include_sensitive:
            placeholders = ",".join("?" for _ in PROMPT_SAFE_REDACTION_STATES)
            where += f" AND ev.redaction_state IN ({placeholders})"
            params.extend(PROMPT_SAFE_REDACTION_STATES)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT ev.*
                FROM incident_failures inf
                JOIN failure_evidence ev ON ev.failure_id = inf.failure_id
                WHERE {where}
                ORDER BY
                    ev.occurred_at_ms,
                    replace(replace(ev.created_at, ' ', 'T'), '+00:00', 'Z'),
                    ev.id
                """,
                params,
            ).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    def transition_app_error_incident(
        self,
        *,
        project_id: str,
        environment_id: str,
        incident_id: str,
        status: str,
        actor_type: str = "service_token",
        actor_id: str = "",
        reason: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> IncidentRow:
        clean_status = _normalize_app_error_incident_status(status)
        clean_actor_type = actor_type.strip()[:80] or "service_token"
        clean_actor_id = actor_id.strip()[:200]
        clean_reason = reason.strip()[:2000]
        if metadata is None:
            clean_metadata: dict[str, Any] = {}
        elif isinstance(metadata, dict):
            clean_metadata = metadata
        else:
            raise ValueError("lifecycle metadata must be a JSON object")
        try:
            metadata_json = json.dumps(clean_metadata, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("lifecycle metadata must be JSON-serializable") from exc
        failure_status = APP_ERROR_FAILURE_STATUS_BY_INCIDENT_STATUS[clean_status]
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute(
                """
                SELECT *
                FROM incidents
                WHERE (id = ? OR public_id = ?)
                  AND project_id = ?
                  AND environment_id = ?
                """,
                (incident_id, incident_id, project_id, environment_id),
            ).fetchone()
            if incident is None:
                raise ValueError(f"unknown incident_id: {incident_id}")
            resolved_incident_id = str(incident["id"])
            previous_status = str(incident["status"] or "")
            incident_metadata = dict(_safe_json_obj(incident["metadata_json"]))
            incident_metadata.update(
                {
                    "last_lifecycle_actor_type": clean_actor_type,
                    "last_lifecycle_actor_id": clean_actor_id,
                    "last_lifecycle_reason": clean_reason,
                    "last_lifecycle_at": now,
                }
            )
            conn.execute(
                """
                UPDATE incidents
                SET status = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_status,
                    json.dumps(incident_metadata, sort_keys=True),
                    now,
                    resolved_incident_id,
                ),
            )
            self._append_incident_lifecycle_event(
                conn,
                incident_id=resolved_incident_id,
                project_id=project_id,
                environment_id=environment_id,
                from_status=previous_status,
                to_status=clean_status,
                actor_type=clean_actor_type,
                actor_id=clean_actor_id,
                reason=clean_reason,
                metadata=clean_metadata,
                metadata_json=metadata_json,
                created_at=now,
            )
            conn.execute(
                """
                UPDATE failures
                SET status = ?,
                    updated_at = ?
                WHERE id IN (
                    SELECT failure_id
                    FROM incident_failures
                    WHERE incident_id = ?
                )
                  AND source_type = 'monitor_incident'
                """,
                (failure_status, now, resolved_incident_id),
            )
            self._refresh_incident_rollup(conn, incident_id=resolved_incident_id)
            row = conn.execute(
                """
                SELECT *
                FROM incidents
                WHERE id = ?
                """,
                (resolved_incident_id,),
            ).fetchone()
            assert row is not None
        return self._incident_from_row(row)

    def list_incident_lifecycle_events(
        self,
        *,
        incident_id: str,
        limit: int = 100,
    ) -> list[IncidentLifecycleEventRow]:
        resolved_incident_id = self._resolve_incident_id(incident_id)
        if not resolved_incident_id:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM incident_lifecycle_events
                WHERE incident_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (resolved_incident_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._incident_lifecycle_event_from_row(row) for row in rows]

    def _append_incident_lifecycle_event(
        self,
        conn: sqlite3.Connection,
        *,
        incident_id: str,
        project_id: str,
        environment_id: str,
        from_status: str,
        to_status: str,
        actor_type: str,
        actor_id: str,
        reason: str,
        metadata: Optional[dict[str, Any]] = None,
        metadata_json: str = "",
        created_at: str = "",
    ) -> None:
        if not metadata_json:
            metadata_json = json.dumps(metadata or {}, sort_keys=True)
        conn.execute(
            """
            INSERT INTO incident_lifecycle_events
            (id, incident_id, project_id, environment_id, from_status, to_status,
             actor_type, actor_id, reason, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id("ilc"),
                incident_id,
                project_id,
                environment_id,
                from_status,
                to_status,
                actor_type,
                actor_id,
                reason,
                metadata_json,
                created_at or datetime.now(timezone.utc).isoformat(),
            ),
        )

    def _resolve_incident_id(self, incident_id: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
        return str(row["id"]) if row is not None else ""

    def set_incident_repair_task(self, *, incident_id: str, repair_task_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            incident = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM incidents
                WHERE id = ? OR public_id = ?
                """,
                (incident_id, incident_id),
            ).fetchone()
            if incident is None:
                raise ValueError(f"unknown incident_id: {incident_id}")
            repair_task = conn.execute(
                """
                SELECT id, project_id, environment_id
                FROM repair_tasks
                WHERE id = ? OR public_id = ?
                """,
                (repair_task_id, repair_task_id),
            ).fetchone()
            if repair_task is None:
                raise ValueError(f"unknown repair_task_id: {repair_task_id}")
            if (
                str(incident["project_id"]) != str(repair_task["project_id"])
                or str(incident["environment_id"]) != str(repair_task["environment_id"])
            ):
                raise ValueError(
                    "incident and repair task must belong to the same workspace"
                )
            conn.execute(
                """
                UPDATE incidents
                SET repair_task_id = ?, updated_at = ?
                WHERE id = ? OR public_id = ?
                """,
                (str(repair_task["id"]), now, str(incident["id"]), str(incident["id"])),
            )

    def _incident_from_row(self, row: sqlite3.Row) -> IncidentRow:
        return IncidentRow(
            id=str(row["id"]),
            public_id=str(row["public_id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            group_key=str(row["group_key"]),
            title=str(row["title"] or ""),
            summary=str(row["summary"] or ""),
            severity=str(row["severity"] or "medium"),
            status=str(row["status"] or "open"),
            failure_count=int(row["failure_count"] or 0),
            evidence_count=int(row["evidence_count"] or 0),
            repair_task_id=str(row["repair_task_id"] or ""),
            metadata=dict(_safe_json_obj(row["metadata_json"])),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def _incident_lifecycle_event_from_row(
        self, row: sqlite3.Row
    ) -> IncidentLifecycleEventRow:
        return IncidentLifecycleEventRow(
            id=str(row["id"]),
            incident_id=str(row["incident_id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            from_status=str(row["from_status"] or ""),
            to_status=str(row["to_status"] or ""),
            actor_type=str(row["actor_type"] or ""),
            actor_id=str(row["actor_id"] or ""),
            reason=str(row["reason"] or ""),
            metadata=dict(_safe_json_obj(row["metadata_json"])),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
        )

    def upsert_app_error_alert_rule(
        self,
        *,
        project_id: str,
        environment_id: str,
        name: str,
        enabled: bool = True,
        precedence: int = 0,
        action: str = "alert",
        min_severity: str = "",
        provider: str = "",
        title_contains: str = "",
        fingerprint_contains: str = "",
        route_contains: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("alert rule name is required")
        clean_action = action.strip().lower() or "alert"
        if clean_action not in {"alert", "suppress"}:
            raise ValueError("alert rule action must be alert or suppress")
        clean_severity = min_severity.strip().lower()
        if clean_severity and clean_severity not in _SEVERITY_ORDER:
            raise ValueError("alert rule min_severity is invalid")
        if metadata is None:
            clean_metadata: dict[str, Any] = {}
        elif not isinstance(metadata, dict):
            raise ValueError("alert rule metadata must be a JSON object")
        else:
            clean_metadata = metadata
        try:
            metadata_json = json.dumps(clean_metadata, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("alert rule metadata must be JSON-serializable") from exc
        rule_id = _public_id("alertrule", project_id, environment_id, clean_name)
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO app_error_alert_rules
                (id, public_id, project_id, environment_id, name, enabled, precedence, action,
                 min_severity, provider, title_contains, fingerprint_contains,
                 route_contains, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, environment_id, name) DO UPDATE SET
                    enabled = excluded.enabled,
                    precedence = excluded.precedence,
                    action = excluded.action,
                    min_severity = excluded.min_severity,
                    provider = excluded.provider,
                    title_contains = excluded.title_contains,
                    fingerprint_contains = excluded.fingerprint_contains,
                    route_contains = excluded.route_contains,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    rule_id,
                    rule_id,
                    project_id,
                    environment_id,
                    clean_name,
                    int(bool(enabled)),
                    int(precedence),
                    clean_action,
                    clean_severity,
                    provider.strip().lower(),
                    title_contains.strip(),
                    fingerprint_contains.strip(),
                    route_contains.strip(),
                    metadata_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id
                FROM app_error_alert_rules
                WHERE project_id = ? AND environment_id = ? AND name = ?
                """,
                (project_id, environment_id, clean_name),
            ).fetchone()
            assert row is not None
            return str(row["id"])

    def list_app_error_alert_rules(
        self,
        *,
        project_id: str,
        environment_id: str,
        enabled: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AppErrorAlertRuleRow]:
        params: list[object] = [project_id, environment_id]
        where = "project_id = ? AND environment_id = ?"
        if enabled is not None:
            where += " AND enabled = ?"
            params.append(int(bool(enabled)))
        params.append(max(1, min(int(limit), 500)))
        params.append(max(0, int(offset)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM app_error_alert_rules
                WHERE {where}
                ORDER BY precedence DESC, created_at ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [self._app_error_alert_rule_from_row(row) for row in rows]

    def delete_app_error_alert_rule(
        self,
        *,
        project_id: str,
        environment_id: str,
        name: str,
    ) -> bool:
        """Delete one alert rule by (project, env, name). Returns True if a
        row was removed, False if nothing matched."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                DELETE FROM app_error_alert_rules
                WHERE project_id = ? AND environment_id = ? AND name = ?
                """,
                (project_id, environment_id, name.strip()),
            )
            return bool(cur.rowcount)

    def _app_error_alert_rule_from_row(
        self,
        row: sqlite3.Row,
    ) -> AppErrorAlertRuleRow:
        return AppErrorAlertRuleRow(
            id=str(row["id"]),
            public_id=str(row["public_id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            name=str(row["name"]),
            enabled=bool(row["enabled"]),
            precedence=int(row["precedence"] or 0),
            action=str(row["action"] or "alert"),
            min_severity=str(row["min_severity"] or ""),
            provider=str(row["provider"] or ""),
            title_contains=str(row["title_contains"] or ""),
            fingerprint_contains=str(row["fingerprint_contains"] or ""),
            route_contains=str(row["route_contains"] or ""),
            metadata=dict(_safe_json_obj(row["metadata_json"])),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    # ---- Alert routes (P1.1) -------------------------------------------

    def upsert_alert_route(
        self,
        *,
        project_id: str,
        environment_id: str,
        name: str,
        target_kind: str,
        target_url: str,
        target_secret: str = "",
        rule_name: str = "",
        min_severity: str = "",
        dedup_window_seconds: int = 300,
        enabled: bool = True,
    ) -> "AlertRouteRow":
        """Insert or update one route by `(project, env, name)`."""
        name = name.strip()
        if not name:
            raise ValueError("alert route name cannot be empty")
        target_kind = target_kind.strip().lower()
        if target_kind not in {"slack", "discord", "pagerduty", "webhook"}:
            raise ValueError(
                f"unsupported target_kind: {target_kind!r} "
                "(expected slack/discord/pagerduty/webhook)"
            )
        target_url = target_url.strip()
        if not target_url:
            raise ValueError("alert route target_url cannot be empty")
        # Validate `min_severity` + PagerDuty secret at write-time so
        # we fail loudly on misconfig instead of silently producing
        # 401s / wrong-rank dispatches at runtime. (CodeRabbit Major
        # catch on PR #131.)
        _ALLOWED_SEVERITIES = {"low", "medium", "high", "critical"}
        clean_min_severity = min_severity.strip().lower()
        if clean_min_severity and clean_min_severity not in _ALLOWED_SEVERITIES:
            raise ValueError(
                f"invalid min_severity: {min_severity!r} "
                f"(allowed: {sorted(_ALLOWED_SEVERITIES)})"
            )
        clean_target_secret = target_secret.strip()
        if target_kind == "pagerduty" and not clean_target_secret:
            raise ValueError(
                "pagerduty routes require target_secret "
                "(the Events v2 routing key)"
            )
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id, public_id FROM alert_routes "
                "WHERE project_id = ? AND environment_id = ? AND name = ?",
                (project_id, environment_id, name),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """
                    UPDATE alert_routes
                    SET enabled = ?, rule_name = ?, target_kind = ?,
                        target_url = ?, target_secret = ?, min_severity = ?,
                        dedup_window_seconds = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        int(bool(enabled)),
                        rule_name.strip(),
                        target_kind,
                        target_url,
                        clean_target_secret,
                        clean_min_severity,
                        max(0, int(dedup_window_seconds)),
                        existing["id"],
                    ),
                )
                row_id = str(existing["id"])
            else:
                row_id = _id("artr")
                public_id = _public_id(
                    "ARTR", project_id, environment_id, name
                )
                conn.execute(
                    """
                    INSERT INTO alert_routes
                        (id, public_id, project_id, environment_id, name,
                         enabled, rule_name, target_kind, target_url,
                         target_secret, min_severity, dedup_window_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id, public_id, project_id, environment_id, name,
                        int(bool(enabled)), rule_name.strip(), target_kind,
                        target_url, clean_target_secret,
                        clean_min_severity,
                        max(0, int(dedup_window_seconds)),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM alert_routes WHERE id = ?", (row_id,)
            ).fetchone()
        return self._alert_route_from_row(row)

    def list_alert_routes(
        self,
        *,
        project_id: str,
        environment_id: str,
        enabled: Optional[bool] = None,
        rule_name: Optional[str] = None,
    ) -> list["AlertRouteRow"]:
        params: list[object] = [project_id, environment_id]
        where = "project_id = ? AND environment_id = ?"
        if enabled is not None:
            where += " AND enabled = ?"
            params.append(int(bool(enabled)))
        if rule_name is not None:
            where += " AND (rule_name = ? OR rule_name = '')"
            params.append(rule_name.strip())
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM alert_routes WHERE {where} "
                "ORDER BY created_at ASC, id ASC",
                params,
            ).fetchall()
        return [self._alert_route_from_row(r) for r in rows]

    def get_alert_route(
        self,
        *,
        project_id: str,
        environment_id: str,
        name: str,
    ) -> Optional["AlertRouteRow"]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM alert_routes "
                "WHERE project_id = ? AND environment_id = ? AND name = ?",
                (project_id, environment_id, name.strip()),
            ).fetchone()
        return self._alert_route_from_row(row) if row is not None else None

    def delete_alert_route(
        self,
        *,
        project_id: str,
        environment_id: str,
        name: str,
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM alert_routes "
                "WHERE project_id = ? AND environment_id = ? AND name = ?",
                (project_id, environment_id, name.strip()),
            )
            return bool(cur.rowcount)

    def record_alert_dispatch(
        self,
        *,
        route_id: str,
        project_id: str,
        environment_id: str,
        fingerprint: str,
        status: str,
        target_kind: str,
        target_url: str,
        payload: dict,
        error: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO alert_dispatches
                    (route_id, project_id, environment_id, fingerprint,
                     status, error, target_kind, target_url, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route_id, project_id, environment_id, fingerprint,
                    status, error, target_kind, target_url,
                    json.dumps(payload or {}),
                ),
            )
            return int(cur.lastrowid or 0)

    def recent_alert_dispatch_for(
        self,
        *,
        route_id: str,
        fingerprint: str,
        within_seconds: int,
    ) -> Optional[sqlite3.Row]:
        """Find a successful dispatch for this (route, fingerprint) pair
        in the last `within_seconds` — used to dedup fast repeats."""
        within_seconds = max(0, int(within_seconds))
        if within_seconds <= 0:
            return None
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT * FROM alert_dispatches
                WHERE route_id = ?
                  AND fingerprint = ?
                  AND status = 'sent'
                  AND created_at >= datetime('now', ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (route_id, fingerprint, f"-{within_seconds} seconds"),
            ).fetchone()

    def list_recent_alert_dispatches(
        self,
        *,
        project_id: str,
        environment_id: str,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT * FROM alert_dispatches
                WHERE project_id = ? AND environment_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (project_id, environment_id, max(1, min(int(limit), 500))),
            ).fetchall()

    def _alert_route_from_row(self, row: sqlite3.Row) -> "AlertRouteRow":
        return AlertRouteRow(
            id=str(row["id"]),
            public_id=str(row["public_id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            name=str(row["name"]),
            enabled=bool(row["enabled"]),
            rule_name=str(row["rule_name"] or ""),
            target_kind=str(row["target_kind"]),
            target_url=str(row["target_url"]),
            target_secret=str(row["target_secret"] or ""),
            min_severity=str(row["min_severity"] or ""),
            dedup_window_seconds=int(row["dedup_window_seconds"] or 0),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
        )

    def _refresh_incident_rollup(
        self,
        conn: sqlite3.Connection,
        *,
        incident_id: str,
    ) -> None:
        rows = conn.execute(
            """
            SELECT severity, updated_at
            FROM incident_failures inf
            JOIN failures f ON f.id = inf.failure_id
            WHERE inf.incident_id = ?
            """,
            (incident_id,),
        ).fetchall()
        evidence_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM incident_failures inf
            JOIN failure_evidence ev ON ev.failure_id = inf.failure_id
            WHERE inf.incident_id = ?
            """,
            (incident_id,),
        ).fetchone()
        severity = _rollup_severity([str(row["severity"] or "") for row in rows])
        conn.execute(
            """
            UPDATE incidents
            SET failure_count = ?,
                evidence_count = ?,
                severity = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                int(evidence_count["count"] if evidence_count is not None else 0),
                severity,
                datetime.now(timezone.utc).isoformat(),
                incident_id,
            ),
        )

    def _refresh_incidents_for_failure(
        self,
        conn: sqlite3.Connection,
        *,
        failure_id: str,
    ) -> None:
        rows = conn.execute(
            """
            SELECT incident_id
            FROM incident_failures
            WHERE failure_id = ?
            """,
            (failure_id,),
        ).fetchall()
        for row in rows:
            self._refresh_incident_rollup(
                conn,
                incident_id=str(row["incident_id"]),
            )

    def _replace_failure_trace_map(
        self,
        conn: sqlite3.Connection,
        *,
        failure_id: str,
        metadata: dict[str, Any],
    ) -> None:
        conn.execute("DELETE FROM failure_trace_map WHERE failure_id = ?", (failure_id,))
        trace_ids = _string_values(metadata.get("trace_ids"))
        span_ids = _string_values(metadata.get("span_ids")) or [""]
        for trace_id in trace_ids:
            for span_id in span_ids:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO failure_trace_map
                    (failure_id, trace_id, span_id)
                    VALUES (?, ?, ?)
                    """,
                    (failure_id, trace_id, span_id),
                )

    def _backfill_failure_trace_map(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT id, metadata_json
            FROM failures
            WHERE id NOT IN (SELECT DISTINCT failure_id FROM failure_trace_map)
            """
        ).fetchall()
        for row in rows:
            self._replace_failure_trace_map(
                conn,
                failure_id=str(row["id"]),
                metadata=_safe_json_obj(row["metadata_json"]),
            )

    def record_deploy_marker(
        self,
        *,
        project_id: str,
        environment_id: str,
        sha: str,
        branch: str = "",
        author: str = "",
        deployed_at_ms: int = 0,
        changed_files: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        clean_sha = sha.strip()
        if not clean_sha:
            raise ValueError("sha is required")
        now = datetime.now(timezone.utc).isoformat()
        deploy_id = _id("dep")
        public_id = _public_id("dep", project_id, environment_id, clean_sha)
        changed_files_was_omitted = changed_files is None
        metadata_was_omitted = metadata is None
        clean_changed_files = _merge_string_lists(changed_files or [])
        try:
            metadata_json = json.dumps(metadata or {}, sort_keys=True)
            changed_files_json = json.dumps(clean_changed_files, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("deploy marker metadata must be JSON-serializable") from exc
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO deploy_markers
                (id, public_id, project_id, environment_id, sha, branch, author,
                 deployed_at_ms, changed_files_json, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, environment_id, sha) DO UPDATE SET
                    branch = excluded.branch,
                    author = excluded.author,
                    deployed_at_ms = excluded.deployed_at_ms,
                    changed_files_json = CASE
                        WHEN ? THEN deploy_markers.changed_files_json
                        ELSE excluded.changed_files_json
                    END,
                    metadata_json = CASE
                        WHEN ? THEN deploy_markers.metadata_json
                        ELSE excluded.metadata_json
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    deploy_id,
                    public_id,
                    project_id,
                    environment_id,
                    clean_sha,
                    branch.strip(),
                    author.strip(),
                    int(deployed_at_ms),
                    changed_files_json,
                    metadata_json,
                    now,
                    now,
                    int(changed_files_was_omitted),
                    int(metadata_was_omitted),
                ),
            )
            row = conn.execute(
                """
                SELECT id
                FROM deploy_markers
                WHERE project_id = ? AND environment_id = ? AND sha = ?
                """,
                (project_id, environment_id, clean_sha),
            ).fetchone()
            assert row is not None
            return str(row["id"])

    def get_deploy_marker(self, deploy_id: str) -> Optional[DeployMarkerRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM deploy_markers
                WHERE id = ? OR public_id = ? OR sha = ?
                ORDER BY deployed_at_ms DESC
                LIMIT 1
                """,
                (deploy_id, deploy_id, deploy_id),
            ).fetchone()
        return self._deploy_marker_from_row(row) if row is not None else None

    def get_deploy_marker_by_sha(
        self,
        *,
        project_id: str,
        environment_id: str,
        sha: str,
    ) -> Optional[DeployMarkerRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM deploy_markers
                WHERE project_id = ? AND environment_id = ? AND sha = ?
                """,
                (project_id, environment_id, sha),
            ).fetchone()
        return self._deploy_marker_from_row(row) if row is not None else None

    def list_deploy_markers(
        self,
        *,
        project_id: str,
        environment_id: str,
        limit: int = 50,
    ) -> list[DeployMarkerRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM deploy_markers
                WHERE project_id = ? AND environment_id = ?
                ORDER BY deployed_at_ms DESC, updated_at DESC
                LIMIT ?
                """,
                (project_id, environment_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._deploy_marker_from_row(row) for row in rows]

    def nearest_deploy_marker(
        self,
        *,
        project_id: str,
        environment_id: str,
        at_ms: int,
    ) -> Optional[DeployMarkerRow]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM deploy_markers
                WHERE project_id = ? AND environment_id = ?
                  AND deployed_at_ms <= ?
                ORDER BY deployed_at_ms DESC, updated_at DESC
                LIMIT 1
                """,
                (project_id, environment_id, max(0, int(at_ms))),
            ).fetchone()
        return self._deploy_marker_from_row(row) if row is not None else None


    def upsert_source_map(
        self,
        *,
        project_id: str,
        environment_id: str,
        release: str,
        artifact_url: str,
        source_map: dict[str, Any],
        dist: str = "",
    ) -> str:
        clean_release = release.strip()
        clean_artifact_url = artifact_url.strip()
        if not clean_release:
            raise ValueError("release is required")
        if not clean_artifact_url:
            raise ValueError("artifact_url is required")
        if not isinstance(source_map, dict) or not source_map:
            raise ValueError("source_map must be a non-empty object")
        self._validate_source_map_payload(source_map)
        try:
            source_map_json = json.dumps(source_map, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("source_map must be JSON-serializable") from exc
        source_map_id = _public_id(
            "smap", project_id, environment_id, clean_release, dist.strip(), clean_artifact_url
        )
        public_id = source_map_id
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO source_maps
                (id, public_id, project_id, environment_id, release, dist,
                 artifact_url, source_map_json, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, environment_id, release, dist, artifact_url)
                DO UPDATE SET
                    source_map_json = excluded.source_map_json,
                    uploaded_at = excluded.uploaded_at
                """,
                (
                    source_map_id,
                    public_id,
                    project_id,
                    environment_id,
                    clean_release,
                    dist.strip(),
                    clean_artifact_url,
                    source_map_json,
                    now,
                ),
            )
        return source_map_id

    def list_source_maps(
        self,
        *,
        project_id: str,
        environment_id: str,
        release: str,
        dist: Optional[str] = "",
        limit: int = 100,
    ) -> list[SourceMapRow]:
        where = "project_id = ? AND environment_id = ? AND release = ?"
        params: list[object] = [project_id, environment_id, release.strip()]
        if dist is not None:
            where += " AND dist = ?"
            params.append(dist.strip())
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM source_maps
                WHERE {where}
                ORDER BY uploaded_at DESC, artifact_url
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._source_map_from_row(row) for row in rows]

    def list_recent_source_maps(
        self,
        *,
        project_id: str,
        environment_id: str,
        limit: int = 100,
    ) -> list[SourceMapRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM source_maps
                WHERE project_id = ? AND environment_id = ?
                ORDER BY uploaded_at DESC
                LIMIT ?
                """,
                (
                    project_id,
                    environment_id,
                    max(1, min(int(limit), 500)),
                ),
            ).fetchall()
        return [self._source_map_from_row(row) for row in rows]

    def _validate_source_map_payload(self, source_map: dict[str, Any]) -> None:
        if source_map.get("version") != 3:
            raise ValueError("source_map must be a supported Source Map v3 object")
        if not isinstance(source_map.get("mappings"), str) or not source_map.get(
            "mappings"
        ):
            raise ValueError("source_map mappings must be a non-empty string")
        sources = source_map.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("source_map sources must be a non-empty list")
        if not all(isinstance(item, str) and item.strip() for item in sources):
            raise ValueError("source_map sources must contain only non-empty strings")
        names = source_map.get("names")
        if names is not None and not isinstance(names, list):
            raise ValueError("source_map names must be a list when provided")
        source_root = source_map.get("sourceRoot")
        if source_root is not None and not isinstance(source_root, str):
            raise ValueError("source_map sourceRoot must be a string when provided")
        file_value = source_map.get("file")
        if file_value is not None and not isinstance(file_value, str):
            raise ValueError("source_map file must be a string when provided")


    def _failure_from_row(self, row: sqlite3.Row) -> FailureRow:
        return self._storage._failure_from_row(row)

    def _evidence_from_row(self, row: sqlite3.Row) -> EvidenceRow:
        return self._storage._evidence_from_row(row)
