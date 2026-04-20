#!/usr/bin/env bash
set -euo pipefail

CRON_SCHEDULE="${RETRACE_CRON:-0 */6 * * *}"

# Write a crontab entry that runs `retrace run` on schedule.
CRON_FILE=/etc/cron.d/retrace
cat > "$CRON_FILE" <<CRON
$CRON_SCHEDULE root cd /app && retrace run >> /app/data/retrace.log 2>&1
CRON
chmod 0644 "$CRON_FILE"
crontab "$CRON_FILE"

# Run once at startup so first report exists before first cron tick.
retrace run >> /app/data/retrace.log 2>&1 || true

# Foreground cron so container stays alive.
exec cron -f
