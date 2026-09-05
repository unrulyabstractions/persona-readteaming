#!/usr/bin/env bash
# early_judge.sh — ON-BOX early S3 overlap (coordinator directive 2026-09-04).
#
# Judges already-generated roles WHILE stage 1 is still running: the judge is
# API-only (no GPU) and 3_judge.py's per-role merge is resume-safe, so
# repeated passes over a growing responses/ dir converge, and the stage
# driver's own S3 (which starts after S1_RC=0) finds most roles scored and
# only fills gaps. Saves up to ~4h wall vs waiting for S1.
#
# Exits when: S1_RC=0 appears in stages.log (driver's S3 takes over — keeps
# the two-judges race window to at most one in-flight pass; lost concurrent
# merges are healed by the driver's 3-pass loop), or EARLY_JUDGE_STOP exists.
#
# RPS is re-read each pass from $OUT_DIR/judge_rps (control file) so the
# raise-to-50-after-30-clean-minutes rule needs no restart. Pidfile liveness
# contract as everywhere (never pgrep).
case $- in *x*) set +x ;; esac
set -u
. /root/.axis_env
. /root/.axis_env_common
PY="$(command -v python3)"
export PYTHONPATH="$REMOTE_AA"
cd "$REMOTE_AA" || exit 1

echo $$ > "$OUT_DIR/early_judge.pid"
trap 'rm -f "$OUT_DIR/early_judge.pid"; exit 0' TERM INT
trap 'rm -f "$OUT_DIR/early_judge.pid"' EXIT
echo "EARLY_JUDGE start $(date -u +%FT%TZ) pid=$$"

while true; do
  if grep -q 'S1_RC=0' "$OUT_DIR/stages.log" 2>/dev/null; then
    echo "EARLY_JUDGE stop: S1 done, driver S3 takes over $(date -u +%FT%TZ)"
    break
  fi
  if [ -f "$OUT_DIR/EARLY_JUDGE_STOP" ]; then
    echo "EARLY_JUDGE stop marker seen"
    break
  fi
  RPS="$(cat "$OUT_DIR/judge_rps" 2>/dev/null || echo "$JUDGE_RPS")"
  n_resp="$(ls "$OUT_DIR/responses"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')"
  n_scored="$(ls "$OUT_DIR/scores"/*.json 2>/dev/null | wc -l | tr -d ' ')"
  echo "EARLY_JUDGE pass $(date -u +%H:%M:%S) rps=$RPS responses=$n_resp scored=$n_scored"
  "$PY" pipeline/3_judge.py --responses_dir "$OUT_DIR/responses" \
      --roles_dir data/roles/instructions \
      --output_dir "$OUT_DIR/scores" --judge_model "$JUDGE_MODEL" \
      --requests_per_second "$RPS" --batch_size "$JUDGE_BATCH"
  echo "EARLY_JUDGE_PASS_RC=$? $(date -u +%H:%M:%S)"
  sleep 60
done
