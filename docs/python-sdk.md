# Python SDK

`retrace-sdk` is the official Python client for sending backend errors
and breadcrumbs to a Retrace server. It's wire-compatible with the
Sentry envelope protocol, so it talks to the existing
`/api/sentry/<project_id>/envelope/` endpoint the way the Sentry SDK
already does — no new server endpoint needed.

## Install

```bash
pip install retrace-sdk
# Framework extras (pulls the framework itself in)
pip install 'retrace-sdk[fastapi]'
pip install 'retrace-sdk[flask]'
pip install 'retrace-sdk[django]'
```

## Quickstart

```python
import retrace_sdk

retrace_sdk.init(
    dsn="http://rtpk_…@127.0.0.1:8788/<project_id>",
    release="v1.2.3",
    environment="production",
    max_breadcrumbs=50,
    integrations=[retrace_sdk.LoggingIntegration()],
)

retrace_sdk.add_breadcrumb(category="auth", message="login attempt")
retrace_sdk.set_user({"id": "u_123"})

try:
    do_thing()
except Exception:
    retrace_sdk.capture_exception()
```

`dsn` is the same shape the API server's `retrace api init` already
emits — copy it from the `credentials.sentry_dsn` field in
`retrace api init` output.

If `dsn` is empty or invalid the SDK initializes as a no-op — every
subsequent `capture_*` call returns `None` without raising. This makes
it safe to keep SDK calls in code that also runs in environments
without a Retrace server (CI, dev laptops without docker up, etc.).

## Public API

```python
retrace_sdk.init(dsn=..., release=..., environment=..., max_breadcrumbs=50,
                 default_tags={...}, traces_sample_rate=1.0,
                 before_send=callable, integrations=[...], debug=False)
retrace_sdk.capture_exception(exc=None, level="error", tags=..., extra=...)
retrace_sdk.capture_message("string", level="info", tags=..., extra=...)
retrace_sdk.add_breadcrumb(category=..., message=..., level=..., data={...})
retrace_sdk.set_user({"id": "u_123", "email": "..."})
retrace_sdk.set_tag("k", "v")
retrace_sdk.set_context("checkout", {"order_id": "..."})
retrace_sdk.set_extra("debug_blob", {...})
retrace_sdk.set_transaction("checkout.flow")
retrace_sdk.push_scope()                # context manager
retrace_sdk.flush(timeout=2.0)
retrace_sdk.close(timeout=2.0)
```

`capture_exception()` with no argument uses `sys.exc_info()` — call it
inside an `except` block. With an explicit `exc` it captures that
exception (handy for caught-and-logged paths).

`before_send(event) -> dict | None` runs synchronously before the event
is queued. Return `None` to drop. Use it for last-mile scrubbing
(e.g. mask user emails).

## Framework integrations

### FastAPI

```python
from fastapi import FastAPI
import retrace_sdk

retrace_sdk.init(
    dsn="...",
    integrations=[retrace_sdk.FastAPIIntegration()],
)

app = FastAPI()
retrace_sdk.FastAPIIntegration.attach(app)
```

The integration installs an ASGI middleware that:

- Wraps each request in a fresh scope (no per-request leakage of
  breadcrumbs/tags across concurrent requests).
- Sets `transaction = "<METHOD> <path>"`.
- Adds an HTTP-method/path breadcrumb at the start of every request.
- On unhandled exceptions: captures with `http.method` and `http.route`
  tags, then re-raises so FastAPI's own 500 handling runs.

### Flask

```python
from flask import Flask
import retrace_sdk

retrace_sdk.init(
    dsn="...",
    integrations=[retrace_sdk.FlaskIntegration()],
)

app = Flask(__name__)
retrace_sdk.FlaskIntegration.attach(app)
```

Uses Flask's `got_request_exception` signal + a `before_request` hook.

### Django

```python
# settings.py
MIDDLEWARE = [
    "retrace_sdk.integrations.django.RetraceMiddleware",
    # ...your other middleware
]

# apps.py / asgi.py / wsgi.py
import retrace_sdk
retrace_sdk.init(
    dsn="...",
    integrations=[retrace_sdk.DjangoIntegration()],
)
```

`RetraceMiddleware` captures any exception that propagates through the
view stack, plus implements `process_exception` for cases where another
middleware caught and rethrew.

### stdlib `logging`

```python
import logging
import retrace_sdk

retrace_sdk.init(
    dsn="...",
    integrations=[
        retrace_sdk.LoggingIntegration(
            breadcrumb_level=logging.INFO,    # INFO → breadcrumb
            event_level=logging.ERROR,        # ERROR → captured event
            logger_name=None,                  # None = root logger
        ),
    ],
)
```

`log.exception(...)` (which carries `exc_info`) becomes a captured
exception event. Plain `log.error("...")` becomes a `capture_message`.

## Design notes

- **Stdlib only at runtime.** Networking is `urllib.request`; no
  `httpx`/`urllib3`/`requests` dependency. Keeps install footprint
  small and avoids version skew with the host app.
- **Background-thread transport** (`retrace_sdk.transport`). The hot
  path of `capture_*` is queue.put_nowait → return. The worker thread
  pulls items and POSTs them.
- **Drop policy: oldest-first.** A bounded queue (default size 100)
  drops the *oldest* item when full. The freshest crash is more likely
  to be the one you care about. `transport.stats.dropped_overflow`
  counts drops.
- **`contextvars`-backed scope.** Each asyncio task / thread gets its
  own scope — concurrent requests don't smear each other's user/tag/
  breadcrumb state.
- **`atexit` flush.** The first `init()` registers an `atexit` handler
  that calls `close(timeout=2.0)`. Tests use `flush(timeout=...)`
  directly.

## Testing the SDK

```bash
cd packages/python-sdk
uv pip install -e '.[test]'
pytest tests -q
```

The end-to-end test (`tests/test_e2e_ingest.py`) requires the main
`retrace` package importable; it sends the SDK's actual envelope bytes
through the server-side `ingest_sentry_compat_request` to prove the
wire format matches.
