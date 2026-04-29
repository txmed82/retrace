"""Outbound notification sinks (webhooks, Slack).

Self-host users wire one or more notification destinations into config.yaml
so Retrace pings them when meaningful product events happen.  This module
owns: payload shape, transport (httpx with retry-on-429), and a small
fan-out helper that callers use at hook sites without knowing which sinks
the user has configured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)


class NotificationEvent(str, Enum):
    ISSUE_CREATED = "issue.created"
    ISSUE_REGRESSED = "issue.regressed"
    ISSUE_RESOLVED = "issue.resolved"
    RUN_FAILED = "run.failed"
    TICKET_CREATED = "ticket.created"


@dataclass
class NotificationPayload:
    event: str
    title: str
    summary: str
    severity: str = ""
    public_id: str = ""
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass(frozen=True)
class NotificationDeliveryResult:
    sink: str
    target: str
    ok: bool
    status_code: int
    error: str = ""


# ----------------------------------------------------------------------------
# Sinks
# ----------------------------------------------------------------------------


def _post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    content: bytes | None = None,
    max_attempts: int = 3,
) -> httpx.Response:
    if json_body is not None and content is not None:
        raise ValueError("pass json_body or content, not both")
    last: httpx.Response | None = None
    for attempt in range(max_attempts):
        if content is not None:
            last = client.post(url, headers=headers, content=content)
        else:
            last = client.post(url, headers=headers, json=json_body)
        if last.status_code != 429 and last.status_code < 500:
            return last
        if attempt >= max_attempts - 1:
            return last
        retry_after = last.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            sleep_s = float(retry_after)
        else:
            sleep_s = min(8.0, 0.5 * (2**attempt))
        time.sleep(sleep_s)
    assert last is not None
    return last


class WebhookSink:
    """Generic JSON webhook.  Sends the raw NotificationPayload as-is so
    receivers can shape their own templates."""

    name = "webhook"

    def __init__(
        self,
        *,
        url: str,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
        secret: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        if not url.strip():
            raise ValueError("webhook url is required")
        self.url = url.strip()
        self.secret = secret.strip()
        self._extra_headers = dict(headers or {})
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "WebhookSink":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def send(self, payload: NotificationPayload) -> NotificationDeliveryResult:
        body_dict = payload.as_dict()
        body_bytes = json.dumps(body_dict, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "retrace-webhook/1",
            "X-Retrace-Timestamp": timestamp,
            **self._extra_headers,
        }
        if self.secret:
            signed = (timestamp + ".").encode("utf-8") + body_bytes
            digest = hmac.new(
                self.secret.encode("utf-8"), signed, hashlib.sha256
            ).hexdigest()
            headers["X-Retrace-Signature"] = f"sha256={digest}"
        try:
            resp = _post_with_retry(
                self._client,
                self.url,
                headers=headers,
                content=body_bytes,
            )
        except Exception as exc:
            return NotificationDeliveryResult(
                sink=self.name, target=self.url, ok=False, status_code=0, error=str(exc)
            )
        ok = resp.status_code < 400
        return NotificationDeliveryResult(
            sink=self.name,
            target=self.url,
            ok=ok,
            status_code=resp.status_code,
            error="" if ok else _truncate(resp.text),
        )


class SlackSink:
    """Slack incoming webhook.  Formats the payload as Slack blocks; falls
    back to plain text if the receiver rejects blocks."""

    name = "slack"

    def __init__(
        self,
        *,
        webhook_url: str,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not webhook_url.strip():
            raise ValueError("slack webhook_url is required")
        self.webhook_url = webhook_url.strip()
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SlackSink":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def send(self, payload: NotificationPayload) -> NotificationDeliveryResult:
        body = self._format(payload)
        try:
            resp = _post_with_retry(
                self._client,
                self.webhook_url,
                headers={"Content-Type": "application/json"},
                json_body=body,
            )
        except Exception as exc:
            return NotificationDeliveryResult(
                sink=self.name,
                target=self.webhook_url,
                ok=False,
                status_code=0,
                error=str(exc),
            )
        ok = resp.status_code < 400
        return NotificationDeliveryResult(
            sink=self.name,
            target=self.webhook_url,
            ok=ok,
            status_code=resp.status_code,
            error="" if ok else _truncate(resp.text),
        )

    @staticmethod
    def _format(payload: NotificationPayload) -> dict[str, Any]:
        emoji = {
            NotificationEvent.ISSUE_CREATED.value: ":rotating_light:",
            NotificationEvent.ISSUE_REGRESSED.value: ":warning:",
            NotificationEvent.ISSUE_RESOLVED.value: ":white_check_mark:",
            NotificationEvent.RUN_FAILED.value: ":x:",
            NotificationEvent.TICKET_CREATED.value: ":pencil:",
        }.get(payload.event, ":mag:")
        header = f"{emoji} *{payload.event}* — {payload.title}"
        if payload.severity:
            header += f"  _{payload.severity}_"
        text_lines = [header]
        if payload.summary:
            text_lines.append(payload.summary)
        if payload.public_id:
            text_lines.append(f"`{payload.public_id}`")
        if payload.url:
            text_lines.append(f"<{payload.url}|Open in Retrace>")
        text = "\n".join(text_lines)
        return {
            "text": text,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            ],
        }


# ----------------------------------------------------------------------------
# Fan-out helper
# ----------------------------------------------------------------------------


def dispatch_notification(
    sinks: Iterable[Any],
    payload: NotificationPayload,
) -> list[NotificationDeliveryResult]:
    """Fan out a notification to every configured sink.  Never raises:
    individual delivery failures are returned in the result list and logged.
    """
    results: list[NotificationDeliveryResult] = []
    for sink in sinks:
        try:
            result = sink.send(payload)
        except Exception as exc:
            logger.exception("notification sink %r raised", getattr(sink, "name", sink))
            result = NotificationDeliveryResult(
                sink=getattr(sink, "name", "unknown"),
                target="",
                ok=False,
                status_code=0,
                error=str(exc),
            )
        if not result.ok:
            logger.warning(
                "notification %s -> %s failed (%s): %s",
                payload.event,
                result.sink,
                result.status_code,
                result.error,
            )
        results.append(result)
    return results


def build_sinks_from_config(cfg: Any) -> list[Any]:
    """Construct sink instances from a NotificationConfig.

    Returns an empty list when nothing is enabled, so callers can dispatch
    unconditionally without checking for None.
    """
    sinks: list[Any] = []
    if cfg is None:
        return sinks
    webhook_url = getattr(cfg, "webhook_url", "").strip()
    if webhook_url:
        sinks.append(
            WebhookSink(
                url=webhook_url,
                secret=getattr(cfg, "webhook_secret", "").strip(),
            )
        )
    slack_url = getattr(cfg, "slack_webhook_url", "").strip()
    if slack_url:
        sinks.append(SlackSink(webhook_url=slack_url))
    return sinks


def close_sinks(sinks: Iterable[Any]) -> None:
    for sink in sinks:
        try:
            close = getattr(sink, "close", None)
            if callable(close):
                close()
        except Exception:
            logger.debug("error closing notification sink", exc_info=True)


def _truncate(text: str, limit: int = 400) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


# json import is used by callers that want to log payload bodies; keep it
# referenced so import lint stays quiet.
_ = json
