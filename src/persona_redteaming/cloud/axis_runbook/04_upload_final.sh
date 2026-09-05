#!/usr/bin/env bash
# 04_upload_final.sh — quiescent, gate-verified FINAL upload to the bucket.
#
# 1. Stops the pusher (marker + wait), so nothing writes during the final sync.
# 2. Refreshes the upload staging tree (hardlinks; activations/*.pt excluded —
#    byte-duplicates of vectors/rollouts/*.safetensors, temp/41 §3.2; they
#    still reach local capture in 05).
# 3. Runs hf_upload_verified.py: sync + independent remote re-list + per-file
#    size AND local-xet-hash-vs-server-xet_hash compare. Exit 0 is the ONLY
#    acceptable outcome here (rc=0 from a plain uploader proves nothing —
#    four documented false-success modes, temp/40 §3.2).
# 4. Verifies expected artifact COUNTS in the staging tree.
# 5. Writes $STATE_DIR/UPLOAD_OK_<instance> — 06_destroy.sh requires it.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_instance
resolve_ssh_target
assert_box_identity

log "final upload for $MODEL_TAG -> $BUCKET/$BUCKET_PREFIX"

# ---- 1. stop the pusher and wait for it to exit --------------------------
# Pidfile liveness (never pgrep -f: self-matches the probing shell's argv —
# measured 2026-09-04). The pusher removes its pidfile on exit.
vssh "touch $OUT_DIR/PUSHER_STOP" </dev/null
for i in $(seq 1 70); do
  if ! vssh "[ -f $OUT_DIR/pusher.pid ] && kill -0 \$(cat $OUT_DIR/pusher.pid) 2>/dev/null" </dev/null 2>/dev/null; then
    log "pusher exited after ~$((i*5))s"; break
  fi
  sleep 5
done
vssh "[ -f $OUT_DIR/pusher.pid ] && kill -0 \$(cat $OUT_DIR/pusher.pid) 2>/dev/null" </dev/null 2>/dev/null \
  && die "pusher still running after 350s (one full push cycle) — investigate before final upload"

# ---- 2+3. quiescent staging refresh + strict gate run --------------------
GATE_OUT="$(vssh "bash -s" <<EOF
set -u
. /root/.axis_env
. /root/.axis_env_common
export PATH="\$HOME/.local/bin:\$PATH"
[ -f "$OUT_DIR/STAGES_DONE" ] || { echo "GATE_ABORT: no STAGES_DONE"; exit 3; }
if [ "${SKIP_S5:-0}" = "1" ]; then
  # Fleet worker (run-2): worker-scoped files go under meta/\$MODEL_TAG/ so 8
  # workers sharing one prefix never overwrite each other at the root.
  rsync -a --link-dest="$OUT_DIR/" --exclude='activations/*.pt' \
        --exclude='.s2_rc' --exclude='.s3_rc' --exclude='PUSHER_STOP' \
        --exclude='env_fingerprint.json' --exclude='STAGES_DONE' \
        --exclude='judge_rps' --exclude='stages.log' --exclude='pusher.log' \
        --exclude='*.pid' \
        "$OUT_DIR/" "$UPLOAD_DIR/"
  mkdir -p "$UPLOAD_DIR/meta/\$MODEL_TAG"
  for f in env_fingerprint.json STAGES_DONE judge_rps stages.log pusher.log; do
    cp -f "$OUT_DIR/\$f" "$UPLOAD_DIR/meta/\$MODEL_TAG/" 2>/dev/null || true
  done
else
  rsync -a --link-dest="$OUT_DIR/" --exclude='activations/*.pt' \
        --exclude='.s2_rc' --exclude='.s3_rc' --exclude='PUSHER_STOP' \
        "$OUT_DIR/" "$UPLOAD_DIR/"
fi
uv run --no-project "$REMOTE_ROOT/hf_upload_verified.py" "$UPLOAD_DIR" "\$BUCKET_DEST" --allow-extra
rc=\$?
echo "FINAL_GATE_RC=\$rc"
exit \$rc
EOF
)" || true
printf '%s\n' "$GATE_OUT" | tail -15 | while IFS= read -r l; do log "gate: $l"; done
printf '%s' "$GATE_OUT" | grep -q "FINAL_GATE_RC=0" \
  || die "FINAL UPLOAD GATE FAILED — nothing is proven on the bucket; fix and re-run 04"

# ---- 4. artifact-count completion gate (counted on the staging tree) -----
COUNTS="$(vssh "printf 'responses=%s scores=%s vectors=%s rollouts=%s manifests=%s axis=%s\n' \
\"\$(ls $UPLOAD_DIR/responses/*.jsonl 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$(ls $UPLOAD_DIR/scores/*.json 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$(ls $UPLOAD_DIR/vectors/*.pt 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$(ls $UPLOAD_DIR/vectors/rollouts/*.safetensors 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$(ls $UPLOAD_DIR/vectors/rollouts/*.manifest.json 2>/dev/null | wc -l | tr -d ' ')\" \
\"\$([ -f $UPLOAD_DIR/axis.pt ] && echo 1 || echo 0)\"" </dev/null)"
log "counts: $COUNTS"
# Full-run expectations (temp/41): 276 responses, 275 scores (default is
# unjudged), 276 rollout safetensors + manifests, axis=1. vectors/*.pt may be
# < 276 if roles failed min_count — that is a FINDING to record, not a pass.
# Fleet workers (SKIP_S5=1, run-2) hold a shard and no axis: gate on rollouts
# instead; the axis is assembled + gated centrally (temp/45).
if [ "${SKIP_S5:-0}" = "1" ]; then
  if printf '%s' "$COUNTS" | grep -q "rollouts=0 "; then
    die "no rollout safetensors in upload staging (fleet worker shard empty?)"
  fi
else
  printf '%s' "$COUNTS" | grep -q "axis=1" || die "axis.pt missing from upload staging"
fi
{
  echo "instance=$INSTANCE"
  echo "date=$(date -u +%FT%TZ)"
  echo "dest=$BUCKET/$BUCKET_PREFIX"
  echo "gate=FINAL_GATE_RC=0 (sync + size + xet content hash, every file)"
  echo "counts: $COUNTS"
} > "$STATE_DIR/UPLOAD_OK_$INSTANCE"
log "UPLOAD VERIFIED — marker $STATE_DIR/UPLOAD_OK_$INSTANCE"
cost_status
log "next: AXIS_CONFIG=$AXIS_CONFIG bash 05_capture.sh"
