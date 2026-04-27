from __future__ import annotations

import gzip
from io import BytesIO
import json
from dataclasses import asdict, dataclass
from typing import Any

from retrace.sdk_keys import authenticate_sdk_key
from retrace.storage import Storage


MAX_REPLAY_BODY_BYTES = 5 * 1024 * 1024


class ReplayIngestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ReplayIngestResponse:
    accepted: bool
    duplicate: bool
    session_id: str
    sequence: int
    event_count: int
    batch_id: str


def _header(headers: dict[str, str], name: str) -> str:
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return str(v)
    return ""


def _extract_key(headers: dict[str, str], query: dict[str, str] | None = None) -> str:
    direct = _header(headers, "x-retrace-key").strip()
    if direct:
        return direct
    auth = _header(headers, "authorization").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if query:
        return str(
            query.get("key")
            or query.get("api_key")
            or query.get("apiKey")
            or ""
        ).strip()
    return ""


def decode_replay_body(body: bytes, *, content_encoding: str = "") -> dict[str, Any]:
    if len(body) > MAX_REPLAY_BODY_BYTES:
        raise ReplayIngestError(413, "body_too_large", "Replay batch is too large.")
    raw = body
    if content_encoding.strip().lower() == "gzip":
        try:
            with gzip.GzipFile(fileobj=BytesIO(body)) as gz:
                raw = gz.read(MAX_REPLAY_BODY_BYTES + 1)
        except (EOFError, OSError) as exc:
            raise ReplayIngestError(400, "invalid_gzip", "Invalid gzip body.") from exc
        if len(raw) > MAX_REPLAY_BODY_BYTES:
            raise ReplayIngestError(413, "body_too_large", "Replay batch is too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayIngestError(400, "invalid_json", "Replay body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise ReplayIngestError(400, "invalid_payload", "Replay body must be an object.")
    return payload


def ingest_replay_request(
    *,
    store: Storage,
    headers: dict[str, str],
    body: bytes,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    raw_key = _extract_key(headers, query)
    auth = authenticate_sdk_key(store, raw_key)
    if auth is None:
        raise ReplayIngestError(401, "unauthorized", "Missing or invalid SDK key.")

    payload = decode_replay_body(
        body,
        content_encoding=_header(headers, "content-encoding"),
    )
    session_id = str(payload.get("sessionId") or payload.get("session_id") or "").strip()
    if not session_id:
        raise ReplayIngestError(400, "missing_session_id", "sessionId is required.")

    try:
        sequence = int(payload.get("sequence"))
    except (TypeError, ValueError) as exc:
        raise ReplayIngestError(400, "invalid_sequence", "sequence must be an integer.") from exc
    if sequence < 0:
        raise ReplayIngestError(400, "invalid_sequence", "sequence must be non-negative.")

    events = payload.get("events")
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        raise ReplayIngestError(400, "invalid_events", "events must be an array of objects.")

    flush_type = str(payload.get("flushType") or payload.get("flush_type") or "normal")
    distinct_id = str(payload.get("distinctId") or payload.get("distinct_id") or "")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}

    result = store.insert_replay_batch(
        project_id=auth.project_id,
        environment_id=auth.environment_id,
        session_id=session_id,
        sequence=sequence,
        events=events,
        flush_type=flush_type,
        distinct_id=distinct_id,
        metadata=metadata,
    )
    response = asdict(
        ReplayIngestResponse(
            accepted=result.inserted,
            duplicate=not result.inserted,
            session_id=session_id,
            sequence=sequence,
            event_count=result.event_count,
            batch_id=result.batch_id,
        )
    )
    response["processing_job_id"] = result.processing_job_id
    return response
