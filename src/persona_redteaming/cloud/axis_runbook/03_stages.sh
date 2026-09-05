#!/usr/bin/env bash
# 03_stages.sh — run the pipeline: resume pre-seed, start the on-box pusher,
# launch the detached stage driver, monitor until done.
#
# Usage:
#   AXIS_CONFIG=config.qwen3-1.7b.env SLICE=1 bash 03_stages.sh   # canary FIRST
#   AXIS_CONFIG=config.qwen3-1.7b.env ACK_PUBLIC_BUCKET=1 bash 03_stages.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_instance
resolve_ssh_target
cost_guard_or_die
[ -f "$STATE_DIR/provisioned_$INSTANCE" ] || die "02_provision.sh has not succeeded for $INSTANCE"

HEARTBEAT="$STATE_DIR/heartbeat"

# ---- canary slice (temp/41 §6: vLLM stage 1 has never run anywhere yet) --
if [ "${SLICE:-0}" = "1" ]; then
  log "SLICE canary: 1 role x 2 questions through all 5 stages (foreground)"
  vssh "SLICE=1 bash $REMOTE_ROOT/remote/run_stages.sh 2>&1 | tee $OUT_DIR-slice.log" </dev/null \
    | while IFS= read -r l; do log "slice: $l"; done
  if vssh "test -f $OUT_DIR-slice/STAGES_DONE" </dev/null 2>/dev/null; then
    log "SLICE OK — inspect the COUNTS line and $OUT_DIR-slice on the box, then re-run 03 without SLICE=1"
    exit 0
  fi
  die "SLICE FAILED — read $OUT_DIR-slice.log on the box; per the web-search-first rule, \
search any error string + official docs BEFORE local iteration, and log the finding in temp/44"
fi

# ---- public-bucket acknowledgment (temp/42: bucket is world-readable) ----
[ "${ACK_PUBLIC_BUCKET:-0}" = "1" ] || die "the HF bucket is PUBLIC and the quota decision \
(README §3.3) must be made first — re-run with ACK_PUBLIC_BUCKET=1 once decided"

# ---- resume-from-bucket pre-seed (idempotent relaunches) -----------------
# EXPECTED_ROLLOUTS gates partial responses files (default 1200 = 5 x 240;
# override to 5 x question_count when STAGE1_EXTRA changes it).
log "pre-seeding $OUT_DIR from $BUCKET/$BUCKET_PREFIX (resume)"
vssh "export PATH=\$HOME/.local/bin:\$PATH && . /root/.axis_env && \
EXPECTED_ROLLOUTS=${EXPECTED_ROLLOUTS:-1200} \
uv run --no-project $REMOTE_ROOT/remote/resume_from_bucket.py $OUT_DIR '$BUCKET/$BUCKET_PREFIX'" </dev/null \
  | while IFS= read -r l; do log "resume: $l"; done

# ---- start the on-box pusher (detached; </dev/null is load-bearing) ------
# Liveness contract = pidfile written by the script itself + kill -0 + log
# file present. NEVER pgrep -f: any probing shell whose argv mentions the
# script path self-matches (two measured false-LIVE/false-RUNNING incidents
# on 2026-09-04, ~45 min idle GPU).
PUSHER_STATUS="$(vssh "bash -s" <<EOF
set -u
rm -f $OUT_DIR/PUSHER_STOP
if [ -f $OUT_DIR/pusher.pid ] && kill -0 \$(cat $OUT_DIR/pusher.pid) 2>/dev/null; then
  echo PUSHER_ALREADY
else
  rm -f $OUT_DIR/pusher.pid
  nohup setsid bash $REMOTE_ROOT/remote/pusher.sh </dev/null >> $OUT_DIR/pusher.log 2>&1 &
  for i in 1 2 3 4 5 6; do
    sleep 1
    if [ -f $OUT_DIR/pusher.pid ] && kill -0 \$(cat $OUT_DIR/pusher.pid) 2>/dev/null \
       && [ -f $OUT_DIR/pusher.log ]; then echo PUSHER_LIVE; break; fi
  done
