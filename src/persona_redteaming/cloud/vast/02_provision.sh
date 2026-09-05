#!/usr/bin/env bash
# 02_provision.sh — ship code + secrets, install the campaign extras, download
# the base model at its pinned revision, fingerprint the environment, start
# vLLM, and prove the server is serving. This campaign serves the BASE model
# with no adapter of any kind.
#
# ADAPTED from workspace/axis-run/runbook/02_provision.sh.
#
# CLOUD.md law 2: the image already carries torch + transformers + vLLM. This
# script RECORDS those three versions before the installs and FAILS if any of
# them moved. If you ever find yourself resolving a serving stack here, STOP:
# wrong image.
#
# Secrets: HF_TOKEN travels over the ssh STDIN channel into a 0600 file. It
# never appears in argv, in this log, in a remote log, or in any generated
# file. Only its NAME and byte length are ever printed. No judge runs in a
# campaign (mechanical grading only), so no other key is shipped.
#
# Two trees go to the box: the persona-redteaming package (LOCAL_PKG, the
# repo root; workspace/, temp/, data and venvs excluded) and the
# agent-interp-envs fork (LOCAL_AIE, the pinned submodule). Both are pip
# installed editable into the IMAGE's python.
#
# Idempotent: every remote step checks its own done-condition first, so a
# re-run after a transient failure resumes rather than repeats.
set -euo pipefail
case "$-" in *x*) echo "FATAL: xtrace on in a secret-adjacent script"; exit 90;; esac
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_cmd rsync
require_cmd python3
require_instance
resolve_ssh_target
cost_guard_or_die
require_resolved IMAGE "$IMAGE"
require_resolved MODEL_ID "$MODEL_ID"

log "provisioning instance $INSTANCE at $SSH_HOST:$SSH_PORT for $MODEL_ID at revision ${MODEL_REVISION:-<unpinned>}"
assert_box_identity

# ---- 0. local preconditions: every source tree must exist ----------------
MISSING=""
for pair in "LOCAL_PKG:$LOCAL_PKG" "LOCAL_AIE:$LOCAL_AIE"; do
  name="${pair%%:*}"; path="${pair#*:}"
  if [ ! -d "$path" ]; then
    MISSING="${MISSING}  $name -> $path (NOT A DIRECTORY)\n"
  elif [ -z "$(ls -A "$path" 2>/dev/null)" ]; then
    MISSING="${MISSING}  $name -> $path (EMPTY)\n"
  fi
done
if [ -n "$MISSING" ]; then
  printf 'FATAL: campaign source trees are missing or empty:\n%b' "$MISSING" >&2
  die "put the code in place (or override LOCAL_PKG/LOCAL_AIE in the config) and re-run 02"
fi
[ -f "$LOCAL_PKG/pyproject.toml" ] || die "LOCAL_PKG=$LOCAL_PKG has no pyproject.toml (point it at the persona-redteaming repo root)"
log "source trees OK: $LOCAL_PKG, $LOCAL_AIE"

# ---- 1. secret by stdin into a 0600 file (name + length only in logs) ----
[ -n "${HF_TOKEN:-}" ] || die "HF_TOKEN not set in the local shell env (the model download needs it)"

vssh "mkdir -p '$REMOTE_ROOT' '$REMOTE_PKG' '$REMOTE_AIE' '$REMOTE_HF_HOME'" </dev/null

printf '%s' "$HF_TOKEN"       | vssh "umask 077 && cat > /root/.hf_token"
REMOTE_HF_LEN="$(vssh "wc -c < /root/.hf_token" </dev/null | tr -d '[:space:]')"
[ "$REMOTE_HF_LEN" = "${#HF_TOKEN}" ] \
  || die "HF_TOKEN delivery truncated (local ${#HF_TOKEN} bytes, remote $REMOTE_HF_LEN) — re-run 02"
log "secret delivered by name: HF_TOKEN (${#HF_TOKEN} bytes), 0600, byte-length verified; no judge key shipped"

