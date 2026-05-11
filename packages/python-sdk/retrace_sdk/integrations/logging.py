"""stdlib `logging` integration.

Two responsibilities, configurable independently:

  - **Breadcrumb logger** — every `INFO`+ log record below the
    `event_level` becomes a breadcrumb on the current scope.
  - **Event logger** — every `ERROR`+ record (or higher, configurable)
    becomes a Retrace event via `client.capture_message()` or
    `client.capture_exception()` if the record carries an exc_info.

Usage:

    import logging
    import retrace_sdk

    retrace_sdk.init(
        dsn="...",
        integrations=[
            retrace_sdk.LoggingIntegration(
                breadcrumb_level=logging.INFO,
                event_level=logging.ERROR,
            ),
        ],
    )

The integration installs a single `Handler` on the root logger. Pass
`logger_name=...` to attach to a specific logger only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ..scope import Scope
from ._base import Integration


if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client


class LoggingIntegration(Integration):
    identifier = "logging"

    def __init__(
        self,
        *,
        breadcrumb_level: int = logging.INFO,
        event_level: int = logging.ERROR,
        logger_name: Optional[str] = None,
    ) -> None:
        self.breadcrumb_level = int(breadcrumb_level)
        self.event_level = int(event_level)
        self.logger_name = logger_name
        self._handler: Optional[logging.Handler] = None

    def setup(self, client: "Client") -> None:
        handler = _RetraceLoggingHandler(
            client=client,
            breadcrumb_level=self.breadcrumb_level,
            event_level=self.event_level,
        )
        target = logging.getLogger(self.logger_name) if self.logger_name else logging.getLogger()
        # Don't double-install on init() retries / test resets.
        for h in list(target.handlers):
            if isinstance(h, _RetraceLoggingHandler):
                target.removeHandler(h)
        target.addHandler(handler)
        # Make sure the target's effective level lets our threshold
        # through. We do NOT lower the level globally — if the user
        # has root at WARNING, INFO breadcrumbs are still dropped.
        self._handler = handler


class _RetraceLoggingHandler(logging.Handler):
    def __init__(
        self,
        *,
        client: "Client",
        breadcrumb_level: int,
        event_level: int,
    ) -> None:
        # Handler level must be the *floor* of breadcrumb_level and
        # event_level — otherwise a config like
        # `breadcrumb=ERROR, event=WARNING` would block WARNING records
        # at the handler before `emit()` could promote them to events.
        # (CodeRabbit Major catch on PR #128.)
        super().__init__(level=min(int(breadcrumb_level), int(event_level)))
        self._client = client
        self._breadcrumb_level = int(breadcrumb_level)
        self._event_level = int(event_level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - record formatting failed
            msg = record.msg if isinstance(record.msg, str) else "<unformattable log record>"

        if record.levelno >= self._event_level:
            tags = {"logger": record.name, "level": record.levelname}
            if record.exc_info and record.exc_info[1] is not None:
                self._client.capture_exception(
                    record.exc_info[1],
                    level=_LEVEL_NAMES.get(record.levelno, "error"),
                    tags=tags,
                    extra={"log_message": msg},
                )
            else:
                self._client.capture_message(
                    msg,
                    level=_LEVEL_NAMES.get(record.levelno, "error"),
                    tags=tags,
                )
            return

        if record.levelno >= self._breadcrumb_level:
            Scope.current().add_breadcrumb(
                category=record.name or "log",
                message=msg,
                level=_LEVEL_NAMES.get(record.levelno, "info"),
                data={"logger": record.name},
            )


_LEVEL_NAMES = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "fatal",
}