fi
EOF
)"
case "$PUSHER_STATUS" in
  *PUSHER_LIVE*|*PUSHER_ALREADY*) log "on-box pusher live (5-min verified pushes to $BUCKET/$BUCKET_PREFIX)" ;;
  *) die "pusher did not start (pidfile liveness): $(redact "$PUSHER_STATUS") — read $OUT_DIR/pusher.log on the box" ;;
esac

# ---- launch the stage driver (detached) ----------------------------------
if vssh "test -f $OUT_DIR/STAGES_DONE" </dev/null 2>/dev/null; then
  log "STAGES_DONE already present — skipping to monitoring/inspection"
else
  DRIVER_STATUS="$(vssh "bash -s" <<EOF
set -u
if [ -f $OUT_DIR/driver.pid ] && kill -0 \$(cat $OUT_DIR/driver.pid) 2>/dev/null; then
  echo STAGES_ALREADY
else
  rm -f $OUT_DIR/driver.pid
  nohup setsid bash $REMOTE_ROOT/remote/run_stages.sh </dev/null >> $OUT_DIR/stages.log 2>&1 &
  for i in 1 2 3 4 5 6; do
    sleep 1
    if [ -f $OUT_DIR/driver.pid ] && kill -0 \$(cat $OUT_DIR/driver.pid) 2>/dev/null \
       && [ -f $OUT_DIR/stages.log ]; then echo STAGES_LIVE; break; fi
  done
fi
EOF
)"
  case "$DRIVER_STATUS" in
    *STAGES_LIVE*|*STAGES_ALREADY*) log "stage driver live -> $OUT_DIR/stages.log" ;;
    *) die "stage driver did not start (pidfile liveness): $(redact "$DRIVER_STATUS")" ;;
  esac
fi

# ---- monitor loop --------------------------------------------------------
# Progress verbs: stage 2 logs role processing, stage 3 logs scoring; grep
# them separately (they interleave). Heartbeat feeds the watchdog's grace.
log "monitoring (Ctrl-C is safe: stages+pusher run detached on the box)"
while true; do
  touch "$HEARTBEAT"
  STATUS="$(vssh "tail -40 $OUT_DIR/stages.log 2>/dev/null | grep -E 'S[0-9]_RC=|RUN_FAILED|ALL_STAGES_RC|COUNTS|SLICE|GPU_FREED' | tail -6; \
tail -3 $OUT_DIR/pusher.log 2>/dev/null | grep PUSH_CYCLE_RC | tail -1; \
df --output=avail -BG /workspace 2>/dev/null | tail -1" </dev/null 2>/dev/null || echo SSH_BLIP)"
  printf '%s\n' "$STATUS" | while IFS= read -r l; do [ -n "$l" ] && log "box: $l"; done
  cost_status
  if printf '%s' "$STATUS" | grep -q "ALL_STAGES_RC=0"; then
    log "ALL STAGES DONE (every S<N>_RC checked by the driver, not ALL_DONE vibes)"
    break
  fi
  if printf '%s' "$STATUS" | grep -q "RUN_FAILED"; then
    die "a stage FAILED (see markers above) — box stays up; ssh in, read $OUT_DIR/stages.log, \
web-search the error before iterating (CLOUD.md law 1), fix, re-run 03 (stages resume)"
  fi
  # Driver-death detection: a dead driver with no DONE marker must not leave
  # this loop spinning silently while the GPU bills idle (the 2026-09-04
  # pgrep-self-match incident looped exactly this way). '; true' keeps the
  # remote rc at 0 so SSH_BLIP fires only on real connection failure.
  ALIVE="$(vssh "[ -f $OUT_DIR/driver.pid ] && kill -0 \$(cat $OUT_DIR/driver.pid) 2>/dev/null && echo RUNNING; \
test -f $OUT_DIR/STAGES_DONE && echo DONEMARK; true" </dev/null 2>/dev/null || echo SSH_BLIP)"
  if [ -z "$ALIVE" ]; then
    die "stage driver is NOT running and no STAGES_DONE exists — it died; \
read $OUT_DIR/stages.log on the box, then re-run 03 (stages resume)"
  fi
  sleep 180
done

touch "$HEARTBEAT"
cost_status
log "next: AXIS_CONFIG=$AXIS_CONFIG bash 04_upload_final.sh"
