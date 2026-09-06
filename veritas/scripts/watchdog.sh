#!/usr/bin/env bash
# Veritas 24/7/365 uptime watchdog (architecture §13 Q2). The lean, MVP-host
# choice: a lightweight supervisor loop that (a) keeps the uvicorn service alive
# and (b) restarts it when /health stops answering. It alerts the owner by writing
# to a durable alert log (no third-party email at MVP — real email needs a paid
# plan; the in-app owner review queue surfaces these alongside it).
#
# Choice rationale: a systemd unit is ideal where systemd is available (see
# veritas.service), but a portable watchdog works identically on hosts without it
# and is what we run this box on. Both reduce to the same contract: probe /health,
# restart on failure.
#
# Config (environment):
#   VERITAS_HEALTH_URL   health probe (default http://127.0.0.1:3000/health)
#   VERITAS_ALERT_LOG    alert log path (default /var/log/veritas-watchdog.log)
#   VERITAS_SERVICE_CMD  the command that starts the service (default uvicorn ...)
#   VERITAS_WATCH_INTERVAL  probe interval seconds (default 10)
#   VERITAS_MAX_FAILS    consecutive failures before restart (default 3)
set -euo pipefail

HEALTH_URL="${VERITAS_HEALTH_URL:-http://127.0.0.1:3000/health}"
ALERT_LOG="${VERITAS_ALERT_LOG:-/var/log/veritas-watchdog.log}"
SERVICE_CMD="${VERITAS_SERVICE_CMD:-uvicorn app.main:app --host 0.0.0.0 --port 3000}"
INTERVAL="${VERITAS_WATCH_INTERVAL:-10}"
MAX_FAILS="${VERITAS_MAX_FAILS:-3}"

alert() { echo "[$(date -Iseconds)] watchdog: $*" >> "$ALERT_LOG"; }

start_service() {
  alert "starting service"
  ( cd "$(dirname "$0")/.." && setsid nohup $SERVICE_CMD >> "$ALERT_LOG" 2>&1 & )
}

# Boot once; if the probe isn't up within a grace window, start the service.
fails=0
if ! curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1; then
  alert "health down at boot"
  start_service
fi

while true; do
  sleep "$INTERVAL"
  if curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1; then
    fails=0
    continue
  fi
  fails=$((fails + 1))
  alert "health check failed ($fails/$MAX_FAILS)"
  if [ "$fails" -ge "$MAX_FAILS" ]; then
    alert "restarting service after $MAX_FAILS failures"
    pkill -f 'uvicorn app.main' 2>/dev/null || true
    sleep 2
    start_service
    fails=0
  fi
done
