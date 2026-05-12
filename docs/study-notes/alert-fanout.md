# Study note: alert fan-out (Slack / Discord / PagerDuty / webhook)

**Studied 2026-05-12.** Cross-referenced GlitchTip, SigNoz, and
Grafana's alerting model.

## What we read

- [GlitchTip](https://gitlab.com/glitchtip/glitchtip-backend) (HEAD)
  - `apps/alerts/` — Django models for `AlertRecipient` etc.
  - `apps/events/views/` — where alert fan-out is triggered after
    event normalization.
- [SigNoz](https://github.com/SigNoz/signoz) (HEAD)
  - `pkg/alertmanager/` — receivers + routing config.
- [Grafana alerting](https://github.com/grafana/grafana) (HEAD) — the
  routing tree + dedup-key semantics are the canonical reference.

## Takeaways we took

1. **Per-target payload, not a one-size-fits-all envelope.** Slack
   wants Block Kit; Discord wants embeds; PagerDuty wants Events v2
   shape. A "generic webhook" target ships the raw normalized alert.
   Each builder is a small pure function in `alert_dispatch.py`.
2. **Dedup-key parity.** PD's `dedup_key`, ours in
   `alert_dispatches.fingerprint`, and Grafana's `groupKey` all play
   the same role. We expose a single `dedup_window_seconds` knob per
   route (default 300) so operators can tune per channel.
3. **Severity floor per route**, not just per rule. The rule
   layer evaluates "is this an alert?"; the route layer evaluates "is
   this important enough for THIS channel?". A `critical-only`
   PagerDuty route can coexist with a `>=low` archive webhook.
4. **`rule_name` scoping.** A route either matches one named rule
   (`rule_name="prod-crash"`) or matches every alert (`rule_name=""`).
   Mirrors Grafana's "specific route + default route" tree without
   needing a full routing-tree DSL.
5. **Persist every dispatch attempt.** The `alert_dispatches` table
   is both audit log and dedup ledger. `recent_alert_dispatch_for`
   queries it; the CLI can read it for operator visibility (future
   `retrace monitor route history` command).
6. **Best-effort wire-up at the ingest site.**
   `monitoring_ingest.py` calls `dispatch_alert()` after the failure
   and incident are persisted; any dispatcher exception is caught
   so a dead Slack webhook can't roll back error capture.
7. **Stdlib-only HTTP.** Same philosophy as the Python SDK: no
   `httpx`/`urllib3`/`requests` dep on the server. `urllib.request`
   covers the four target kinds and gives us deterministic timeouts.

## What we deliberately don't take

- **A scheduler for stateful alerts** (e.g. "more than N errors in
  5min"). GlitchTip and SigNoz both run a background job for this.
  Retrace's current rules are reactive (per-event); the stateful
  flavor is a P1.1 follow-up, not part of this ship.
- **A routing-tree DSL.** Grafana has a powerful nested routing
  tree with inheritance. We collapse to `{rule_name, min_severity}`
  filters per route. If users want a third-axis matcher (matchers
  on the alert metadata, e.g. `service=billing`), that's an
  additive change to `alert_routes.match_*` columns later.
- **Signing for webhook targets.** PD has the routing key; Slack
  and Discord use bearer URLs. Generic webhook targets *will* want
  HMAC signing, but that's a 30-minute follow-up; for v1 the URL
  itself is the credential.
- **Channel-specific render templates.** GlitchTip lets users
  customize Slack templates. We ship one opinionated card per
  channel; if a deployment wants its own templates that's a
  follow-up.

## Files in this PR that map back

- `src/retrace/alert_dispatch.py` ← shape inspired by GlitchTip's
  `apps/alerts/` recipient dispatch; payload builders original.
- `src/retrace/storage.py` (`alert_routes`, `alert_dispatches` tables)
  ← GlitchTip-style `AlertRecipient` + audit log.
- `src/retrace/commands/monitor.py` (`route` subgroup) ← UX shape
  modeled on `kubectl get/create/delete` style.
- `tests/test_alert_dispatch.py` ← payload-shape contract tests so
  future renames are loud.
