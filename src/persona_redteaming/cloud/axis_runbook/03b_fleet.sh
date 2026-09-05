#!/usr/bin/env bash
# 03b_fleet.sh — run-2 FLEET variant of 03_stages.sh (temp/45-run2-exec.md).
#
# A separate file (NOT an edit of 03_stages.sh) because run-1's live local
# monitor is executing 03_stages.sh from this directory: editing a script
# under a running bash shifts its read offset (parse corruption risk).
# Deltas vs 03_stages.sh:
#   - resume pre-seed SKIPPED by default (SKIP_RESUME=1): the shared fleet
#     prefix holds OTHER workers' roles; pre-seeding would pollute this
#     worker's responses/ and make S2/S4 process foreign shards. Re-enable
#     only with a shard-filtered resume (not implemented; relaunch = re-gen).
#   - early_judge.sh started detached right after the stage driver (run-1
#     pattern, commit 819bced): per-box S3 overlap from S1 start at the
#     config's JUDGE_RPS (fleet aggregate 47 <= 50 measured-safe).
#   - monitor loop identical.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_instance
resolve_ssh_target
assert_box_identity
cost_guard_or_die
[ -f "$STATE_DIR/provisioned_$INSTANCE" ] || die "02_provision.sh has not succeeded for $INSTANCE"

HEARTBEAT="$STATE_DIR/heartbeat"

# ---- canary slice --------------------------------------------------------
if [ "${SLICE:-0}" = "1" ]; then
  log "SLICE canary through all 5 stages (foreground; pooled S1; roles: ${SLICE_ROLES:-accountant default})"
  vssh "SLICE=1 SLICE_ROLES='${SLICE_ROLES:-}' bash $REMOTE_ROOT/remote/run_stages.sh 2>&1 | tee $OUT_DIR-slice.log" </dev/null \
    | while IFS= read -r l; do log "slice: $l"; done
  if vssh "test -f $OUT_DIR-slice/STAGES_DONE" </dev/null 2>/dev/null; then
    log "SLICE OK — inspect the COUNTS line and $OUT_DIR-slice on the box, then re-run 03b without SLICE=1"
    exit 0
  fi
  die "SLICE FAILED — read $OUT_DIR-slice.log on the box; web-search the error string FIRST (CLOUD.md law 1), log in temp/45"
fi

# ---- public-bucket acknowledgment ----------------------------------------
[ "${ACK_PUBLIC_BUCKET:-0}" = "1" ] || die "the HF bucket is PUBLIC — re-run with ACK_PUBLIC_BUCKET=1 once acknowledged"

# ---- resume pre-seed: fleet default OFF (see header) ---------------------
if [ "${SKIP_RESUME:-1}" != "1" ]; then
  log "pre-seeding $OUT_DIR from $BUCKET/$BUCKET_PREFIX (resume)"
  vssh "export PATH=\$HOME/.local/bin:\$PATH && . /root/.axis_env && \
EXPECTED_ROLLOUTS=${EXPECTED_ROLLOUTS:-1200} \
uv run --no-project $REMOTE_ROOT/remote/resume_from_bucket.py $OUT_DIR '$BUCKET/$BUCKET_PREFIX'" </dev/null \
    | while IFS= read -r l; do log "resume: $l"; done
else
  log "resume pre-seed SKIPPED (fleet: shared prefix holds other workers' shards)"
fi

# ---- start the on-box pusher (detached; pidfile liveness) ----------------
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

# ---- start early_judge (detached; per-box S3 overlap from S1 start) ------
if [ "${EARLY_JUDGE:-1}" = "1" ]; then
  EJ_STATUS="$(vssh "bash -s" <<EOF
set -u
rm -f $OUT_DIR/EARLY_JUDGE_STOP
if [ -f $OUT_DIR/early_judge.pid ] && kill -0 \$(cat $OUT_DIR/early_judge.pid) 2>/dev/null; then
  echo EJ_ALREADY
else
  rm -f $OUT_DIR/early_judge.pid
  nohup setsid bash $REMOTE_ROOT/remote/early_judge.sh </dev/null >> $OUT_DIR/early_judge.log 2>&1 &
  for i in 1 2 3 4 5 6; do
    sleep 1
    if [ -f $OUT_DIR/early_judge.pid ] && kill -0 \$(cat $OUT_DIR/early_judge.pid) 2>/dev/null \
       && [ -f $OUT_DIR/early_judge.log ]; then echo EJ_LIVE; break; fi
  done
fi
EOF
)"
  case "$EJ_STATUS" in
    *EJ_LIVE*|*EJ_ALREADY*) log "early_judge live (rps=$JUDGE_RPS from config; control file $OUT_DIR/judge_rps)" ;;
    *) log "WARNING: early_judge did not start: $(redact "$EJ_STATUS") — driver's S3 still covers judging (slower)" ;;
  esac
fi

# ---- monitor loop --------------------------------------------------------
log "monitoring (Ctrl-C is safe: stages+pusher+early_judge run detached on the box)"
while true; do
  touch "$HEARTBEAT"
  STATUS="$(vssh "tail -40 $OUT_DIR/stages.log 2>/dev/null | grep -E 'S[0-9]_RC=|S5_SKIPPED|RUN_FAILED|ALL_STAGES_RC|COUNTS|SLICE|GPU_FREED' | tail -6; \
tail -5 $OUT_DIR/early_judge.log 2>/dev/null | grep -E 'EARLY_JUDGE pass|EARLY_JUDGE_PASS_RC' | tail -1; \
tail -3 $OUT_DIR/pusher.log 2>/dev/null | grep PUSH_CYCLE_RC | tail -1; \
df --output=avail -BG /workspace 2>/dev/null | tail -1" </dev/null 2>/dev/null || echo SSH_BLIP)"
  printf '%s\n' "$STATUS" | while IFS= read -r l; do [ -n "$l" ] && log "box: $l"; done
  cost_status
  if printf '%s' "$STATUS" | grep -q "ALL_STAGES_RC=0"; then
    log "ALL STAGES DONE (every S<N>_RC checked by the driver)"
    break
  fi
  if printf '%s' "$STATUS" | grep -q "RUN_FAILED"; then
    die "a stage FAILED (see markers above) — box stays up; ssh in, read $OUT_DIR/stages.log, \
web-search the error before iterating (CLOUD.md law 1), fix, re-run 03b (stages resume)"
  fi
  ALIVE="$(vssh "[ -f $OUT_DIR/driver.pid ] && kill -0 \$(cat $OUT_DIR/driver.pid) 2>/dev/null && echo RUNNING; \
test -f $OUT_DIR/STAGES_DONE && echo DONEMARK; true" </dev/null 2>/dev/null || echo SSH_BLIP)"
  if [ -z "$ALIVE" ]; then
    die "stage driver is NOT running and no STAGES_DONE exists — it died; \
read $OUT_DIR/stages.log on the box, then re-run 03b (stages resume)"
  fi
  sleep 180
done

touch "$HEARTBEAT"
cost_status
log "next: AXIS_CONFIG=$AXIS_CONFIG bash 04_upload_final.sh"
