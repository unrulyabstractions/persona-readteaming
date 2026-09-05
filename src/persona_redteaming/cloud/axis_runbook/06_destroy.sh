#!/usr/bin/env bash
# 06_destroy.sh — tear the box down. REFUSES unless BOTH gates are green:
#   capture  : $STATE_DIR/CAPTURE_OK_<instance>  (05, byte-verified sweep)
#   upload   : $STATE_DIR/UPLOAD_OK_<instance>   (04, xet-verified bucket gate)
# Vendored from workspace/vast-harness/vast/05_destroy.sh @0c418be; changes:
# the additional upload gate, per-model state, foreign-instance guard.
#
# Requirements, all hard:
#   --marker <path>            the CAPTURE_OK_<instance> file 05 wrote
#   --yes-i-am-really-sure     explicit irreversibility acknowledgment
# The marker must name THIS instance. If the box was written to after the
# capture sweep, destroy refuses: re-run 05. After destroying, verifies the
# instance is actually gone by walking every instances-v1 page.
#
# Sole exception: --never-provisioned destroys a dud box that never took our
# code (no provision marker from 02 exists). The moment 02 succeeds, that
# path is closed forever.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_instance

MARKER=""; CONFIRM=0; NEVER_PROVISIONED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --marker) MARKER="${2:-}"; shift 2 ;;
    --yes-i-am-really-sure) CONFIRM=1; shift ;;
    --never-provisioned) NEVER_PROVISIONED=1; shift ;;
    *) die "unknown arg: $1" ;;
  esac
done

echo "==============================================================="
echo " DESTROY REQUEST for vast instance: $INSTANCE ($MODEL_TAG)"
cost_status
echo "==============================================================="

[ "$CONFIRM" = "1" ] || die "refusing: pass --yes-i-am-really-sure (destroy is irreversible)"
if [ "$NEVER_PROVISIONED" = "1" ]; then
  [ ! -f "$STATE_DIR/provisioned_$INSTANCE" ] \
    || die "refusing --never-provisioned: 02 DID provision $INSTANCE; run 04+05"
  log "--never-provisioned accepted: no provision marker for $INSTANCE"
else
  [ -n "$MARKER" ] || die "refusing: pass --marker <path-to-CAPTURE_OK file> (run 05_capture.sh first)"
  [ -f "$MARKER" ] || die "refusing: marker $MARKER does not exist (05 did not succeed)"
  grep -q "^instance=$INSTANCE$" "$MARKER" \
    || die "refusing: marker is for '$(grep '^instance=' "$MARKER" 2>/dev/null)', not instance $INSTANCE"
  # The SECOND gate: verified bucket upload (mission rule: destroy is gated
  # on capture AND upload verification both green).
  [ -f "$STATE_DIR/UPLOAD_OK_$INSTANCE" ] \
    || die "refusing: no UPLOAD_OK_$INSTANCE (run 04_upload_final.sh — the xet-verified gate must pass)"
  grep -q "^gate=FINAL_GATE_RC=0" "$STATE_DIR/UPLOAD_OK_$INSTANCE" \
    || die "refusing: UPLOAD_OK marker exists but does not record a passing gate"
fi

# Freshness: nothing on the box may be newer than the capture SWEEP.
row="$(instance_row_retry "$INSTANCE")"
case "$row" in
  error*) die "cannot see instance $INSTANCE (API error persists) — NOT destroying blind" ;;
esac
if [ "$row" = "absent" ]; then
  log "instance $INSTANCE not listed (3 clean page-walks) — already gone; archiving state"
elif [ "$NEVER_PROVISIONED" = "1" ]; then
  log "skipping freshness check (--never-provisioned: nothing of ours on the box)"
  log "destroying instance $INSTANCE"
  printf 'y\n' | vastai destroy instance "$INSTANCE"
  sleep 5
else
  resolve_ssh_target
  # Reference = sweep_epoch (stamped BEFORE 05's find), minus 1s so a file
  # written in the sweep's own second still trips the check; the safe
  # direction is a spurious refusal.
  SWEEP_EPOCH=$(grep '^sweep_epoch=' "$MARKER" | cut -d= -f2)
  [ -n "$SWEEP_EPOCH" ] || die "marker has no sweep_epoch= — re-run 05_capture.sh"
  REF_EPOCH=$((SWEEP_EPOCH - 1))
  EXCLUDES_FIND="$(capture_excludes_find)"   # SAME list as 05's manifest sweep
  NEWER=$(vssh "find /root /workspace -type f -newermt @$REF_EPOCH \
      $EXCLUDES_FIND 2>/dev/null | head -5" </dev/null || echo "SSH_FAILED")
  if [ "$NEWER" = "SSH_FAILED" ]; then
    log "WARNING: cannot ssh to verify freshness; proceeding on marker alone (box may be dead)"
  elif [ -n "$NEWER" ]; then
    echo "$NEWER" | while read -r f; do log "  newer than capture: $f"; done
    die "refusing: files written AFTER the capture — re-run 05_capture.sh"
  fi

  log "destroying instance $INSTANCE"
  # The CLI prompts and exits 0 even on 'Aborted' — feed the confirmation.
  printf 'y\n' | vastai destroy instance "$INSTANCE"
  sleep 5
fi

# Verify gone: walk every page; 'absent' after clean walks is the ONLY
# acceptable answer — an API error is NOT "gone".
for i in $(seq 1 10); do
  row="$(instance_row "$INSTANCE")"
  case "$row" in
    absent) break ;;
    error*) log "[$i] API error while verifying ($(printf '%s' "$row" | cut -f2-)); retrying" ;;
    *)      log "[$i] still listed ($(printf '%s' "$row" | cut -f2)); waiting" ;;
  esac
  sleep 6
done
if [ "$row" != "absent" ]; then
  case "$row" in
    error*) die "cannot CONFIRM instance $INSTANCE is gone (API errors) — NOT archiving id; re-run 06 to re-verify" ;;
    *)      die "instance $INSTANCE STILL LISTED after destroy — NOT archiving id; investigate" ;;
  esac
fi

if [ -f "$STATE_DIR/instance_id" ] && [ "$(cat "$STATE_DIR/instance_id")" = "$INSTANCE" ]; then
  mv "$STATE_DIR/instance_id" "$STATE_DIR/destroyed_${INSTANCE}_$(date -u +%Y%m%dT%H%M%SZ)"
fi
{
  echo "destroyed=$INSTANCE model=$MODEL_TAG date=$(date -u +%FT%TZ)"
  if [ "$NEVER_PROVISIONED" = "1" ]; then
    echo "capture=NONE (--never-provisioned; box never took our code)"
  else
    echo "capture_marker=$MARKER"
    cat "$MARKER"
    echo "upload_marker=$STATE_DIR/UPLOAD_OK_$INSTANCE"
    cat "$STATE_DIR/UPLOAD_OK_$INSTANCE"
  fi
} >> "$STATE_DIR/DESTROY_LOG.txt"
log "instance $INSTANCE destroyed and VERIFIED gone (all pages walked). Billing stopped."
log "log the id + files swept/captured/lost(0) + destroy in runbook/VERIFICATION_LOG.md and temp/44"
