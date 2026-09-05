#!/usr/bin/env bash
# run_stages.sh — ON-BOX driver for the 5-stage assistant-axis pipeline.
#
# Stage flags per temp/40 §5.1 + temp/41 §1 (VERIFIED from pipeline code).
# S2 (GPU) and S3 (API-only) run CONCURRENTLY — serial ordering bills the
# GPU idle through the judge stage (bluedot incident). Every stage prints
# S<N>_RC=<rc> (greppable); there is deliberately NO `set -e` for stages,
# and NO false ALL_DONE: ALL_STAGES_RC=0 is printed ONLY when every rc is 0.
#
# Usage (normally launched detached by 03_stages.sh):
#   run_stages.sh                # full run into $OUT_DIR
#   SLICE=1 run_stages.sh        # canary: 1 role x 2 questions into $OUT_DIR-slice
case $- in *x*) set +x ;; esac
set -u
. /root/.axis_env
. /root/.axis_env_common

PY="$(command -v python3)"
export PYTHONPATH="$REMOTE_AA"
cd "$REMOTE_AA" || { echo "S0_RC=1 (no $REMOTE_AA)"; exit 1; }

OUT="$OUT_DIR"
S1_ARGS="$STAGE1_EXTRA"
S4_ARGS=""
if [ "${SLICE:-0}" = "1" ]; then
  OUT="$OUT_DIR-slice"
  # 'default' must be in the slice: 5_axis needs default vectors
  # (axis = mean(default) - mean(roles)); a role-only slice fails S5.
  # SLICE_ROLES override (fleet, temp/45): a multi-GPU box needs >= 2 roles
  # PER WORKER to exercise the pooled-S1 leg (roles split across workers).
  S1_ARGS="--roles ${SLICE_ROLES:-accountant default} --question_count 2"
  S4_ARGS="--min_count 1"   # slice has 10 rollouts/role; default min_count=50 must not fail it
  echo "SLICE MODE -> $OUT (roles: ${SLICE_ROLES:-accountant default})"
fi
mkdir -p "$OUT"

# Pidfile liveness contract (no pgrep — see pusher.sh note).
echo $$ > "$OUT/driver.pid"
trap 'rm -f "$OUT/driver.pid"; exit 1' TERM INT
trap 'rm -f "$OUT/driver.pid"' EXIT

echo "RUN_STAGES start $(date -u +%FT%TZ) pid=$$ model=$MODEL_ID out=$OUT"

# ---- stage 1: generation (vLLM, GPU) -------------------------------------
# --roles_dir/--questions_file explicitly: the argparse defaults are
# CWD-relative to pipeline/ (../data/...), and we run from the repo root.
# shellcheck disable=SC2086  # S1_ARGS word-splitting is deliberate
$PY pipeline/1_generate.py --model "$MODEL_ID" \
    --roles_dir data/roles/instructions \
    --questions_file data/extraction_questions.jsonl \
    --output_dir "$OUT/responses" --tensor_parallel_size 1 $S1_ARGS
rc=$?; echo "S1_RC=$rc"
if [ "$rc" != "0" ]; then echo "RUN_FAILED_S1"; exit 1; fi

# vLLM must release the GPU before stage 2 loads the HF model: poll VRAM
# instead of sleeping (a fixed sleep is not evidence).
for i in $(seq 1 24); do
  used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1)
  [ -n "$used" ] && [ "$used" -lt 2048 ] && { echo "GPU_FREED used=${used}MiB after ~$((i*5))s"; break; }
  sleep 5
done

# ---- stages 2 (GPU) + 3 (API) concurrently -------------------------------
(
  # --tensor_parallel_size 1: data-parallel S2 (one worker per GPU). The
  # default (None) makes ONE worker span ALL GPUs (observed on the w1
  # canary: "Single-worker mode: Using 4 GPU(s)") — model-parallel and slow.
  $PY pipeline/2_activations.py --model "$MODEL_ID" \
      --responses_dir "$OUT/responses" --output_dir "$OUT/activations" \
      --batch_size "$S2_BATCH" --tensor_parallel_size 1
  echo $? > "$OUT/.s2_rc"
) &
S2_PID=$!
(
  rc=1
  for pass in 1 2 3; do
    echo "S3 judge pass $pass ($(date -u +%H:%M:%S))"
    $PY pipeline/3_judge.py --responses_dir "$OUT/responses" \
        --roles_dir data/roles/instructions \
        --output_dir "$OUT/scores" --judge_model "$JUDGE_MODEL" \
        --requests_per_second "$JUDGE_RPS" --batch_size "$JUDGE_BATCH"
    rc=$?
    [ "$rc" = "0" ] && break
    sleep 30   # resume-safe merge: a re-run only fills missing keys
  done
  echo $rc > "$OUT/.s3_rc"
) &
S3_PID=$!
wait "$S2_PID"; wait "$S3_PID"
S2_RC="$(cat "$OUT/.s2_rc" 2>/dev/null || echo 99)"
S3_RC="$(cat "$OUT/.s3_rc" 2>/dev/null || echo 99)"
echo "S2_RC=$S2_RC"
echo "S3_RC=$S3_RC"
if [ "$S2_RC" != "0" ] || [ "$S3_RC" != "0" ]; then echo "RUN_FAILED_S2_OR_S3"; exit 1; fi

# ---- stage 4: aggregate + per-rollout export (CPU) -----------------------
# shellcheck disable=SC2086
$PY pipeline/4_vectors.py --activations_dir "$OUT/activations" \
    --scores_dir "$OUT/scores" --output_dir "$OUT/vectors" $S4_ARGS
rc=$?; echo "S4_RC=$rc"
if [ "$rc" != "0" ]; then echo "RUN_FAILED_S4"; exit 1; fi

# ---- stage 5: the axis (CPU) ---------------------------------------------
# Fleet mode (run-2, temp/45): each worker holds only a role SHARD, so the
# axis (mean(default) - mean(roles), needs ALL roles) is assembled centrally
# after coverage. SKIP_S5=1 skips it per worker; slice canaries still run it.
if [ "${SKIP_S5:-0}" = "1" ] && [ "${SLICE:-0}" != "1" ]; then
  echo "S5_SKIPPED (fleet worker: axis assembled centrally from bucket vectors)"
else
  $PY pipeline/5_axis.py --vectors_dir "$OUT/vectors" --output "$OUT/axis.pt"
  rc=$?; echo "S5_RC=$rc"
  if [ "$rc" != "0" ]; then echo "RUN_FAILED_S5"; exit 1; fi
fi

# ---- artifact presence gate (counts, not vibes) --------------------------
n_resp=$(ls "$OUT/responses"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
n_act=$(ls "$OUT/activations"/*.pt 2>/dev/null | wc -l | tr -d ' ')
n_scores=$(ls "$OUT/scores"/*.json 2>/dev/null | wc -l | tr -d ' ')
n_vec=$(ls "$OUT/vectors"/*.pt 2>/dev/null | wc -l | tr -d ' ')
n_roll=$(ls "$OUT/vectors/rollouts"/*.safetensors 2>/dev/null | wc -l | tr -d ' ')
echo "COUNTS responses=$n_resp activations_pt=$n_act scores=$n_scores vectors=$n_vec rollouts=$n_roll axis=$([ -f "$OUT/axis.pt" ] && echo 1 || echo 0)"

echo "ALL_STAGES_RC=0"
date -u +%FT%TZ > "$OUT/STAGES_DONE"
