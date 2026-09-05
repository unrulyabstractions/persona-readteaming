#!/usr/bin/env bash
# pusher.sh — ON-BOX continuous uploader (bluedot's 5-min partial pusher,
# but through the xet-verified gate; temp/42).
#
# Every $PUSH_INTERVAL_SEC:
#   1. refresh $UPLOAD_DIR = hardlink mirror of $OUT_DIR MINUS activations/*.pt
#      (the *.pt store byte-duplicates vectors/rollouts/*.safetensors —
#      torch.equal-proven in temp/41 §3.2; the .pt files still reach LOCAL
#      capture before destroy).
#   2. run hf_upload_verified.py (sync + independent re-list + local xet-hash
#      vs server xet_hash per file). Exit 2 mid-run is EXPECTED while stages
#      are still writing (a file can change between sync and verify) — it is
#      logged and retried next cycle; only 04_upload_final's quiescent run
#      counts as proof.
#
# Stops when $OUT_DIR/PUSHER_STOP exists. Never passes --delete (buckets are
# unversioned; deletes are final).
case $- in *x*) set +x ;; esac
set -u
. /root/.axis_env
. /root/.axis_env_common
export PATH="$HOME/.local/bin:$PATH"

# Pidfile is the ONLY liveness contract (pgrep -f self-matches any probing
# shell whose argv mentions this script's path — measured incident 2026-09-04).
echo $$ > "$OUT_DIR/pusher.pid"
trap 'rm -f "$OUT_DIR/pusher.pid"; exit 0' TERM INT
trap 'rm -f "$OUT_DIR/pusher.pid"' EXIT

echo "PUSHER start $(date -u +%FT%TZ) pid=$$ dest=$BUCKET_DEST interval=${PUSH_INTERVAL_SEC}s"
while true; do
  if [ -f "$OUT_DIR/PUSHER_STOP" ]; then
    echo "PUSHER stop marker seen $(date -u +%FT%TZ)"
    break
  fi
  # Live logs and pidfiles are excluded mid-run: staging a file that this
  # very loop is appending to makes every cycle fail verification (rc=2
  # forever, masking real drift). 04's quiescent staging picks the logs up.
  # Fleet (run-2): 8 workers share one bucket prefix; worker-scoped files
  # (fingerprint, done marker, judge_rps control) must never overwrite each
  # other at the prefix root -> excluded here, staged under meta/$MODEL_TAG/.
  rsync -a --link-dest="$OUT_DIR/" --exclude='activations/*.pt' \
        --exclude='.s2_rc' --exclude='.s3_rc' --exclude='PUSHER_STOP' \
        --exclude='pusher.log' --exclude='stages.log' --exclude='*.pid' \
        --exclude='env_fingerprint.json' --exclude='STAGES_DONE' \
        --exclude='judge_rps' \
        "$OUT_DIR/" "$UPLOAD_DIR/" 2>&1 | tail -1
  mkdir -p "$UPLOAD_DIR/meta/$MODEL_TAG"
  cp -f "$OUT_DIR/env_fingerprint.json" "$UPLOAD_DIR/meta/$MODEL_TAG/" 2>/dev/null || true
  uv run --no-project "$REMOTE_ROOT/hf_upload_verified.py" \
      "$UPLOAD_DIR" "$BUCKET_DEST" --allow-extra
  rc=$?
  echo "PUSH_CYCLE_RC=$rc $(date -u +%FT%TZ)"   # rc=2 mid-run is normal (files in flight)
  sleep "$PUSH_INTERVAL_SEC"
done
echo "PUSHER_EXITED"
