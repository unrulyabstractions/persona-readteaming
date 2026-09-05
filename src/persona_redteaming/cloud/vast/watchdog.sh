#!/usr/bin/env bash
# watchdog.sh — the LOCAL hard money bound. Armed by 01_launch.sh the moment
# the box is ours; safe to run by hand in a second terminal (extra copies just
# watch). ADAPTED from workspace/axis-run/runbook/watchdog.sh; the teardown
# chain here is 04_capture.sh -> 05_destroy.sh.
#
# Every WATCHDOG_POLL_SEC:
#   - reads the instance row (three-state; an API error is NEVER "gone");
#   - refreshes $STATE_DIR/dph from the LIVE dph_total (measured ~+7% over the
#     offer price — an unenforced budget cap is decoration);
#   - prints elapsed time and elapsed cost;
#   - past MAX_HOURS or past MAX_DOLLARS: if the heartbeat file is fresh, grants
#     grace up to the cap + WATCHDOG_GRACE_MIN with loud warnings; otherwise —
#     and unconditionally past the grace bound — runs the teardown chain.
#
# A failed capture NEVER leads to a destroy. The box stays alive and the alert
# repeats every cycle: data outranks money, and money stays bounded because the
# operator is alerted continuously.
#
# The vast API key never goes to the box; this loop is local by design.
set -uo pipefail
case "$-" in *x*) echo "FATAL: xtrace on in a secret-adjacent script"; exit 90;; esac
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

HEARTBEAT="${HEARTBEAT:-$STATE_DIR/heartbeat}"
log "watchdog: MAX_HOURS=$MAX_HOURS MAX_DOLLARS=\$$MAX_DOLLARS grace=${WATCHDOG_GRACE_MIN}min \
heartbeat-fresh=${HEARTBEAT_FRESH_MIN}min poll=${WATCHDOG_POLL_SEC}s"
log "watchdog: touch $HEARTBEAT from the campaign driver to earn the grace window"

while true; do
  INSTANCE="$(instance_id)"
  if [ -z "$INSTANCE" ]; then
    log "watchdog: no instance recorded — exiting"
    exit 0
  fi
  assert_not_foreign "$INSTANCE"

  row="$(instance_row_retry "$INSTANCE")"
  case "$row" in
    absent)
      log "watchdog: instance $INSTANCE no longer listed (destroyed elsewhere) — exiting"
      exit 0 ;;
    error*)
      log "watchdog: API error persists — cannot see the box; concluding NOTHING; retrying"
      sleep "$WATCHDOG_POLL_SEC"; continue ;;
  esac
  assert_row_label "$row" "$INSTANCE"

  LIVE_DPH="$(printf '%s' "$row" | cut -f5)"
  [ -n "$LIVE_DPH" ] && echo "$LIVE_DPH" > "$STATE_DIR/dph"

  t0="$(cat "$STATE_DIR/launch_epoch" 2>/dev/null || echo 0)"
  now=$(date +%s)
  elapsed_min=$(( (now - t0) / 60 ))
  cap_min=$(python3 -c "print(int($MAX_HOURS*60))")
  hard_min=$(( cap_min + WATCHDOG_GRACE_MIN ))
  spent="$(python3 -c "print(f'{($now-$t0)/3600*${LIVE_DPH:-0}:.2f}')")"
  over_dollars=$(python3 -c "print(1 if $spent >= $MAX_DOLLARS else 0)")
  log "watchdog: $(cost_status) | elapsed ${elapsed_min}min (cap ${cap_min}, hard ${hard_min}) \
| live \$${LIVE_DPH:-?}/hr | spent \$${spent}"

  if [ "$elapsed_min" -ge "$cap_min" ] || [ "$over_dollars" = "1" ]; then
    [ "$over_dollars" = "1" ] && log "watchdog: BUDGET CAP hit (\$${spent} >= \$${MAX_DOLLARS} at the LIVE rate)"
    hb_fresh=0
    if [ -f "$HEARTBEAT" ]; then
      hb_mtime=$(stat -f %m "$HEARTBEAT" 2>/dev/null || stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0)
      hb_age_min=$(( (now - hb_mtime) / 60 ))
      [ "$hb_age_min" -lt "$HEARTBEAT_FRESH_MIN" ] && hb_fresh=1
    fi
    if [ "$hb_fresh" = "1" ] && [ "$elapsed_min" -lt "$hard_min" ] && [ "$over_dollars" = "0" ]; then
      log "watchdog: PAST MAX_HOURS but the heartbeat is fresh — grace until ${hard_min}min. WRAP UP NOW."
    else
      log "watchdog: CAP REACHED — capture, then destroy (the capture gate stays absolute)"
      if INSTANCE="$INSTANCE" QC_CONFIG="$QC_CONFIG" bash "$QC_HERE/04_capture.sh"; then
        MARKER="$STATE_DIR/CAPTURE_OK_$INSTANCE"
        log "watchdog: capture gate GREEN — destroying"
        if INSTANCE="$INSTANCE" QC_CONFIG="$QC_CONFIG" bash "$QC_HERE/05_destroy.sh" \
             --marker "$MARKER" --yes-i-am-really-sure; then
          log "watchdog: destroyed at the cap. Done."
          exit 0
        else
          log "watchdog: DESTROY FAILED — the box may still bill; retrying next cycle"
        fi
      else
        log "watchdog: CAPTURE FAILED — box stays ALIVE (data outranks money). FIX AND RE-RUN 04. Retrying next cycle."
      fi
    fi
  fi
  sleep "$WATCHDOG_POLL_SEC"
done
