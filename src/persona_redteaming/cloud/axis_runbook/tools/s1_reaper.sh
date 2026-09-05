#!/usr/bin/env bash
# s1_reaper.sh — STOP-ORDER endgame (orchestrator, 2026-09-04; temp/45).
#
# Priority order enforced here: (1) lose no data, (2) stop the R1 work,
# (3) save money. Per worker, independently and as soon as ITS OWN data is
# safe:
#   wait for S1_RC=0  ->  stop writers  ->  04b xet-verified upload
#   ->  05 byte+sha256 capture  ->  06 destroy (refuses without both gates)
#
# A box is NEVER destroyed before its responses are proven in the bucket by
# a fresh remote listing (04b's gate) AND proven on local disk (05's sweep).
# Any failure leaves that box ALIVE and flagged; the other workers continue.
#
# S2-S5 are deliberately not run: they are recomputable from the S1
# responses on fresh hardware, so stopping there costs no data. The driver
# would otherwise auto-continue into S2, hence the kill in 04b.
case $- in *x*) set +x ;; esac
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE" || exit 1
WORKERS="${WORKERS:-w1 w2 w3 w4 w5 w6 w7 w8}"

reap_one() {
  local w="$1" tag="r1d-qwen-14b-$1" iid
  iid="$(cat ".state/$tag/instance_id" 2>/dev/null)"
  [ -n "$iid" ] || { echo "REAP $w: no instance id"; return 1; }

  echo "REAP $w: S1 done — stopping writers + partial upload ($(date -u +%H:%M:%S))"
  if ! AXIS_CONFIG="config.$tag.env" bash 04b_upload_partial.sh > ".state/${w}_04b.log" 2>&1; then
    echo "REAP $w: 04b UPLOAD FAILED — box ALIVE, not destroying (.state/${w}_04b.log)"
    touch ".state/${w}_reap_failed"; return 1
  fi
  echo "REAP $w: upload verified — capturing ($(date -u +%H:%M:%S))"
  if ! AXIS_CONFIG="config.$tag.env" bash 05_capture.sh > ".state/${w}_05.log" 2>&1; then
    echo "REAP $w: 05 CAPTURE FAILED — box ALIVE, not destroying (.state/${w}_05.log)"
    touch ".state/${w}_reap_failed"; return 1
  fi
  echo "REAP $w: capture verified — destroying ($(date -u +%H:%M:%S))"
  if ! AXIS_CONFIG="config.$tag.env" bash 06_destroy.sh \
       --marker ".state/$tag/CAPTURE_OK_$iid" --yes-i-am-really-sure \
       > ".state/${w}_06.log" 2>&1; then
    echo "REAP $w: 06 DESTROY refused/failed — check .state/${w}_06.log"
    touch ".state/${w}_reap_failed"; return 1
  fi
  touch ".state/${w}_reaped"
  echo "REAP $w: DONE uploaded+captured+destroyed ($(date -u +%H:%M:%S))"
  return 0
}

while true; do
  pending=0
  for w in $WORKERS; do
    [ -f ".state/${w}_reaped" ] && continue
    [ -f ".state/${w}_reap_failed" ] && continue
    tag="r1d-qwen-14b-$w"
    iid="$(cat ".state/$tag/instance_id" 2>/dev/null)"
    [ -n "$iid" ] || continue
    pending=1
    # S1 completion is read from the BOX's own stages.log, not from the
    # local monitor's tail (which can lag or miss lines).
    host_port="$(AXIS_CONFIG="config.$tag.env" bash -c '
      . ./lib.sh >/dev/null 2>&1; require_instance >/dev/null 2>&1
      resolve_ssh_target >/dev/null 2>&1; printf "%s %s" "$SSH_HOST" "$SSH_PORT"' 2>/dev/null)"
    [ -n "$host_port" ] || continue
    s1="$(AXIS_CONFIG="config.$tag.env" bash -c '
      . ./lib.sh >/dev/null 2>&1; require_instance >/dev/null 2>&1
      resolve_ssh_target >/dev/null 2>&1
      vssh "grep -c \"S1_RC=0\" $OUT_DIR/stages.log 2>/dev/null || echo 0" </dev/null 2>/dev/null' 2>/dev/null | tail -1)"
    case "$s1" in ''|*[!0-9]*) continue ;; esac
    [ "$s1" -ge 1 ] || continue
    reap_one "$w" &
  done
  wait
  [ "$pending" = "0" ] && { echo "REAPER: nothing pending — exiting"; break; }
  sleep 45
done