# /root/.qc_env carries NO secret value: it reads the 0600 file at source time.
# Written with a QUOTED heredoc, so nothing is expanded on this machine.
vssh "umask 077 && cat > /root/.qc_env" <<'QCENV'
# shellcheck shell=bash
# sourced by every on-box script; reads the 0600 token file, holds no values
# shellcheck disable=SC2155  # a missing token file must yield "", not an error
export HF_TOKEN="$(cat /root/.hf_token 2>/dev/null)"
QCENV

# ---- 2. non-secret environment ------------------------------------------
vssh "cat > /root/.qc_env_common" <<EOF
export HF_HOME='$REMOTE_HF_HOME'
export HF_HUB_DOWNLOAD_TIMEOUT=20
export TOKENIZERS_PARALLELISM=false
export PYTHONUNBUFFERED=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export REMOTE_ROOT='$REMOTE_ROOT'
export REMOTE_PKG='$REMOTE_PKG'
export REMOTE_AIE='$REMOTE_AIE'
export MODEL_ID='$MODEL_ID'
export MODEL_REVISION='$MODEL_REVISION'
export SERVED_MODEL_NAME='$SERVED_MODEL_NAME'
export MAX_MODEL_LEN='$MAX_MODEL_LEN'
export VLLM_PORT='$VLLM_PORT'
export VLLM_HOST='$VLLM_HOST'
export VLLM_EXTRA_ARGS='$VLLM_EXTRA_ARGS'
export VLLM_LOG='$VLLM_LOG'
export EXTRA_PIP='$EXTRA_PIP'
export AIE_PIP_ARGS='$AIE_PIP_ARGS'
export FINGERPRINT_PATH='$FINGERPRINT_PATH'
export QC_IMAGE='$IMAGE'
export QC_INSTANCE='$INSTANCE'
EOF
log "wrote /root/.qc_env (0600, no values) and /root/.qc_env_common"

# ---- 3. ship the three code trees ---------------------------------------
rsync_tree() {  # rsync_tree <local> <remote> <extra rsync excludes...>
  local src="$1" dst="$2"; shift 2
  local args=(-a --delete --timeout=120 --exclude '__pycache__' --exclude '.git'
              --exclude '*.pyc' --exclude '.DS_Store')
  local e
  for e in "$@"; do args+=(--exclude "$e"); done
  rsync "${args[@]}" -e "ssh $SSH_OPTS -i $SSH_KEY -p $SSH_PORT" "$src/" "root@$SSH_HOST:$dst/"
}
rsync_tree "$LOCAL_PKG" "$REMOTE_PKG" '.venv' '.pytest_cache' 'workspace' 'temp' \
  '_OLD_DATA' 'paper' 'submodules' '.backups' '*.log'
log "rsynced $LOCAL_PKG -> $REMOTE_PKG (package only; workspace/, temp/, data, submodules excluded)"
rsync_tree "$LOCAL_AIE"     "$REMOTE_AIE"     '.venv' '.pytest_cache'
log "rsynced $LOCAL_AIE -> $REMOTE_AIE"

REMOTE_FILE_COUNT="$(vssh "find '$REMOTE_PKG' '$REMOTE_AIE' -type f | wc -l" </dev/null | tr -d '[:space:]')"
LOCAL_FILE_COUNT="$(find "$LOCAL_PKG/src" "$LOCAL_PKG/tests" "$LOCAL_PKG/pyproject.toml" "$LOCAL_AIE" -type f \
  -not -path '*/.git/*' -not -path '*/.venv/*' -not -path '*/__pycache__/*' \
  -not -path '*/.pytest_cache/*' -not -name '*.pyc' -not -name '.DS_Store' | wc -l | tr -d ' ')"
log "code shipped: $LOCAL_FILE_COUNT local files -> $REMOTE_FILE_COUNT remote files"
[ "$REMOTE_FILE_COUNT" -ge 1 ] || die "no files landed on the box — rsync silently did nothing"

