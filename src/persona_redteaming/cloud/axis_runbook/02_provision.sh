#!/usr/bin/env bash
# 02_provision.sh — ship code + secrets, assert the prebuilt stack, download
# the model, preflight the Gemini judge FROM the box.
#
# CLOUD.md law 2: the image (vllm/vllm-openai:v0.13.0) already carries
# torch+vllm. Provisioning ASSERTS their versions and installs ONLY pinned
# pure-python extras (--no-deps). If anything here starts resolving
# torch/vllm, STOP: wrong image.
#
# Idempotent: every remote step checks its own done-condition first.
# Secrets: HF_TOKEN + GEMINI_API_KEY travel stdin -> 0600 file on the box.
# They never appear in argv, in this log, or in remote logs.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_instance
resolve_ssh_target
cost_guard_or_die
require_resolved IMAGE "$IMAGE"

log "provisioning instance $INSTANCE at $SSH_HOST:$SSH_PORT for $MODEL_ID"
assert_box_identity

# ---- 0. local precondition: the modified submodule is what we think ------
[ -d "$LOCAL_AA" ] || die "assistant-axis checkout not found at $LOCAL_AA"
AA_BRANCH="$(git -C "$LOCAL_AA" rev-parse --abbrev-ref HEAD)"
[ "$AA_BRANCH" = "persona-redteaming" ] \
  || die "submodule is on '$AA_BRANCH', expected persona-redteaming (temp/41 mods)"
git -C "$LOCAL_AA" merge-base --is-ancestor "$AA_REQUIRED_COMMIT" HEAD \
  || die "submodule HEAD lacks required commit $AA_REQUIRED_COMMIT (judge swap + per-rollout + R1 fix)"
AA_HEAD="$(git -C "$LOCAL_AA" rev-parse --short HEAD)"
if [ -n "$(git -C "$LOCAL_AA" status --porcelain)" ]; then
  log "WARNING: submodule worktree has uncommitted changes on top of $AA_HEAD — shipping them as-is"
fi
log "shipping assistant-axis @ $AA_BRANCH ($AA_HEAD)"

# ---- 1. secrets via stdin (0600), names only in logs ---------------------
[ -n "${GEMINI_API_KEY:-}" ] || die "GEMINI_API_KEY not set locally (judge convention: fail loudly)"
[ -n "${HF_TOKEN:-}" ] || die "HF_TOKEN not set locally — bucket uploads need it (gate reads HF_TOKEN)"
vssh "umask 077 && cat > /root/.axis_env" <<EOF
export GEMINI_API_KEY='${GEMINI_API_KEY}'
export HF_TOKEN='${HF_TOKEN}'
EOF
log "pushed /root/.axis_env (0600): GEMINI_API_KEY len ${#GEMINI_API_KEY}, HF_TOKEN len ${#HF_TOKEN}"

# Non-secret env. NOTE: HF_HUB_DISABLE_XET is deliberately NOT set — buckets
# are Xet-only (temp/42 §8); the old flag would exercise an untested path.
vssh "cat > /root/.axis_env_common" <<EOF
export HF_HOME=$REMOTE_HF_HOME
export HF_HUB_DOWNLOAD_TIMEOUT=20
export TOKENIZERS_PARALLELISM=false
export PYTHONUNBUFFERED=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export OUT_DIR=$OUT_DIR
export UPLOAD_DIR=$UPLOAD_DIR
export REMOTE_AA=$REMOTE_AA
export REMOTE_ROOT=$REMOTE_ROOT
export MODEL_ID=$MODEL_ID
export MODEL_TAG=$MODEL_TAG
export BUCKET_DEST=$BUCKET/$BUCKET_PREFIX
export PUSH_INTERVAL_SEC=$PUSH_INTERVAL_SEC
export S2_BATCH=$S2_BATCH
export JUDGE_RPS=$JUDGE_RPS
export JUDGE_BATCH=$JUDGE_BATCH
export JUDGE_MODEL=$JUDGE_MODEL
export STAGE1_EXTRA="$STAGE1_EXTRA"
export SKIP_S5=${SKIP_S5:-0}
EOF

# ---- 2. push code: runbook remote/, the upload gate, the aa worktree -----
vssh "mkdir -p $REMOTE_ROOT $OUT_DIR $UPLOAD_DIR $REMOTE_HF_HOME"
tar -C "$AXIS_LIB_HERE" --exclude '__pycache__' -czf - remote | vssh "tar -xzf - -C $REMOTE_ROOT"
tar -C "$AXIS_LIB_HERE/.." --exclude '__pycache__' -czf - hf_upload.py \
  | vssh "tar -xzf - -C $REMOTE_ROOT && mv -f $REMOTE_ROOT/hf_upload.py $REMOTE_ROOT/hf_upload_verified.py"
log "staged remote/ + hf_upload_verified.py -> $REMOTE_ROOT/"
# rsync the submodule WORKTREE (the persona-redteaming branch is never
# pushed anywhere; the box gets it only via this copy — bluedot's judge
# patch lesson: gitignored/local fixes never reach the box implicitly).
rsync -a --delete --timeout=120 --exclude '.git' --exclude '.venv' \
  --exclude '__pycache__' --exclude 'transcripts' \
  -e "ssh $SSH_OPTS -i $SSH_KEY -p $SSH_PORT" \
  "$LOCAL_AA/" "root@$SSH_HOST:$REMOTE_AA/"
