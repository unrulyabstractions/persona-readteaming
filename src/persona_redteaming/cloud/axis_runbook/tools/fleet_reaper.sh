#!/usr/bin/env bash
# fleet_reaper.sh — run-2 fleet endgame automation (temp/45-run2-exec.md).
# Polls every worker's stages monitor log; when ALL STAGES DONE appears for
# a worker, runs the standard gated chain 04_upload_final -> 05_capture ->
# 06_destroy for it. Safety properties are inherited, not reimplemented:
#   - 04 exits nonzero unless the xet gate verifies every staged file;
#   - 05 exits nonzero unless the byte/hash sweep verifies every file;
#   - 06 refuses without BOTH markers green + freshness vs sweep_epoch;
#   - assert_box_identity guards 04/05 against the shared-proxy incident.
# Captures are serialized (one at a time) to protect local bandwidth; a
# worker whose chain fails is flagged (.state/<w>_reap_failed) and left
# ALIVE for manual handling — its watchdog keeps alerting.
case $- in *x*) set +x ;; esac
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE" || exit 1
WORKERS="${WORKERS:-w1 w2 w3 w4 w5 w6 w7 w8}"

while true; do
  all_done=1
  for w in $WORKERS; do
    tag="r1d-qwen-14b-$w"
    [ -f ".state/${w}_reaped" ] && continue
    if [ -f ".state/${w}_reap_failed" ]; then all_done=0; continue; fi
    all_done=0
    log=".state/${w}_stages.log"
    [ -f "$log" ] || continue
    grep -q "ALL STAGES DONE" "$log" 2>/dev/null || continue
    echo "REAPER: $w stages done — running 04/05/06 ($(date -u +%H:%M:%S))"
    if ! AXIS_CONFIG="config.$tag.env" bash 04_upload_final.sh > ".state/${w}_04.log" 2>&1; then
      echo "REAPER: $w 04 FAILED — box left alive (see .state/${w}_04.log)"
      touch ".state/${w}_reap_failed"; continue
    fi
    if ! AXIS_CONFIG="config.$tag.env" bash 05_capture.sh > ".state/${w}_05.log" 2>&1; then
      echo "REAPER: $w 05 FAILED — box left alive (see .state/${w}_05.log)"
      touch ".state/${w}_reap_failed"; continue
    fi
    iid="$(cat ".state/$tag/instance_id" 2>/dev/null)"
    if ! AXIS_CONFIG="config.$tag.env" bash 06_destroy.sh \
         --marker ".state/$tag/CAPTURE_OK_$iid" --yes-i-am-really-sure \
         > ".state/${w}_06.log" 2>&1; then
      echo "REAPER: $w 06 REFUSED/FAILED — box may still bill (see .state/${w}_06.log)"
      touch ".state/${w}_reap_failed"; continue
    fi
    touch ".state/${w}_reaped"
    echo "REAPER: $w UPLOADED+CAPTURED+DESTROYED ($(date -u +%H:%M:%S))"
  done
  [ "$all_done" = "1" ] && { echo "REAPER: all workers reaped"; break; }
  sleep 60
done
