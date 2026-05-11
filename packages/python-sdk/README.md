# retrace-sdk

Python SDK for [Retrace](https://github.com/txmed82/retrace) — capture
backend exceptions, breadcrumbs, and context from FastAPI / Flask /
Django apps. Wire-compatible with the Sentry envelope protocol so it
talks to your local Retrace API server out of the box.

## Install

```bash
pip install retrace-sdk
# Optional framework extras
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
    integrations=[
        retrace_sdk.LoggingIntegration(),
    ],
)

# Anywhere in your code:
retrace_sdk.add_breadcrumb(category="auth", message="login attempt")
retrace_sdk.set_user({"id": "u_123"})

try:
    do_thing()
except Exception:
    retrace_sdk.capture_exception()
```

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

### Django

```python
# settings.py
MIDDLEWARE = [
    "retrace_sdk.integrations.django.RetraceMiddleware",
    # ... your other middleware
]

# wsgi.py / asgi.py
import retrace_sdk
retrace_sdk.init(
    dsn="...",
    integrations=[retrace_sdk.DjangoIntegration()],
)
```

## Design

- **Standard-library only at runtime** — `urllib`, `threading`,
  `queue`, `contextvars`. No `urllib3`, `httpx`, or `requests`
  dependency. Keeps the install tiny and avoids version skew with your
  app's networking stack.
- **Sentry envelope wire format** — accepted by Retrace's
  `/api/sentry/<project>/envelope/` endpoint (and by any Sentry server
  if you want to forward).
- **Background thread + bounded queue** — `capture_*` never blocks
  your request path. Queue overflow drops the *oldest* event (the
  fresh crash is more likely to be the one you care about).
- **`contextvars`-backed scope** — breadcrumbs/tags/user are isolated
  per async task and per thread, so concurrent requests don't smear
  each other's context onto events.

## License

MIT.
