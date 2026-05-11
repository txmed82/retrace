"""Per-request scope (breadcrumbs, user, tags, contexts).

Backed by `contextvars` so it isolates per asyncio task and per thread
without the host code having to thread a context object around. Same
shape as Sentry's `Scope`, smaller surface.

Public mutators live on the top-level package (`retrace_sdk.set_user`,
etc.) and are thin wrappers around `Scope.current()`.
"""

from __future__ import annotations

import contextvars
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


_DEFAULT_MAX_BREADCRUMBS = 50


@dataclass
class Breadcrumb:
    timestamp: float  # epoch seconds, UTC
    category: str
    message: str
    level: str = "info"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "category": self.category,
            "message": self.message,
            "level": self.level,
            "data": dict(self.data),
        }


@dataclass
class Scope:
    """One per-context bucket of event-decorating state.

    `Scope.current()` returns the active scope for this thread/task. The
    bucket is mutable in place — every call to `set_user`/`add_breadcrumb`
    mutates the same `Scope` instance, so background-thread captures
    (e.g. logging) see what was already set on the caller.
    """

    max_breadcrumbs: int = _DEFAULT_MAX_BREADCRUMBS
    breadcrumbs: deque = field(default_factory=lambda: deque(maxlen=_DEFAULT_MAX_BREADCRUMBS))
    user: dict[str, Any] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    contexts: dict[str, dict[str, Any]] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    transaction: str = ""

    def add_breadcrumb(
        self,
        *,
        category: str = "",
        message: str = "",
        level: str = "info",
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        self.breadcrumbs.append(
            Breadcrumb(
                timestamp=time.time(),
                category=str(category or "default"),
                message=str(message or ""),
                level=str(level or "info"),
                data=dict(data or {}),
            )
        )

    def clear_breadcrumbs(self) -> None:
        self.breadcrumbs.clear()

    def set_user(self, user: Optional[dict[str, Any]]) -> None:
        if user is None:
            self.user = {}
        else:
            self.user = {k: v for k, v in user.items() if v is not None}

    def set_tag(self, key: str, value: Any) -> None:
        if value is None:
            self.tags.pop(key, None)
        else:
            self.tags[str(key)] = str(value)

    def set_context(self, key: str, value: Optional[dict[str, Any]]) -> None:
        if value is None:
            self.contexts.pop(key, None)
        else:
            self.contexts[str(key)] = dict(value)

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[str(key)] = value

    def set_transaction(self, name: str) -> None:
        self.transaction = str(name or "")

    def clear(self) -> None:
        self.breadcrumbs.clear()
        self.user = {}
        self.tags = {}
        self.contexts = {}
        self.extras = {}
        self.transaction = ""

    def reconfigure(self, *, max_breadcrumbs: int) -> None:
        """Resize the breadcrumb buffer (used by `init`)."""
        self.max_breadcrumbs = max(1, int(max_breadcrumbs))
        new_q: deque = deque(self.breadcrumbs, maxlen=self.max_breadcrumbs)
        self.breadcrumbs = new_q

    def snapshot(self) -> dict[str, Any]:
        """Return an event-merge-ready dict; safe to call from any
        thread because we copy the mutable parts out."""
        return {
            "user": dict(self.user),
            "tags": dict(self.tags),
            "contexts": {k: dict(v) for k, v in self.contexts.items()},
            "extras": dict(self.extras),
            "breadcrumbs": [b.to_dict() for b in self.breadcrumbs],
            "transaction": self.transaction,
        }

    @staticmethod
    def current() -> "Scope":
        scope = _current_scope.get()
        if scope is None:
            scope = Scope()
            _current_scope.set(scope)
        return scope

    @staticmethod
    def replace_current(scope: "Scope") -> None:
        _current_scope.set(scope)


_current_scope: contextvars.ContextVar[Optional[Scope]] = contextvars.ContextVar(
    "retrace_sdk_scope", default=None
)


def push_scope() -> "ScopeManager":
    """Context manager that isolates breadcrumbs/tags/user changes to a
    block. The original scope is restored on exit.

        with retrace_sdk.push_scope() as scope:
            scope.set_tag("phase", "checkout")
            do_thing()
        # tag gone
    """
    return ScopeManager()


class ScopeManager:
    def __enter__(self) -> Scope:
        self._token = _current_scope.set(Scope())
        return _current_scope.get()  # type: ignore[return-value]

    def __exit__(self, exc_type, exc, tb) -> None:
        _current_scope.reset(self._token)


def merge_event_with_scope(event: dict[str, Any], scope: Scope) -> dict[str, Any]:
    """Apply scope decorations to an event dict in place (returning it).

    User-supplied values on the event win over scope values — Sentry's
    behavior. Tags / contexts are merged shallowly with scope as the base.
    """
    snap = scope.snapshot()

    if snap["user"] and not event.get("user"):
        event["user"] = snap["user"]

    merged_tags: dict[str, Any] = dict(snap["tags"] or {})
    merged_tags.update(event.get("tags") or {})
    if merged_tags:
        event["tags"] = merged_tags

    merged_ctx: dict[str, Any] = {}
    for k, v in (snap["contexts"] or {}).items():
        merged_ctx[k] = dict(v or {})
    for k, v in (event.get("contexts") or {}).items():
        existing = merged_ctx.get(k) or {}
        existing.update(v or {})
        merged_ctx[k] = existing
    if merged_ctx:
        event["contexts"] = merged_ctx

    merged_extra: dict[str, Any] = dict(snap["extras"] or {})
    merged_extra.update(event.get("extra") or {})
    if merged_extra:
        event["extra"] = merged_extra

    if snap["breadcrumbs"]:
        event.setdefault("breadcrumbs", {})
        if isinstance(event["breadcrumbs"], list):
            # Allow user-supplied list form; convert.
            event["breadcrumbs"] = {"values": list(event["breadcrumbs"])}
        existing_values: Iterable[Any] = (event["breadcrumbs"].get("values") or [])
        event["breadcrumbs"]["values"] = list(snap["breadcrumbs"]) + list(existing_values)

    if snap["transaction"] and not event.get("transaction"):
        event["transaction"] = snap["transaction"]

    return event