# ---- 4. generate the on-box scripts (quoted heredocs: zero local expansion)
# Every remote script is syntax-checked ON THE BOX with `bash -n` before it is
# run. That checks the exact bytes that will execute — a heredoc bug in a
# launcher once made every offer search return "no offer".
run_remote() {  # run_remote <script-name>
  local name="$1"
  vssh "bash -n '$REMOTE_ROOT/$name'" </dev/null || die "$name failed bash -n ON THE BOX — do not run it"
  vssh "bash '$REMOTE_ROOT/$name'" </dev/null || die "$name failed on the box (see output above)"
}

vssh "cat > '$REMOTE_ROOT/qc_pip.sh'" <<'REMOTE_PIP'
#!/usr/bin/env bash
# Install the campaign extras into the IMAGE's python. Asserts that torch,
# transformers and vllm are exactly what the image shipped, before and after.
set -euo pipefail
case $- in *x*) set +x ;; esac
# shellcheck source=/dev/null
. /root/.qc_env_common
PY="$(command -v python3)"
echo "IMAGE_PYTHON: $("$PY" -V 2>&1)  ($PY)"

stack_versions() {
  "$PY" - <<'PYEOF'
from importlib.metadata import version, PackageNotFoundError
out = []
for p in ("torch", "transformers", "vllm"):
    try:
        out.append(version(p))
    except PackageNotFoundError:
        out.append("MISSING")
print(" ".join(out))
PYEOF
}

BEFORE="$(stack_versions)"
echo "STACK_BEFORE torch/transformers/vllm = $BEFORE"

echo "--- pip install -e $REMOTE_AIE $AIE_PIP_ARGS ---"
# shellcheck disable=SC2086  # AIE_PIP_ARGS must word-split (e.g. '--no-deps')
"$PY" -m pip install -q $AIE_PIP_ARGS -e "$REMOTE_AIE"
echo "--- pip install $EXTRA_PIP ---"
# shellcheck disable=SC2086
"$PY" -m pip install -q $EXTRA_PIP
# The package itself: --no-deps, because its runtime deps are the image's
# torch/transformers (law 2) plus what EXTRA_PIP names; the pyproject's
# path dependency on agent-interp-envs is satisfied by the editable install
# above, not by resolution.
echo "--- pip install --no-deps -e $REMOTE_PKG ---"
"$PY" -m pip install -q --no-deps -e "$REMOTE_PKG"

AFTER="$(stack_versions)"
echo "STACK_AFTER  torch/transformers/vllm = $AFTER"
# torch and vllm are the serving stack CLOUD.md law 2 protects: if either
# moved, a wheel was resolved on a billing box and nothing downstream can be
# trusted. transformers is different. The campaign's renderer was MEASURED
# against transformers 5.16.1 (temp/10; the 5.x BatchEncoding behaviour is
# handled explicitly in rendering/render.py), and aie requires >=5.16.1, so a
# transformers bump is intended, not an accident. It is reported loudly and
# recorded in the fingerprint; the vLLM health check downstream is the real
# test of whether the server still works with it.
BEFORE_TORCH="$(echo "$BEFORE" | cut -d' ' -f1)"
BEFORE_TF="$(echo "$BEFORE" | cut -d' ' -f2)"
BEFORE_VLLM="$(echo "$BEFORE" | cut -d' ' -f3)"
AFTER_TORCH="$(echo "$AFTER" | cut -d' ' -f1)"
AFTER_TF="$(echo "$AFTER" | cut -d' ' -f2)"
AFTER_VLLM="$(echo "$AFTER" | cut -d' ' -f3)"
if [ "$BEFORE_TORCH" != "$AFTER_TORCH" ] || [ "$BEFORE_VLLM" != "$AFTER_VLLM" ]; then
  echo "FATAL: the SERVING stack moved during provisioning (CLOUD.md law 2)."
  echo "       torch : $BEFORE_TORCH -> $AFTER_TORCH"
  echo "       vllm  : $BEFORE_VLLM -> $AFTER_VLLM"
  echo "       Deliberate ways forward: set AIE_PIP_ARGS='--no-deps' and add what"
  echo "       aie actually needs to EXTRA_PIP, or pick a different vLLM image."
  echo "       Do NOT proceed on a moved serving stack."
  exit 1
