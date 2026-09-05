#!/usr/bin/env bash
# s1_freeze.sh — hold every worker AT the S1 boundary (stop order, temp/45).
#
# run_stages.sh auto-continues into S2 within ~5 s of printing S1_RC=0. The
# stop order forbids S2-S5, and the reaper is deliberately SEQUENCED (the
# first 04b run is a live test, not an assumption), so workers waiting their
# turn must not drift into stage 2.
#
# This loop watches each worker's own stages.log and, the moment S1_RC=0
# appears, kills the stage driver's process GROUP — stopping S2 before it
# does meaningful work — while LEAVING THE PUSHER RUNNING, so the landed
# responses keep flowing to the bucket through the xet-verified gate. It
# never uploads, captures, or destroys: that is 04b/05/06's job.
case $- in *x*) set +x ;; esac
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE" || exit 1
WORKERS="${WORKERS:-w1 w2 w3 w4 w5 w6 w7 w8}"

while true; do
  pending=0
  for w in $WORKERS; do
    tag="r1d-qwen-14b-$w"
    [ -f ".state/${w}_frozen" ] && continue
    [ -f ".state/${w}_reaped" ] && continue
    [ -f ".state/$tag/instance_id" ] || continue
    pending=1
    out="$(AXIS_CONFIG="config.$tag.env" bash -c '
      . ./lib.sh >/dev/null 2>&1; require_instance >/dev/null 2>&1
      resolve_ssh_target >/dev/null 2>&1
      vssh "grep -c \"S1_RC=0\" $OUT_DIR/stages.log 2>/dev/null || echo 0" </dev/null 2>/dev/null' 2>/dev/null | tail -1)"
    case "$out" in ''|*[!0-9]*) continue ;; esac
    [ "$out" -ge 1 ] || continue

    n="$(AXIS_CONFIG="config.$tag.env" bash -c '
      . ./lib.sh >/dev/null 2>&1; require_instance >/dev/null 2>&1
      resolve_ssh_target >/dev/null 2>&1
      vssh "bash -s" <<'"'"'BOX'"'"'
set -u
. /root/.axis_env_common
touch "$OUT_DIR/EARLY_JUDGE_STOP" 2>/dev/null || true
for pf in driver early_judge; do
  p="$OUT_DIR/$pf.pid"; [ -f "$p" ] || continue
  pid="$(cat "$p" 2>/dev/null)"; [ -n "$pid" ] || continue
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d " ")"
  [ -n "$pgid" ] && kill -TERM "-$pgid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
done
sleep 3
for pid in $(ps -eo pid,args | grep -E "pipeline/[2-5]_" | grep -v grep | awk "{print \$1}"); do
  kill -KILL "$pid" 2>/dev/null || true
done
rm -f "$OUT_DIR/driver.pid" "$OUT_DIR/early_judge.pid"
echo "RESP=$(ls $OUT_DIR/responses/*.jsonl 2>/dev/null | wc -l | tr -d " ")"
BOX' 2>/dev/null | tail -1)"
    touch ".state/${w}_frozen"
    echo "FREEZE $w: S1 landed, driver stopped before S2 ($(date -u +%H:%M:%S)) $n"
  done
  [ "$pending" = "0" ] && { echo "FREEZE: all workers frozen or reaped"; break; }
  sleep 30
done
