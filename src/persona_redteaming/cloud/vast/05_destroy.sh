#!/usr/bin/env bash
# 05_destroy.sh — tear the box down. REFUSES unless the capture gate is green:
#   $STATE_DIR/CAPTURE_OK_<instance>   (written by 04, byte-verified sweep)
# ADAPTED from workspace/axis-run/runbook/06_destroy.sh.
#
# Requirements, all hard:
#   --marker <path>            the CAPTURE_OK_<instance> file that 04 wrote
#   --yes-i-am-really-sure     explicit acknowledgment that this is irreversible
# The marker must name THIS instance. If anything on the box was written after
# the capture sweep, destroy refuses and tells you to re-run 04. After
# destroying, it proves the instance is gone by walking EVERY instances-v1
# page — an API error is never "gone".
#
# Sole exception: --never-provisioned destroys a dud box that never took our
# code (no provision marker from 02 exists). The moment 02 succeeds that path
# closes forever.
set -euo pipefail
case "$-" in *x*) echo "FATAL: xtrace on in a secret-adjacent script"; exit 90;; esac
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_cmd vastai
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
echo " DESTROY REQUEST for vast instance: $INSTANCE ($WORKSTREAM)"
cost_status
echo "==============================================================="

[ "$CONFIRM" = "1" ] || die "refusing: pass --yes-i-am-really-sure (destroy is irreversible)"
if [ "$NEVER_PROVISIONED" = "1" ]; then
  [ ! -f "$STATE_DIR/provisioned_$INSTANCE" ] \
    || die "refusing --never-provisioned: 02 DID provision $INSTANCE; run 04_capture.sh first"
  log "--never-provisioned accepted: no provision marker for $INSTANCE"
else
  [ -n "$MARKER" ] || die "refusing: pass --marker <path-to-CAPTURE_OK file> (run 04_capture.sh first)"
  [ -f "$MARKER" ] || die "refusing: marker $MARKER does not exist (04 did not succeed)"
  grep -q "^instance=$INSTANCE\$" "$MARKER" \
    || die "refusing: marker is for '$(grep '^instance=' "$MARKER" 2>/dev/null)', not instance $INSTANCE"
  grep -q "^files_lost=0\$" "$MARKER" \
    || die "refusing: marker does not record files_lost=0 — re-run 04_capture.sh"
fi

# Three-state read; ownership re-asserted from the live row before any destroy.
row="$(instance_row_retry "$INSTANCE")"
case "$row" in
  error*) die "cannot see instance $INSTANCE (API error persists) — NOT destroying blind" ;;
esac
if [ "$row" = "absent" ]; then
  log "instance $INSTANCE not listed (3 clean page-walks) — already gone; archiving state"
else
  assert_row_label "$row" "$INSTANCE"
  log "ownership confirmed: instance $INSTANCE is labeled '$(printf '%s' "$row" | cut -f6)'"
  if [ "$NEVER_PROVISIONED" = "1" ]; then
    log "skipping the freshness check (--never-provisioned: nothing of ours reached the box)"
  else
    resolve_ssh_target
    assert_box_identity
    # Reference = sweep_epoch (stamped BEFORE 04's find), minus 1s so a file
    # written inside the sweep's own second still trips the check. The safe
    # direction is a spurious refusal.
    SWEEP_EPOCH="$(grep '^sweep_epoch=' "$MARKER" | cut -d= -f2)"
    [ -n "$SWEEP_EPOCH" ] || die "marker has no sweep_epoch= — re-run 04_capture.sh"
    SWEEP_ROOTS="$(grep '^sweep_roots=' "$MARKER" | cut -d= -f2-)"
    [ -n "$SWEEP_ROOTS" ] || die "marker has no sweep_roots= — re-run 04_capture.sh"
    REF_EPOCH=$((SWEEP_EPOCH - 1))
    EXCLUDES_FIND="$(capture_excludes_find)"   # the SAME list 04's sweep used
    NEWER=$(vssh "find $SWEEP_ROOTS -type f -newermt @$REF_EPOCH \
        $EXCLUDES_FIND 2>/dev/null | head -5" </dev/null || echo "SSH_FAILED")
    if [ "$NEWER" = "SSH_FAILED" ]; then
      log "WARNING: cannot ssh to verify freshness; proceeding on the marker alone (box may be dead)"
    elif [ -n "$NEWER" ]; then
      echo "$NEWER" | while read -r f; do log "  newer than the capture: $f"; done
      die "refusing: files were written AFTER the capture sweep — re-run 04_capture.sh"
    else
      log "freshness OK: nothing under $SWEEP_ROOTS is newer than the capture sweep"
    fi
  fi
  log "destroying instance $INSTANCE"
  # The CLI prompts and exits 0 even on 'Aborted' — feed it the confirmation.
  printf 'y\n' | vastai destroy instance "$INSTANCE"
  sleep 5
fi

# Verify gone by walking EVERY page. 'absent' after a clean walk is the only
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
    error*) die "cannot CONFIRM instance $INSTANCE is gone (API errors) — NOT archiving the id; re-run 05 to re-verify" ;;
    *)      die "instance $INSTANCE STILL LISTED after destroy — NOT archiving the id; investigate NOW, it is billing" ;;
  esac
fi

if [ -f "$STATE_DIR/instance_id" ] && [ "$(cat "$STATE_DIR/instance_id")" = "$INSTANCE" ]; then
  mv "$STATE_DIR/instance_id" "$STATE_DIR/destroyed_${INSTANCE}_$(date -u +%Y%m%dT%H%M%SZ)"
fi
{
  echo "destroyed=$INSTANCE workstream=$WORKSTREAM date=$(date -u +%FT%TZ)"
  if [ "$NEVER_PROVISIONED" = "1" ]; then
    echo "capture=NONE (--never-provisioned; the box never took our code)"
  else
    echo "capture_marker=$MARKER"
    cat "$MARKER"
  fi
  cost_status
} >> "$STATE_DIR/DESTROY_LOG.txt"
log "instance $INSTANCE destroyed and VERIFIED gone (all pages walked). Billing stopped."
log "record id + files swept/captured/lost(0) + this destroy in VERIFICATION_LOG.md"
log "CLOUD.md §9: every box carries /root/.vast_api_key — rotate the vast API key after this campaign"