fi
if [ "$BEFORE_TF" != "$AFTER_TF" ]; then
  echo "NOTE: transformers moved $BEFORE_TF -> $AFTER_TF (expected: aie requires"
  echo "      >=5.16.1, the version the renderer was measured against). Recorded"
  echo "      in env_fingerprint.json; the vLLM health check is the real test."
fi

echo "--- resolved versions ---"
"$PY" - <<'PYEOF'
from importlib.metadata import version, PackageNotFoundError
import platform, sys
print("python           ", platform.python_version(), sys.executable)
for p in ("torch", "transformers", "vllm", "accelerate",
          "safetensors", "pytest", "huggingface-hub", "numpy"):
    try:
        print("%-17s %s" % (p, version(p)))
    except PackageNotFoundError:
        print("%-17s MISSING" % p)
PYEOF

# Prove both editable installs are actually registered and importable.
"$PY" - <<'PYEOF'
import importlib, os
from importlib.metadata import distributions
for var in ("REMOTE_AIE", "REMOTE_PKG"):
    target = os.path.realpath(os.environ[var])
    hits = []
    for d in distributions():
        try:
            du = d.read_text("direct_url.json") or ""
        except Exception:
            du = ""
        if target in du:
            hits.append("%s==%s" % (d.metadata["Name"], d.version))
    print("EDITABLE_INSTALL_FROM", target, ":", hits or "NONE")
    if not hits:
        print("FATAL: 'pip install -e' did not register a distribution from", target)
        raise SystemExit(1)
for mod in ("agent_interp_envs.providers.tolerant_vllm_provider",
            "persona_redteaming.envs.campaign", "persona_redteaming.replay.harvest"):
    importlib.import_module(mod)
    print("IMPORT_OK", mod)
PYEOF
# The environments tell the agent to run `python`, and their graders shell out
# to ["python", "-m", "pytest", ...]. The reference image (python:3.11-slim)
# provides `python`; this vLLM image provides only `python3`, and the agent's
# sanitized PATH would resolve neither. MEASURED on instance 49864289: `python`
# did not exist, so every submission in every rollout would have failed with
# "command not found" and every reward would have been null, silently. Restore
# the reference image's behaviour and PROVE it under the exact sanitized PATH
# the agent shell is given.
ln -sf /usr/bin/python3 /usr/local/bin/python
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  sh -c 'command -v python >/dev/null && python -V && python -m pytest --version' \
  || { echo "FATAL: the agent's sanitized PATH cannot reach python + pytest"; exit 1; }
echo "AGENT_TOOLCHAIN_OK"
echo "PIP_STAGE_OK"
REMOTE_PIP
run_remote qc_pip.sh
log "python extras installed; serving stack UNCHANGED (law 2 assertion passed)"

vssh "cat > '$REMOTE_ROOT/qc_download.sh'" <<'REMOTE_DL'
#!/usr/bin/env bash
# Download the base model into $HF_HOME at the pinned $MODEL_REVISION. There is
# no adapter in this campaign: what vLLM serves is exactly these weights.
set -euo pipefail
case $- in *x*) set +x ;; esac
# shellcheck source=/dev/null
. /root/.qc_env_common
# shellcheck source=/dev/null
. /root/.qc_env
PY="$(command -v python3)"

# An empty MODEL_REVISION means "track the branch head" and passes no flag. A
# non-empty one is passed to EVERY resolution path below, the local_files_only
# re-resolution included: downloading by commit sha writes snapshots/<sha> and
# no refs/main, so a later default-revision lookup would not find the snapshot.
REV_ARGS=()
if [ -n "$MODEL_REVISION" ]; then
  REV_ARGS+=(--revision "$MODEL_REVISION")
fi

# ---- base model (cached; a re-run resumes and skips complete shards) ----
echo "--- downloading $MODEL_ID rev=${MODEL_REVISION:-<branch head>} into HF_HOME=$HF_HOME ---"
if command -v hf >/dev/null 2>&1; then
  hf download "$MODEL_ID" ${REV_ARGS[@]+"${REV_ARGS[@]}"} >/dev/null
