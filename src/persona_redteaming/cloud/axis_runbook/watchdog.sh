#!/usr/bin/env bash
# watchdog.sh — LOCAL hard money bound. Started by 01_launch.sh; safe to run
# by hand in a second terminal too (extra copies just watch). Vendored from
# workspace/vast-harness/vast/watchdog.sh @0c418be; change: the time-cap
# teardown chain is 04_upload_final -> 05_capture -> 06_destroy, and destroy
# fires ONLY when BOTH gates are green.
#
# Every 5 min:
#   - reads the instance state (three-state; API errors are never "gone");
#     refreshes $STATE_DIR/dph from the live row; prints elapsed cost.
#   - past MAX_HOURS: if the heartbeat file (touched by 03 while monitoring)
#     is fresh, grants grace up to MAX_HOURS + WATCHDOG_GRACE_MIN with loud
#     warnings; otherwise — and unconditionally past the grace bound — runs
#     the teardown chain. A failed upload or capture NEVER leads to destroy:
#     the box stays up and the alert repeats every cycle (data outranks
#     money; money stays bounded because the operator is alerted continuously).
#
# The vast API key never goes to the box; this loop is local by design.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

HEARTBEAT="$STATE_DIR/heartbeat"
log "watchdog: MAX_HOURS=$MAX_HOURS grace=${WATCHDOG_GRACE_MIN}min heartbeat-fresh=${HEARTBEAT_FRESH_MIN}min"

while true; do
  INSTANCE="$(instance_id)"
  if [ -z "$INSTANCE" ]; then
    log "watchdog: no instance recorded — exiting"
    exit 0
  fi

  row="$(instance_row_retry "$INSTANCE")"
  case "$row" in
    absent)
      log "watchdog: instance $INSTANCE no longer listed (destroyed elsewhere) — exiting"
      exit 0 ;;
    error*)
      log "watchdog: API error persists — cannot see the box; NOT concluding anything; retrying"
      sleep 300; continue ;;
  esac
  LIVE_DPH="$(printf '%s' "$row" | cut -f5)"
  [ -n "$LIVE_DPH" ] && echo "$LIVE_DPH" > "$STATE_DIR/dph"

  t0="$(cat "$STATE_DIR/launch_epoch" 2>/dev/null || echo 0)"
  now=$(date +%s)
  elapsed_min=$(( (now - t0) / 60 ))
  cap_min=$(python3 -c "print(int($MAX_HOURS*60))")
  hard_min=$(( cap_min + WATCHDOG_GRACE_MIN ))
  log "watchdog: $(cost_status) | elapsed ${elapsed_min}min (cap ${cap_min}, hard ${hard_min})"

  if [ "$elapsed_min" -ge "$cap_min" ]; then
    hb_fresh=0
    if [ -f "$HEARTBEAT" ]; then
      hb_age_min=$(( (now - $(stat -f %m "$HEARTBEAT" 2>/dev/null || stat -c %Y "$HEARTBEAT")) / 60 ))
      [ "$hb_age_min" -lt "$HEARTBEAT_FRESH_MIN" ] && hb_fresh=1
    fi
    if [ "$hb_fresh" = "1" ] && [ "$elapsed_min" -lt "$hard_min" ]; then
      log "watchdog: PAST MAX_HOURS but heartbeat is fresh — grace until ${hard_min}min. WRAP UP NOW."
    else
      log "watchdog: TIME CAP — upload-verify, capture, then destroy (both gates stay absolute)"
      if ! INSTANCE="$INSTANCE" AXIS_CONFIG="$AXIS_CONFIG" bash "$AXIS_LIB_HERE/04_upload_final.sh"; then
        log "watchdog: FINAL UPLOAD GATE FAILED — box stays ALIVE; FIX AND RERUN 04. Retrying next cycle."
        sleep 300; continue
      fi
      if INSTANCE="$INSTANCE" AXIS_CONFIG="$AXIS_CONFIG" bash "$AXIS_LIB_HERE/05_capture.sh"; then
        MARKER="$STATE_DIR/CAPTURE_OK_$INSTANCE"
        log "watchdog: both gates green — destroying"
        INSTANCE="$INSTANCE" AXIS_CONFIG="$AXIS_CONFIG" bash "$AXIS_LIB_HERE/06_destroy.sh" \
          --marker "$MARKER" --yes-i-am-really-sure \
          && { log "watchdog: destroyed at time cap. Done."; exit 0; } \
          || log "watchdog: DESTROY FAILED — box may still bill; will retry next cycle"
      else
        log "watchdog: CAPTURE FAILED — box stays ALIVE (data outranks money); FIX AND RERUN 05. Retrying next cycle."
      fi
    fi
  fi
  sleep 300
done
