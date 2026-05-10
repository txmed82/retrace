from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

from retrace.monitoring_ingest import MonitoringIngestResult, ingest_monitoring_webhook
from retrace.sdk_keys import authenticate_sdk_key
from retrace.storage import SDKKeyRow, Storage


MAX_SENTRY_BODY_BYTES = 5 * 1024 * 1024


class SentryCompatIngestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SentryCompatIngestResponse:
    accepted: bool
    event_count: int
    results: list[MonitoringIngestResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "event_count": self.event_count,
            "results": [result.to_dict() for result in self.results],
        }


def build_sentry_dsn(*, public_key: str, base_url: str, project_id: str) -> str:
    parsed = urlparse(base_url.strip().rstrip("/") or "http://127.0.0.1:8788")
    scheme = parsed.scheme or "http"
    host = parsed.netloc or parsed.path
    prefix = parsed.path.strip("/") if parsed.netloc else ""
    path = "/".join(part for part in (prefix, project_id.strip("/")) if part)
    return f"{scheme}://{public_key}@{host}/{path}"


def ingest_sentry_compat_request(
    *,
    store: Storage,
    project_id: str,
    endpoint: str,
    headers: dict[str, str],
    body: bytes,
    query: dict[str, str] | None = None,
) -> SentryCompatIngestResponse:
    clean_endpoint = endpoint.strip().strip("/").lower()
    raw = _decode_body(body, content_encoding=_header(headers, "content-encoding"))
    content_type = _header(headers, "content-type").lower()
    is_envelope = (
        clean_endpoint == "envelope" or "application/x-sentry-envelope" in content_type
    )

    sdk_key = _authenticate_sentry_sdk_key(store, headers=headers, query=query)
    if sdk_key is None and is_envelope:
        sdk_key = authenticate_sdk_key(store, _extract_sentry_key_from_envelope(raw))
    if sdk_key is None:
        raise SentryCompatIngestError(
            401, "unauthorized", "Missing or invalid Sentry SDK key."
        )
    if project_id and sdk_key.project_id != project_id:
        raise SentryCompatIngestError(
            403, "forbidden", "SDK key does not belong to this project."
        )

    if is_envelope:
        events = parse_sentry_envelope(raw)
    elif clean_endpoint == "store":
        events = [parse_sentry_store_event(raw)]
    else:
        raise SentryCompatIngestError(
            404, "not_found", "Unsupported Sentry compatibility endpoint."
        )
    if not events:
        raise SentryCompatIngestError(
            400, "invalid_payload", "Sentry payload did not contain an error event."
        )

    results = [
        ingest_monitoring_webhook(
            store=store,
            project_id=sdk_key.project_id,
            environment_id=sdk_key.environment_id,
            provider="sentry",
            payload={"event": event},
        )
        for event in events
    ]
    return SentryCompatIngestResponse(
        accepted=True,
        event_count=len(results),
        results=results,
    )


def parse_sentry_store_event(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SentryCompatIngestError(
            400, "invalid_json", "Sentry store payload must be JSON."
        ) from exc
    if not isinstance(payload, dict) or not payload:
        raise SentryCompatIngestError(
            400, "invalid_payload", "Sentry store payload must be an object."
        )
    return payload


def parse_sentry_envelope(body: bytes) -> list[dict[str, Any]]:
    if not body.strip():
        raise SentryCompatIngestError(400, "invalid_payload", "Envelope is empty.")
    offset = _consume_json_line(body, 0)[1]
    events: list[dict[str, Any]] = []
    while offset < len(body):
        offset = _skip_newlines(body, offset)
        if offset >= len(body):
            break
        item_header, offset = _consume_json_line(body, offset)
        item_type = str(item_header.get("type") or "event").strip().lower()
        item_length = _safe_int(item_header.get("length"))
        if item_length > 0:
            payload_bytes = body[offset : offset + item_length]
            offset += item_length
            offset = _skip_newlines(body, offset)
        else:
            next_newline = body.find(b"\n", offset)
            if next_newline == -1:
                payload_bytes = body[offset:]
                offset = len(body)
            else:
                payload_bytes = body[offset:next_newline]
                offset = next_newline + 1
        if item_type not in {"event", "error"}:
            continue
        try:
            event = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SentryCompatIngestError(
                400, "invalid_json", "Envelope item payload must be JSON."
            ) from exc
        if isinstance(event, dict) and event:
            events.append(event)
    return events


def _authenticate_sentry_sdk_key(
    store: Storage,
    *,
    headers: dict[str, str],
    query: dict[str, str] | None,
) -> SDKKeyRow | None:
    return authenticate_sdk_key(store, _extract_sentry_key(headers=headers, query=query))


def _extract_sentry_key(
    *, headers: dict[str, str], query: dict[str, str] | None = None
) -> str:
    if query:
        for key in ("sentry_key", "key", "api_key", "apiKey"):
            value = str(query.get(key) or "").strip()
            if value:
                return value
    auth = _header(headers, "x-sentry-auth").strip()
    if auth:
        for part in auth.removeprefix("Sentry").split(","):
            name, sep, value = part.strip().partition("=")
            if sep and name.strip() == "sentry_key" and value.strip():
                return value.strip()
    direct = _header(headers, "x-retrace-key").strip()
    if direct:
        return direct
    bearer = _header(headers, "authorization").strip()
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return ""


def _extract_sentry_key_from_envelope(body: bytes) -> str:
    try:
        envelope_header = _consume_json_line(body, 0)[0]
    except SentryCompatIngestError:
        return ""
    dsn = str(envelope_header.get("dsn") or "").strip()
    if not dsn:
        return ""
    parsed = urlparse(dsn)
    return parsed.username or ""


def _decode_body(body: bytes, *, content_encoding: str) -> bytes:
    if len(body) > MAX_SENTRY_BODY_BYTES:
        raise SentryCompatIngestError(
            413, "body_too_large", "Sentry payload is too large."
        )
    if not body:
        raise SentryCompatIngestError(400, "invalid_payload", "Sentry body is empty.")
    if content_encoding.strip().lower() != "gzip":
        return body
    try:
        with gzip.GzipFile(fileobj=BytesIO(body)) as gz:
            raw = gz.read(MAX_SENTRY_BODY_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise SentryCompatIngestError(400, "invalid_gzip", "Invalid gzip body.") from exc
    if len(raw) > MAX_SENTRY_BODY_BYTES:
        raise SentryCompatIngestError(
            413, "body_too_large", "Sentry payload is too large."
        )
    return raw


def _consume_json_line(body: bytes, offset: int) -> tuple[dict[str, Any], int]:
    newline = body.find(b"\n", offset)
    if newline == -1:
        line = body[offset:]
        next_offset = len(body)
    else:
        line = body[offset:newline]
        next_offset = newline + 1
    try:
        payload = json.loads(line.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SentryCompatIngestError(
            400, "invalid_json", "Envelope header must be JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise SentryCompatIngestError(
            400, "invalid_payload", "Envelope header must be an object."
        )
    return payload, next_offset


def _skip_newlines(body: bytes, offset: int) -> int:
    while offset < len(body) and body[offset] in b"\r\n":
        offset += 1
    return offset


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _header(headers: dict[str, str], name: str) -> str:
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return str(value)
    return ""
