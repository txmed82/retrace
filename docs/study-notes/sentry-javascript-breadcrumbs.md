# Study note: sentry-javascript breadcrumbs

**Studied 2026-05-11.** Cross-referenced sentry-javascript (HEAD) and
datadog/browser-sdk (HEAD).

## What we read

- `getsentry/sentry-javascript`
  - `packages/browser/src/integrations/breadcrumbs.ts` — DOM / fetch /
    XHR / console / history hooks.
  - `packages/core/src/scope.ts` — `addBreadcrumb()`,
    `_breadcrumbs` ring, `_eventProcessors`.
  - `packages/browser/src/transports/fetch.ts` — `instrumentFetch`
    pattern that monkey-patches `globalThis.fetch`.
  - `packages/browser/src/integrations/globalhandlers.ts` —
    `onerror` / `onunhandledrejection` capture and breadcrumb
    attachment.
- `DataDog/browser-sdk`
  - The "single SDK for rum + logs + errors" layout — instructive
    for what NOT to do at our scale.

## Takeaways we took

1. **Categories**: `ui.click`, `http`, `console`, `navigation`,
   `error`. Sentry standardized these and most observability
   tooling (LogRocket, Datadog, Honeycomb) follow the same vocab.
2. **`addBreadcrumb(b)` on the client surface.** Manual breadcrumbs
   are how library authors expose their own state (e.g. "auth:
   login attempt") without imposing on the host app's logger.
3. **Ring buffer with `maxBreadcrumbs`.** Sentry caps at 100; we
   chose 50 by default because every exception event copies the
   trail, so the bytes-per-error tradeoff is steeper. Clamped to
   `[1, 500]` so a malicious `init({maxBreadcrumbs: 1e9})` can't
   OOM the page.
4. **Snapshot before recording the error-as-breadcrumb.** The
   exception event's `breadcrumbs` field is the trail LEADING UP TO
   the crash — the error itself goes onto the trail *after* we
   serialize it. Sentry does the same; we'd otherwise serialize a
   useless self-reference.
5. **`maskTextSelector` extended to breadcrumbs.** rrweb's
   `maskTextSelector` already masks DOM-mutation events; we extend
   the same selector to click breadcrumbs so a masked element's
   text can't leak via the trail. The Python side then sees a
   click breadcrumb like `button#secret` without the visible text.
6. **History wrapping over MutationObserver** for navigation. SPA
   route changes don't fire `popstate`; we monkey-patch
   `history.pushState` / `history.replaceState` plus listen for
   `popstate` / `hashchange` to cover all four routing paths.

## What we deliberately don't take

- **The `Severity` enum.** Sentry has 5 enum members and a duplicate
  string-or-enum API. We expose the strings (`debug` / `info` /
  `warning` / `error` / `fatal`) and skip the enum tax. The Python
  side normalizes lowercased strings anyway.
- **The `eventProcessors` hook chain.** Sentry lets users register
  N processors that mutate the event before send. For Retrace we
  have a simpler `redactText` pass in the SDK already; a full
  processor chain is power without a clear caller yet.
- **The `Hub` / `Scope.run()` API.** Browser SDKs that need
  multi-tenant scoping use Sentry's `Hub`. We keep one global
  `RetraceClient` per page — there's no second tenant in a
  browser tab.
- **Performance traces and spans.** Sentry's breadcrumbs are
  half-merged with their `Span` system. We keep breadcrumbs as a
  pure error-context tool. The replay-channel already carries
  rich timing info for our use case.

## Server-side mirror (`monitoring_ingest.py`)

The Sentry envelope's `event.breadcrumbs.values` is exactly the
shape both their and our SDK send. The new helpers in
`monitoring_ingest.py`:

- `_breadcrumbs_from_sentry(event)` — accepts both the canonical
  `{"values": [...]}` wrapper and a bare list (some older SDKs).
- `_console_excerpts_from_breadcrumbs(crumbs)` — promotes
  `category=console|log` entries to the `console_excerpts` field
  the `qa_incident_bridge` already reads.
- `_network_failures_from_breadcrumbs(crumbs)` — promotes
  `category=http|fetch|xhr` entries with `status >= 400` OR an
  explicit `error` to the `network_failures` field.
- The raw trail is also kept in `metadata.breadcrumbs` so future
  consumers (the repair prompt, the dashboard) can see the full
  sequence.

## Files in this PR that map back

- `packages/browser/src/index.ts` ← Sentry's
  `breadcrumbs.ts`/`globalhandlers.ts` (shape only — code is original).
- `packages/browser/tests/breadcrumbs.test.ts` ← inspired by
  Sentry's `packages/browser/test/unit/integrations/breadcrumbs.test.ts`.
- `src/retrace/monitoring_ingest.py` (`_breadcrumbs_from_sentry`
  and friends) ← original; integrates with the existing
  `_sentry_alert` flow.
