"""retrace-sdk — Python error/event SDK for Retrace.

Public surface (deliberately small):

    init(dsn=..., release=..., environment=..., integrations=[...])
    capture_exception(exc=None)         # current exc_info if None
    capture_message("string", level="info")
    add_breadcrumb(category=..., message=..., level=..., data={})
    set_user({"id": "u_123"})
    set_tag("k", "v")
    set_context("checkout", {"order_id": "..."})
    set_extra("debug_blob", {...})
    push_scope()                         # context manager
    flush(timeout=2.0)
    close(timeout=2.0)

Integrations:
    FastAPIIntegration
    FlaskIntegration
    DjangoIntegration
    LoggingIntegration

Calls made before `init()` (or when `dsn` is empty/missing) are no-ops.
"""

from __future__ import annotations

from typing import Any, Optional

from .client import Client, get_client, init, set_client
from .dsn import Dsn, DsnError, parse_dsn
from .scope import Breadcrumb, Scope, merge_event_with_scope, push_scope


__all__ = [
    "init",
    "get_client",
    "set_client",
    "capture_exception",
    "capture_message",
    "add_breadcrumb",
    "set_user",
    "set_tag",
    "set_context",
    "set_extra",
    "set_transaction",
    "push_scope",
    "flush",
    "close",
    "Client",
    "Scope",
    "Breadcrumb",
    "Dsn",
    "DsnError",
    "parse_dsn",
    "merge_event_with_scope",
    "FastAPIIntegration",
    "FlaskIntegration",
    "DjangoIntegration",
    "LoggingIntegration",
]


# ---------------------------------------------------------------------------
# Top-level functional API — thin wrappers on the active client + scope.
# ---------------------------------------------------------------------------


def capture_exception(
    exc: Optional[BaseException] = None,
    *,
    level: str = "error",
    tags: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    client = get_client()
    if client is None:
        return None
    return client.capture_exception(exc, level=level, tags=tags, extra=extra)


def capture_message(
    message: str,
    *,
    level: str = "info",
    tags: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    client = get_client()
    if client is None:
        return None
    return client.capture_message(message, level=level, tags=tags, extra=extra)


def add_breadcrumb(
    *,
    category: str = "",
    message: str = "",
    level: str = "info",
    data: Optional[dict[str, Any]] = None,
) -> None:
    Scope.current().add_breadcrumb(
        category=category, message=message, level=level, data=data
    )


def set_user(user: Optional[dict[str, Any]]) -> None:
    Scope.current().set_user(user)


def set_tag(key: str, value: Any) -> None:
    Scope.current().set_tag(key, value)


def set_context(key: str, value: Optional[dict[str, Any]]) -> None:
    Scope.current().set_context(key, value)


def set_extra(key: str, value: Any) -> None:
    Scope.current().set_extra(key, value)


def set_transaction(name: str) -> None:
    Scope.current().set_transaction(name)


def flush(timeout: float = 2.0) -> bool:
    client = get_client()
    if client is None:
        return True
    return client.flush(timeout=timeout)


def close(timeout: float = 2.0) -> None:
    client = get_client()
    if client is None:
        return
    client.close(timeout=timeout)


# ---------------------------------------------------------------------------
# Integration classes — exposed at the top level for `from retrace_sdk
# import FastAPIIntegration` style usage. Lazy-imported to avoid pulling
# in framework deps on a plain `import retrace_sdk`.
# ---------------------------------------------------------------------------


def __getattr__(name: str):  # PEP 562
    if name == "FastAPIIntegration":
        from .integrations.fastapi import FastAPIIntegration
        return FastAPIIntegration
    if name == "FlaskIntegration":
        from .integrations.flask import FlaskIntegration
        return FlaskIntegration
    if name == "DjangoIntegration":
        from .integrations.django import DjangoIntegration
        return DjangoIntegration
    if name == "LoggingIntegration":
        from .integrations.logging import LoggingIntegration
        return LoggingIntegration
    raise AttributeError(f"module 'retrace_sdk' has no attribute {name!r}")
