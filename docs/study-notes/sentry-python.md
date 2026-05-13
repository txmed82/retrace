# Study note: sentry-python

**Repo:** [getsentry/sentry-python](https://github.com/getsentry/sentry-python)
**Pin:** HEAD as of 2026-05-11
**Read on disk:** `external/sentry-python` (gitignored).

## What we read

- `sentry_sdk/hub.py`, `sentry_sdk/scope.py` — the global Hub/Scope
  abstraction.
- `sentry_sdk/client.py` — `init()`, `capture_*` plumbing, lifecycle.
- `sentry_sdk/transport.py` — background worker, queue, envelope POST.
- `sentry_sdk/integrations/__init__.py` and the
  `fastapi.py`/`flask.py`/`django.py`/`logging.py` integrations.
- `sentry_sdk/envelope.py` — envelope serialization.

## Takeaways we take

1. **`contextvars`-backed scope** — same shape, smaller surface. We
   ship `Scope.current()` + `push_scope()` and skip the multi-Hub
   layering entirely.
2. **Envelope wire format** — header line + item-header line +
   item-body line, newline-separated, with `event_id`, `sent_at`, and
   `dsn` in the header. Retrace's existing
   `parse_sentry_envelope` accepts the same bytes — no server work
   needed.
3. **Background-thread transport with bounded queue.** Their drop
   policy varies; we pick **drop-oldest** explicitly so the freshest
   crash always wins on overflow.
4. **`Integration` base class** with `setup(client)` for explicit
   wiring. Lazy framework imports inside `setup()` / `attach()` so a
   plain `import retrace_sdk` doesn't pull in Flask/Django/FastAPI.
5. **`before_send(event) -> event | None`** as the user-supplied
   last-mile hook. Returning `None` drops the event silently.
6. **stdlib `logging.Handler`** — INFO+ → breadcrumb, ERROR+ → event,
   `log.exception()` (which carries `exc_info`) becomes an exception
   event with a stacktrace. Configurable thresholds per integration
   instance.
7. **No-op when DSN is empty/invalid.** Sentry's SDK has this
   property and it's load-bearing — it means library authors can
   leave `capture_*` calls in code that also runs in environments
   without a server, with no try/except wrapping.

## What we deliberately don't take

- **Hub / multi-tenant layering.** Sentry's `Hub` lets one process
  carry multiple clients with different DSNs. Useful for SaaS hosting
  the SDK; overkill for the indie-dev use case. We expose a single
  global client. If somebody asks for multi-tenant we'll add a `Hub`
  layer in front of the existing `Client` without breaking the public
  API.
- **`urllib3` / `httpx` dependency.** Sentry-python now requires
  `urllib3`. We use `urllib.request` from the stdlib — slightly less
  robust (no built-in retries) but zero dependency cost.
- **Auto-loading integrations via `setup_integrations`.** Sentry
  registers default integrations when no list is passed. We require
  explicit opt-in (`integrations=[…]`) — keeps the import graph
  predictable and avoids surprise framework imports.
- **OpenTelemetry-as-a-first-class-citizen.** Sentry has a heavy OTel
  integration. Retrace already accepts OTel directly via
  `otel_ingest.py`; the SDK doesn't need to bridge.
- **Performance tracing / spans.** Sentry has full `Transaction`
  /`Span` machinery. We skip for now — `traces_sample_rate` is a
  no-op kwarg that exists so `init()` matches the Sentry shape but
  doesn't yet enable per-span events. Coming back when we have a
  story for spans in `qa_incident`.
- **Settings-based DSN.** Sentry-python pulls DSN from env vars
  (`SENTRY_DSN`) by default. We take it as an explicit kwarg so
  there's no implicit behavior; the user can read env themselves
  if they want that.

## Files in this SDK that map back

- `dsn.py` ← `sentry_sdk/transport.py` parse_dsn
- `scope.py` ← `sentry_sdk/scope.py`
- `client.py` ← `sentry_sdk/client.py` (slimmed, no Hub)
- `transport.py` ← `sentry_sdk/transport.py` (`HttpTransport`)
- `envelope.py` ← `sentry_sdk/envelope.py`
- `integrations/_base.py` ← `sentry_sdk/integrations/__init__.py`
  `Integration` base
- `integrations/fastapi.py` ← `sentry_sdk/integrations/fastapi.py`
  (ASGI middleware approach)
- `integrations/flask.py` ← `sentry_sdk/integrations/flask.py`
  (`got_request_exception` + `before_request`)
- `integrations/django.py` ← `sentry_sdk/integrations/django/__init__.py`
  (middleware class)
- `integrations/logging.py` ← `sentry_sdk/integrations/logging.py`