echo "$AA_HEAD" > "$STATE_DIR/aa_shipped_head"
log "rsynced assistant-axis worktree -> $REMOTE_AA/"
# On-box proof the mods landed (grep, per bluedot's judge-patch verification)
vssh "grep -q 'generativelanguage' $REMOTE_AA/assistant_axis/judge.py" </dev/null \
  || die "judge swap NOT present on box ($REMOTE_AA/assistant_axis/judge.py) — wrong tree shipped"
vssh "grep -rq 'rollouts' $REMOTE_AA/pipeline/4_vectors.py" </dev/null \
  || die "per-rollout export NOT present on box (pipeline/4_vectors.py) — wrong tree shipped"

# ---- 3. assert the prebuilt stack; install pinned pure-python extras -----
vssh "bash -s" <<EOF
set -eu
. /root/.axis_env_common
PY=\$(command -v python3)
echo "IMAGE_PY: \$(\$PY -V)"
\$PY - <<'PYEOF'
import torch, vllm, sys
ok = (vllm.__version__ == "${VLLM_PIN}", torch.__version__.startswith("${TORCH_PIN}"))
print("STACK_ASSERT vllm=%s torch=%s -> %s" % (vllm.__version__, torch.__version__, ok))
sys.exit(0 if all(ok) else 1)
PYEOF
# Pinned extras only; --no-deps so nothing can drag a new torch in. If the
# import smoke below names a missing module, add ITS exact pin from the aa
# uv.lock to EXTRA_PIP_PINS — never unpinned installs (canary step, README §4.2).
\$PY -m pip install --no-deps -q $EXTRA_PIP_PINS
# uv: single static binary for the upload gate's isolated PEP-723 env
# (huggingface_hub 1.7 + hf_xet) — NOT a stack resolve on the image python.
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="\$HOME/.local/bin:\$PATH"
if uv run --no-project $REMOTE_ROOT/hf_upload_verified.py --help >/dev/null 2>&1; then
  echo "GATE_ENV_OK (uv resolved huggingface_hub + hf_xet)"
else
  echo "GATE_ENV_FAIL: uv could not resolve/run the upload gate"; exit 1
fi
\$PY - <<'PYEOF'
import importlib, json, platform, subprocess, sys
import torch, vllm, transformers
assert transformers.__version__ == "4.57.5", transformers.__version__  # 5.x breaks stage 2 (temp/41)
missing = []
for m in ("openai", "safetensors", "huggingface_hub", "jsonlines", "dotenv",
          "sklearn", "plotly", "tqdm"):
    try: importlib.import_module(m)
    except Exception as e: missing.append((m, repr(e)))
# import-smoke the package itself (no GPU work; catches missing deps NOW).
# NB assistant_axis/__init__ pulls .pca -> sklearn+plotly, so this exercises
# the full stage import chain.
sys.path.insert(0, "$REMOTE_AA")
for m in ("assistant_axis", "assistant_axis.judge"):
    try: importlib.import_module(m)
    except Exception as e: missing.append((m, repr(e)))
if missing:
    print("IMPORT_SMOKE_FAILED:", missing); sys.exit(1)
drv = subprocess.run(["nvidia-smi", "--query-gpu=driver_version,name",
                      "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
ok = torch.cuda.is_available()
torch.zeros(1).cuda()  # REAL CUDA init, not the NVML probe
fp = {"python": platform.python_version(), "torch": torch.__version__,
      "torch_cuda": torch.version.cuda, "vllm": vllm.__version__,
      "transformers": transformers.__version__,
      "nvidia_driver_and_gpu": drv, "cuda_available": ok,
      "device": torch.cuda.get_device_name(0) if ok else "NONE",
      "image": "$IMAGE", "aa_head": "$AA_HEAD", "model": "$MODEL_ID"}
with open("$OUT_DIR/env_fingerprint.json", "w") as f:
    json.dump(fp, f, indent=1)
print("PROVISION_FINGERPRINT:", json.dumps(fp))
sys.exit(0 if ok else 1)
PYEOF
EOF
log "stack asserted (vllm==$VLLM_PIN from image), extras pinned, CUDA init OK, fingerprint written"

# ---- 4. judge preflight FROM the box: a REAL completion ------------------
# /v1/models-style checks pass on geo-blocked hosts; only a real completion
# exposes 403 unsupported_country_region_territory (bluedot incident).
if ! vssh "bash $REMOTE_ROOT/remote/judge_preflight.sh" </dev/null; then
  die "JUDGE PREFLIGHT FAILED from the box — geo-block or auth. Destroy \
(--never-provisioned if nothing else ran), blacklist the offer, pick another host"
fi
log "judge preflight OK: real $JUDGE_MODEL completion succeeded from the box"

# ---- 5. model download (idempotent; skips complete files) ----------------
log "downloading $MODEL_ID (skips if cached)"
vssh "bash -s" <<EOF
set -eu
. /root/.axis_env 2>/dev/null || true
. /root/.axis_env_common
python3 - <<'PYEOF'
from huggingface_hub import snapshot_download
p = snapshot_download("$MODEL_ID", ignore_patterns=["*.pth", "original/*"])
print("DOWNLOADED: $MODEL_ID ->", p)
PYEOF
EOF

# ---- 6. disk headroom (a full disk kills the run) ------------------------
vssh "df -h /workspace | tail -1" </dev/null

# Provision marker: from this point the box may hold unique data, so
# 06_destroy.sh's --never-provisioned escape is permanently closed and only
# verified upload + byte-verified capture unlock destroy.
date -u +%FT%TZ > "$STATE_DIR/provisioned_$INSTANCE"
cost_status
log "next: AXIS_CONFIG=$AXIS_CONFIG bash 03_stages.sh   (SLICE=1 first — canary, README §4.2)"