elif command -v huggingface-cli >/dev/null 2>&1; then
  huggingface-cli download "$MODEL_ID" ${REV_ARGS[@]+"${REV_ARGS[@]}"} >/dev/null
else
  "$PY" -c 'import os
from huggingface_hub import snapshot_download
snapshot_download(os.environ["MODEL_ID"],
                  revision=os.environ.get("MODEL_REVISION") or None)' >/dev/null
fi

SNAP="$("$PY" -c 'import os
from huggingface_hub import snapshot_download
print(snapshot_download(os.environ["MODEL_ID"],
                        revision=os.environ.get("MODEL_REVISION") or None,
                        local_files_only=True))')"
echo "MODEL_SNAPSHOT=$SNAP"
"$PY" - "$SNAP" <<'PYEOF'
import json, os, sys
snap = sys.argv[1]
idx = os.path.join(snap, "model.safetensors.index.json")
if os.path.exists(idx):
    with open(idx) as fh:
        files = sorted(set(json.load(fh)["weight_map"].values()))
else:
    files = sorted(f for f in os.listdir(snap) if f.endswith(".safetensors"))
missing, total = [], 0
for f in files:
    p = os.path.join(snap, f)
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        missing.append(f)
    else:
        total += os.path.getsize(p)
print("MODEL_SHARDS=%d TOTAL_GB=%.1f" % (len(files), total / 1e9))
if missing:
    print("FATAL: missing/empty shards:", missing)
    raise SystemExit(1)
PYEOF

echo "DOWNLOAD_STAGE_OK"
REMOTE_DL
run_remote qc_download.sh
log "base model downloaded at revision ${MODEL_REVISION:-<branch head>}; every shard present and non-empty (ASSERTED on the box)"

vssh "cat > '$REMOTE_ROOT/qc_fingerprint.sh'" <<'REMOTE_FP'
#!/usr/bin/env bash
set -euo pipefail
case $- in *x*) set +x ;; esac
# shellcheck source=/dev/null
. /root/.qc_env_common
python3 - <<'PYEOF'
import json, os, platform, subprocess, sys
from importlib.metadata import version, PackageNotFoundError

def ver(p):
    try:
        return version(p)
    except PackageNotFoundError:
        return None

def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""

import torch
cuda_ok = torch.cuda.is_available()
torch.zeros(1).cuda()   # REAL CUDA init, not the NVML probe

fp = {
    "instance": os.environ.get("QC_INSTANCE"),
    "image": os.environ.get("QC_IMAGE"),
    "written_utc": sh("date", "-u", "+%Y-%m-%dT%H:%M:%SZ"),
    "python": platform.python_version(),
    "python_executable": sys.executable,
    "torch": ver("torch"),
    "torch_cuda": torch.version.cuda,
    "transformers": ver("transformers"),
    "vllm": ver("vllm"),
    "accelerate": ver("accelerate"),
    "safetensors": ver("safetensors"),
    "huggingface_hub": ver("huggingface-hub"),
    "cuda_available": cuda_ok,
    "gpu_name": torch.cuda.get_device_name(0) if cuda_ok else "NONE",
    "gpu_count": torch.cuda.device_count(),
    "nvidia_smi": sh("nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                     "--format=csv,noheader"),
    "model_id": os.environ["MODEL_ID"],
    "model_revision": os.environ.get("MODEL_REVISION") or None,
    "served_model_name": os.environ["SERVED_MODEL_NAME"],
    "max_model_len": int(os.environ["MAX_MODEL_LEN"]),
}
path = os.environ["FINGERPRINT_PATH"]
with open(path, "w") as fh:
    json.dump(fp, fh, indent=1, sort_keys=True)
print("FINGERPRINT_WRITTEN", path)
print(json.dumps(fp, indent=1, sort_keys=True))
if not cuda_ok:
    raise SystemExit(1)
PYEOF
echo "FINGERPRINT_STAGE_OK"
REMOTE_FP
run_remote qc_fingerprint.sh
log "wrote $FINGERPRINT_PATH on the box (versions, CUDA/driver, GPU, image, model id + revision)"

vssh "cat > '$REMOTE_ROOT/qc_serve.sh'" <<'REMOTE_SERVE'
#!/usr/bin/env bash
# Start vLLM detached. Idempotent: exits early if the server already answers.
set -euo pipefail
case $- in *x*) set +x ;; esac
# shellcheck source=/dev/null
. /root/.qc_env_common
# shellcheck source=/dev/null
. /root/.qc_env

