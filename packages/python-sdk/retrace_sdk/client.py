"""The `Client` is the central object the public API delegates to.

One global `_active_client` is set by `init()` and consulted by
`capture_*` / `add_breadcrumb` / etc. We don't ship a `Hub` abstraction
yet — the common case is one client per process, and supporting
multi-tenant in-process Hubs adds surface area that we don't need
until someone asks for it.
"""

from __future__ import annotations

import atexit
import logging
import sys
import threading
from typing import Any, Optional

from .dsn import Dsn, DsnError, parse_dsn
from .envelope import build_envelope_bytes, build_event
from .scope import Scope, merge_event_with_scope
from .transport import Transport


log = logging.getLogger("retrace_sdk")


_SDK_VERSION = "0.1.0"


class Client:
    """Holds the DSN, default tags, transport, and active integrations.

    Construct via `retrace_sdk.init(dsn=...)`. The constructor itself
    is exposed for tests so a stub-sender transport can be wired in
    without touching globals.
    """

    def __init__(
        self,
        *,
        dsn: str,
        release: str = "",
        environment: str = "",
        server_name: str = "",
        max_breadcrumbs: int = 50,
        default_tags: Optional[dict[str, str]] = None,
        traces_sample_rate: float = 1.0,
        before_send=None,
        transport: Optional[Transport] = None,
        integrations: Optional[list] = None,
        debug: bool = False,
    ):
        self.dsn_obj: Dsn = parse_dsn(dsn)
        self.release = release or ""
        self.environment = environment or ""
        self.server_name = server_name or ""
        self.max_breadcrumbs = int(max_breadcrumbs)
        self.default_tags = {str(k): str(v) for k, v in (default_tags or {}).items()}
        self.traces_sample_rate = float(traces_sample_rate)
        self.before_send = before_send
        self.debug = bool(debug)
        if self.debug:
            log.setLevel(logging.DEBUG)
            if not log.handlers:
                log.addHandler(logging.StreamHandler())

        # Apply scope sizing immediately so even pre-init breadcrumbs
        # (e.g. left over from a prior init in tests) get truncated.
        Scope.current().reconfigure(max_breadcrumbs=self.max_breadcrumbs)

        self.transport: Transport = transport or Transport(
            url=self.dsn_obj.envelope_url,
            public_key=self.dsn_obj.public_key,
        )

        self._integrations_installed: list[str] = []
        if integrations:
            for integ in integrations:
                self._install_integration(integ)

    # ---- public-API delegates -------------------------------------------

    def capture_exception(
        self,
        exc: Optional[BaseException] = None,
        *,
        level: str = "error",
        tags: Optional[dict[str, Any]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        exc_info = self._resolve_exc_info(exc)
        if exc_info is None:
            return None
        return self._capture(
            event_kwargs={
                "exc_info": exc_info,
                "level": level,
                "tags": tags,
                "extra": extra,
            }
        )

    def capture_message(
        self,
        message: str,
        *,
        level: str = "info",
        tags: Optional[dict[str, Any]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        if not message:
            return None
        return self._capture(
            event_kwargs={
                "message": str(message),
                "level": level,
                "tags": tags,
                "extra": extra,
            }
        )

    def flush(self, timeout: float = 2.0) -> bool:
        return self.transport.flush(timeout=timeout)

    def close(self, timeout: float = 2.0) -> None:
        self.transport.shutdown(timeout=timeout)

    # ---- internals ------------------------------------------------------

    def _resolve_exc_info(self, exc: Optional[BaseException]) -> Optional[tuple]:
        if exc is None:
            exc_info = sys.exc_info()
            if exc_info[1] is None:
                return None
            return exc_info
        return (type(exc), exc, exc.__traceback__)

    def _capture(self, *, event_kwargs: dict[str, Any]) -> Optional[str]:
        tags = dict(self.default_tags)
        tags.update(event_kwargs.pop("tags", None) or {})

        event = build_event(
            release=self.release,
            environment=self.environment,
            server_name=self.server_name,
            tags=tags or None,
            sdk_version=_SDK_VERSION,
            **{k: v for k, v in event_kwargs.items() if v is not None},
        )

        scope = Scope.current()
        merge_event_with_scope(event, scope)

        if self.before_send is not None:
            try:
                event = self.before_send(event)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("before_send raised; dropping event: %s", exc)
                return None
            if event is None:
                return None

        try:
            payload = build_envelope_bytes(event=event, dsn=self.dsn_obj.raw)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("envelope serialization failed: %s", exc)
            return None

        self.transport.enqueue(payload)
        return str(event.get("event_id") or "")

    def _install_integration(self, integration) -> None:
        name = getattr(integration, "identifier", integration.__class__.__name__)
        if name in self._integrations_installed:
            return
        try:
            integration.setup(self)
            self._integrations_installed.append(name)
        except Exception as exc:
            log.warning("integration %s setup failed: %s", name, exc)


# ---------------------------------------------------------------------------
# Module-level singleton wiring
# ---------------------------------------------------------------------------


_active_client: Optional[Client] = None
_client_lock = threading.Lock()


def get_client() -> Optional[Client]:
    return _active_client


def set_client(client: Optional[Client]) -> None:
    """Test-only escape hatch. Real callers go through `init()`."""
    global _active_client
    with _client_lock:
        _active_client = client


def init(
    *,
    dsn: str = "",
    release: str = "",
    environment: str = "",
    server_name: str = "",
    max_breadcrumbs: int = 50,
    default_tags: Optional[dict[str, str]] = None,
    traces_sample_rate: float = 1.0,
    before_send=None,
    integrations: Optional[list] = None,
    debug: bool = False,
    transport: Optional[Transport] = None,
) -> Optional[Client]:
    """Initialize the SDK.

    Empty / missing `dsn` is treated as "disabled" — `init()` returns
    `None`, and all subsequent `capture_*` calls become no-ops. This
    matches Sentry's behavior; it lets you keep SDK calls in code
    that runs in environments without a Retrace server.
    """
    global _active_client

    if not dsn:
        with _client_lock:
            prev = _active_client
            _active_client = None
        _safe_close(prev)
        return None

    try:
        client = Client(
            dsn=dsn,
            release=release,
            environment=environment,
            server_name=server_name,
            max_breadcrumbs=max_breadcrumbs,
            default_tags=default_tags,
            traces_sample_rate=traces_sample_rate,
            before_send=before_send,
            integrations=integrations,
            debug=debug,
            transport=transport,
        )
    except DsnError as exc:
        log.warning("retrace_sdk.init: invalid DSN, disabling: %s", exc)
        with _client_lock:
            prev = _active_client
            _active_client = None
        _safe_close(prev)
        return None

    with _client_lock:
        prev = _active_client
        _active_client = client

    if prev is not None and prev is not client:
        _safe_close(prev)

    atexit.register(_atexit_flush)
    return client


def _safe_close(client: Optional[Client]) -> None:
    """Close a previous client without letting its shutdown errors
    escape into `init()`. Always called outside the singleton lock so
    a slow transport flush can't block other threads from observing
    the new state."""
    if client is None:
        return
    try:
        client.close(timeout=1.0)
    except Exception:  # pragma: no cover - defensive
        pass


def _atexit_flush() -> None:
    client = _active_client
    if client is None:
        return
    try:
        client.close(timeout=2.0)
    except Exception:  # pragma: no cover - defensive
        pass
