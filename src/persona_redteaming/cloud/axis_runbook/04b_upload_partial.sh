#!/usr/bin/env bash
# 04b_upload_partial.sh — STOP-ORDER variant of 04_upload_final.sh
# (run-2 fleet, temp/45-run2-exec.md; orchestrator stop order 2026-09-04).
#
# 04 refuses without $OUT_DIR/STAGES_DONE, which by design never appears when
# a run is deliberately stopped after stage 1. This variant makes the SAME
# proof for a partial run:
#   1. stop every writer (stage driver process GROUP, early_judge, pusher) so
#      the tree is quiescent — a running writer breaks both gates;
#   2. stage everything under $OUT_DIR except the hardlink mirror itself,
#      with worker-scoped files (logs, fingerprint, markers) under
#      meta/$MODEL_TAG/ so 8 workers sharing one prefix cannot overwrite;
#      activations/*.pt are EXCLUDED from the bucket only when their S4
#      safetensors twin exists (temp/41 §3.2) — in a stop-after-S1 run there
#      are no .pt files at all, so nothing is skipped;
#   3. run hf_upload_verified.py STRICTLY (sync + independent re-list +
#      per-file byte size AND local-xet-hash vs server xet_hash). rc=0 only.
#   4. write $STATE_DIR/UPLOAD_OK_<instance> carrying the same
#      `gate=FINAL_GATE_RC=0` line 06_destroy.sh requires, plus partial=true
#      and the artifact counts, so the destroy gate stays honest.
#
# Usage: AXIS_CONFIG=config.r1d-qwen-14b-wN.env bash 04b_upload_partial.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_instance
resolve_ssh_target
assert_box_identity

log "PARTIAL upload (stop order) for $MODEL_TAG -> $BUCKET/$BUCKET_PREFIX"

# ---- 1. stop all writers -------------------------------------------------
# Kill the driver's whole process GROUP: run_stages.sh is started under
# setsid, so its children (1_generate/2_activations/3_judge) share its pgid.
# Killing only the pidfile pid would orphan a running stage that keeps
# writing into the tree we are about to freeze.
vssh "bash -s" <<'EOF' || true
set -u
. /root/.axis_env_common
touch "$OUT_DIR/EARLY_JUDGE_STOP" "$OUT_DIR/PUSHER_STOP" 2>/dev/null || true
for pf in driver early_judge; do
  p="$OUT_DIR/$pf.pid"
  [ -f "$p" ] || continue
  pid="$(cat "$p" 2>/dev/null)"
  [ -n "$pid" ] || continue
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
  if [ -n "$pgid" ]; then kill -TERM "-$pgid" 2>/dev/null || true; fi
  kill -TERM "$pid" 2>/dev/null || true
done
sleep 5
for pf in driver early_judge; do
  p="$OUT_DIR/$pf.pid"
  [ -f "$p" ] || continue
  pid="$(cat "$p" 2>/dev/null)"
  [ -n "$pid" ] || continue
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
  if [ -n "$pgid" ]; then kill -KILL "-$pgid" 2>/dev/null || true; fi
  kill -KILL "$pid" 2>/dev/null || true
done
# Any surviving pipeline python (a stage that lost its parent) must also go.
for pid in $(ps -eo pid,args | grep -E 'pipeline/[1-5]_' | grep -v grep | awk '{print $1}'); do
  kill -KILL "$pid" 2>/dev/null || true
done
rm -f "$OUT_DIR/driver.pid" "$OUT_DIR/early_judge.pid"
echo "WRITERS_STOPPED"
EOF

# pusher exits on its own marker; wait for it, then prove it is gone.
for i in $(seq 1 70); do
  if ! vssh "[ -f $OUT_DIR/pusher.pid ] && kill -0 \$(cat $OUT_DIR/pusher.pid) 2>/dev/null" </dev/null 2>/dev/null; then
    log "pusher exited after ~$((i*5))s"; break
  fi
  sleep 5