probe() {
  python3 - <<'PYEOF'
import json, os, sys, urllib.request
url = "http://%s:%s/v1/models" % (os.environ["VLLM_HOST"], os.environ["VLLM_PORT"])
try:
    with urllib.request.urlopen(url, timeout=5) as r:
        json.load(r)
except Exception:
    sys.exit(1)
PYEOF
}

if probe; then
  echo "SERVE_ALREADY_UP on $VLLM_HOST:$VLLM_PORT"
  exit 0
fi

if [ -f /workspace/vllm.pid ] && kill -0 "$(cat /workspace/vllm.pid)" 2>/dev/null; then
  echo "SERVE_ALREADY_STARTING pid=$(cat /workspace/vllm.pid) (not yet answering; see $VLLM_LOG)"
  exit 0
fi

command -v vllm >/dev/null 2>&1 || { echo "FATAL: no 'vllm' binary in the image"; exit 1; }
mkdir -p "$(dirname "$VLLM_LOG")"
echo "=== vllm serve started $(date -u +%FT%TZ) ===" >> "$VLLM_LOG"

# The pid file is written by the child itself, which then EXECs vllm, so the
# recorded pid is always the real server. Never pgrep -f for it: -f self-matches
# the probing shell and has produced false liveness before.
SETSID=""
if command -v setsid >/dev/null 2>&1; then SETSID=setsid; fi
rm -f /workspace/vllm.pid
# Serve the SAME revision that was downloaded. An empty MODEL_REVISION passes
# no flag and vLLM resolves the branch head.
REV_ARGS=()
if [ -n "$MODEL_REVISION" ]; then
  REV_ARGS+=(--revision "$MODEL_REVISION")
fi
# shellcheck disable=SC2086  # $SETSID and $VLLM_EXTRA_ARGS must word-split
$SETSID nohup bash -c 'echo $$ > /workspace/vllm.pid; exec vllm serve "$@"' vllm-serve \
  "$MODEL_ID" \
  --served-model-name "$SERVED_MODEL_NAME" \
  ${REV_ARGS[@]+"${REV_ARGS[@]}"} \
  --max-model-len "$MAX_MODEL_LEN" \
  --port "$VLLM_PORT" \
  --host "$VLLM_HOST" \
  $VLLM_EXTRA_ARGS \
  </dev/null >> "$VLLM_LOG" 2>&1 &
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -f /workspace/vllm.pid ] && break; sleep 1; done
[ -f /workspace/vllm.pid ] \
  || { echo "FATAL: vllm never wrote its pid file. Tail of $VLLM_LOG:"; tail -40 "$VLLM_LOG"; exit 1; }
sleep 3
kill -0 "$(cat /workspace/vllm.pid)" 2>/dev/null \
  || { echo "FATAL: vllm died within seconds. Tail of $VLLM_LOG:"; tail -40 "$VLLM_LOG"; exit 1; }
echo "SERVE_LAUNCHED pid=$(cat /workspace/vllm.pid) log=$VLLM_LOG"
echo "SERVE_CMD vllm serve $MODEL_ID --served-model-name $SERVED_MODEL_NAME \
${MODEL_REVISION:+--revision $MODEL_REVISION} \
--max-model-len $MAX_MODEL_LEN --port $VLLM_PORT --host $VLLM_HOST $VLLM_EXTRA_ARGS"
REMOTE_SERVE
run_remote qc_serve.sh
log "vLLM launch issued: base $MODEL_ID rev=${MODEL_REVISION:-<branch head>} as '$SERVED_MODEL_NAME' on $VLLM_HOST:$VLLM_PORT"

