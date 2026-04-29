#!/usr/bin/env bash
set -euo pipefail

CRON_SCHEDULE="${RETRACE_CRON:-0 */6 * * *}"
DIGEST_CRON_SCHEDULE="${RETRACE_DIGEST_CRON:-0 8 * * *}"
VERIFY_CRON_SCHEDULE="${RETRACE_VERIFY_CRON:-30 8 * * *}"
SYNC_CRON_SCHEDULE="${RETRACE_SYNC_TICKETS_CRON:-15 * * * *}"
MODE="${RETRACE_MODE:-cron}"

case "$MODE" in
  api)
    exec retrace api serve --host "${RETRACE_API_HOST:-0.0.0.0}" --port "${RETRACE_API_PORT:-8788}"
    ;;
  ui)
    exec retrace ui --host "${RETRACE_UI_HOST:-0.0.0.0}" --port "${RETRACE_UI_PORT:-8787}"
    ;;
  worker)
    while true; do
      retrace api process-replays --limit "${RETRACE_WORKER_LIMIT:-25}" >> /app/data/retrace-worker.log 2>&1 || true
      sleep "${RETRACE_WORKER_INTERVAL_SECONDS:-10}"
    done
    ;;
  browser-runner)
    mkdir -p /app/data/ui-tests/specs /app/data/ui-tests/runs /app/data/ui-tests/cache
    exec retrace tester worker --interval "${RETRACE_BROWSER_RUNNER_INTERVAL_SECONDS:-30}"
    ;;
  cron)
    # Write a crontab entry that runs `retrace run` plus the daily product
    # loops (digest, verify-resolved, sync-tickets) on their own schedules.
    CRON_FILE=/etc/cron.d/retrace
    cat > "$CRON_FILE" <<CRON
$CRON_SCHEDULE root cd /app && retrace run >> /app/data/retrace.log 2>&1
$DIGEST_CRON_SCHEDULE root cd /app && retrace digest --notify >> /app/data/retrace-digest.log 2>&1
$VERIFY_CRON_SCHEDULE root cd /app && retrace api verify-resolved >> /app/data/retrace-verify.log 2>&1
$SYNC_CRON_SCHEDULE root cd /app && retrace api sync-tickets >> /app/data/retrace-sync.log 2>&1
CRON
    chmod 0644 "$CRON_FILE"
    crontab "$CRON_FILE"

    # Run once at startup so first report exists before first cron tick.
    retrace run >> /app/data/retrace.log 2>&1 || true

    # Foreground cron so container stays alive.
    exec cron -f
    ;;
  *)
    exec "$@"
    ;;
esac