done
vssh "[ -f $OUT_DIR/pusher.pid ] && kill -0 \$(cat $OUT_DIR/pusher.pid) 2>/dev/null" </dev/null 2>/dev/null \
  && die "pusher still running after 350s — investigate before the partial upload"
REMAIN="$(vssh "ps -eo args | grep -E 'pipeline/[1-5]_|run_stages.sh|early_judge.sh|pusher.sh' | grep -v grep | wc -l" </dev/null 2>/dev/null || echo 99)"
[ "$REMAIN" = "0" ] || die "writers still alive on the box (n=$REMAIN) — refusing to stage a moving tree"
log "all writers stopped and verified (no pipeline/pusher/judge processes remain)"

# ---- 2+3. staging + strict gate ------------------------------------------
GATE_OUT="$(vssh "bash -s" <<EOF
set -u
. /root/.axis_env
. /root/.axis_env_common
export PATH="\$HOME/.local/bin:\$PATH"
rsync -a --link-dest="$OUT_DIR/" \
      --exclude='.s2_rc' --exclude='.s3_rc' --exclude='PUSHER_STOP' \
      --exclude='EARLY_JUDGE_STOP' --exclude='*.pid' \
      --exclude='env_fingerprint.json' --exclude='STAGES_DONE' \
      --exclude='judge_rps' --exclude='stages.log' --exclude='pusher.log' \
      --exclude='early_judge.log' \
      "$OUT_DIR/" "$UPLOAD_DIR/"
mkdir -p "$UPLOAD_DIR/meta/\$MODEL_TAG"
for f in env_fingerprint.json STAGES_DONE judge_rps stages.log pusher.log early_judge.log; do
  cp -f "$OUT_DIR/\$f" "$UPLOAD_DIR/meta/\$MODEL_TAG/" 2>/dev/null || true
done
uv run --no-project "$REMOTE_ROOT/hf_upload_verified.py" "$UPLOAD_DIR" "\$BUCKET_DEST" --allow-extra
rc=\$?
echo "FINAL_GATE_RC=\$rc"
exit \$rc
EOF
)" || true
printf '%s\n' "$GATE_OUT" | tail -12 | while IFS= read -r l; do log "gate: $l"; done
printf '%s' "$GATE_OUT" | grep -q "FINAL_GATE_RC=0" \
  || die "PARTIAL UPLOAD GATE FAILED — nothing is proven on the bucket; fix and re-run 04b"

# ---- 4. counts + marker --------------------------------------------------
COUNTS="$(vssh "printf 'responses=%s scores=%s vectors=%s rollouts=%s manifests=%s tokens=%s\n' \
\"\$(ls $UPLOAD_DIR/responses/*.jsonl 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$(ls $UPLOAD_DIR/scores/*.json 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$(ls $UPLOAD_DIR/vectors/*.pt 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$(ls $UPLOAD_DIR/vectors/rollouts/*.safetensors 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$(ls $UPLOAD_DIR/vectors/rollouts/*.manifest.json 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$(ls $UPLOAD_DIR/activations/*.tokens.json 2>/dev/null | wc -l | tr -d ' ')\"" </dev/null)"
log "counts: $COUNTS"
{
  echo "instance=$INSTANCE"
  echo "date=$(date -u +%FT%TZ)"
  echo "dest=$BUCKET/$BUCKET_PREFIX"
  echo "gate=FINAL_GATE_RC=0 (sync + size + xet content hash, every file)"
  echo "partial=true (stop order: run halted after stage 1; S2-S5 not run)"
  echo "counts: $COUNTS"
} > "$STATE_DIR/UPLOAD_OK_$INSTANCE"
log "PARTIAL UPLOAD VERIFIED — marker $STATE_DIR/UPLOAD_OK_$INSTANCE"
cost_status
log "next: AXIS_CONFIG=$AXIS_CONFIG bash 05_capture.sh"