# ---- 5. health wait (loading tens of GB of weights takes minutes) --------
vssh "cat > '$REMOTE_ROOT/qc_health.sh'" <<'REMOTE_HEALTH'
#!/usr/bin/env bash
# Prints the served model ids, one per line, or exits non-zero.
set -euo pipefail
case $- in *x*) set +x ;; esac
# shellcheck source=/dev/null
. /root/.qc_env_common
python3 - <<'PYEOF'
import json, os, sys, urllib.request
url = "http://%s:%s/v1/models" % (os.environ["VLLM_HOST"], os.environ["VLLM_PORT"])
with urllib.request.urlopen(url, timeout=10) as r:
    d = json.load(r)
ids = [m.get("id") for m in d.get("data", [])]
if not ids:
    sys.exit(1)
for i in ids:
    print(i)
PYEOF
REMOTE_HEALTH
vssh "bash -n '$REMOTE_ROOT/qc_health.sh'" </dev/null || die "qc_health.sh failed bash -n on the box"

DEADLINE=$(( $(date +%s) + VLLM_HEALTH_DEADLINE_SEC ))
MODELS=""
log "health-waiting on /v1/models (deadline ${VLLM_HEALTH_DEADLINE_SEC}s; loading the weights takes minutes)"
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if MODELS="$(vssh "bash '$REMOTE_ROOT/qc_health.sh'" </dev/null 2>/dev/null)"; then
    [ -n "$MODELS" ] && break
  fi
  # Only conclude "died" when the pid file EXISTS and its process is dead. A
  # missing pid file means an already-running server we did not start, so a
  # momentary probe failure must not be read as a crash.
  if vssh "[ -f /workspace/vllm.pid ] && ! kill -0 \$(cat /workspace/vllm.pid) 2>/dev/null" \
       </dev/null 2>/dev/null; then
    log "vLLM process is GONE. Tail of $VLLM_LOG:"
    vssh "tail -60 '$VLLM_LOG'" </dev/null || true
    die "vLLM exited before becoming healthy — read the tail above, fix, then re-run 02 (it is idempotent)"
  fi
  log "  not up yet ($(( (DEADLINE - $(date +%s)) / 60 ))min left); last log line: \
$(vssh "tail -1 '$VLLM_LOG' 2>/dev/null" </dev/null 2>/dev/null | cut -c1-140)"
  sleep "$VLLM_HEALTH_POLL_SEC"
done

if [ -z "$MODELS" ]; then
  log "HEALTH WAIT TIMED OUT after ${VLLM_HEALTH_DEADLINE_SEC}s. Tail of $VLLM_LOG:"
  vssh "tail -80 '$VLLM_LOG'" </dev/null || true
  die "vLLM never served /v1/models. Common causes: the KV cache does not fit (set \
VLLM_EXTRA_ARGS='--gpu-memory-utilization 0.94' or lower MAX_MODEL_LEN), or MODEL_REVISION \
does not exist in the repo. Box stays ALIVE for debugging; the watchdog still enforces the cap."
fi

log "vLLM is serving. /v1/models reports:"
printf '%s\n' "$MODELS" | while read -r m; do log "  - $m"; done
printf '%s\n' "$MODELS" | grep -qx "$SERVED_MODEL_NAME" \
  || die "served model '$SERVED_MODEL_NAME' is NOT in /v1/models — check --served-model-name"
log "'$SERVED_MODEL_NAME' is served (base weights, no adapter)"

# ---- 6. disk headroom (a full disk kills the run) ------------------------
vssh "df -h '$REMOTE_ROOT' | tail -1" </dev/null

# ---- 7. provision marker -------------------------------------------------
# From here the box may hold unique data, so 05_destroy.sh's
# --never-provisioned escape is permanently closed: only a byte-verified
# capture unlocks destroy.
date -u +%FT%TZ > "$STATE_DIR/provisioned_$INSTANCE"
log "provision marker written: $STATE_DIR/provisioned_$INSTANCE (--never-provisioned is now CLOSED)"
cost_status
log "next: run the campaign, then bash 04_capture.sh"
