#!/usr/bin/env bash
# assemble_axis_local.sh — run-2 fleet endgame (temp/45-run2-exec.md):
# pull all aggregate role vectors from the shared bucket prefix, compute the
# axis LOCALLY (S5 is CPU + seconds), verify it, and upload axis.pt +
# RUN_MANIFEST.json through the xet-verified gate.
#
# Fleet workers run S1-S4 on their role shard and skip S5 (SKIP_S5=1): the
# axis = mean(default) - mean(roles) needs ALL roles, so it is assembled
# here once coverage is complete.
#
# Usage: bash tools/assemble_axis_local.sh [--expect N]   (default 276)
case $- in *x*) set +x ;; esac
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AA="$HERE/../../../submodules/assistant-axis"
BUCKET_ID="unrulyabstractions/persona-redteaming"
PREFIX="axis/r1d-qwen-14b"
WORK="$HERE/.state/assembly"
EXPECT="${2:-276}"
[ "${1:-}" = "--expect" ] && EXPECT="$2"

mkdir -p "$WORK/vectors" "$WORK/upload"

echo "[assemble] 1/5 listing + downloading $PREFIX/vectors/*.pt (aggregates only)"
WORK="$WORK" BUCKET_ID="$BUCKET_ID" PREFIX="$PREFIX" \
  uv run --no-project - <<'PY'
# /// script
# requires-python = ">=3.9"
# dependencies = ["huggingface_hub>=1.7.0"]
# ///
import os
from huggingface_hub import HfApi

work, bucket, prefix = os.environ["WORK"], os.environ["BUCKET_ID"], os.environ["PREFIX"]
api = HfApi()
files = [f for f in api.list_bucket_tree(bucket, f"{prefix}/vectors", recursive=False)
         if getattr(f, "size", None) is not None and f.path.endswith(".pt")]
pairs = []
for f in files:
    local = os.path.join(work, "vectors", os.path.basename(f.path))
    if os.path.exists(local) and os.path.getsize(local) == f.size:
        continue
    pairs.append((f.path, local))
print(f"[assemble] {len(files)} remote aggregate vectors; downloading {len(pairs)}")
if pairs:
    api.download_bucket_files(bucket, files=pairs)
PY

N=$(ls "$WORK/vectors"/*.pt 2>/dev/null | wc -l | tr -d ' ')
echo "[assemble] have $N aggregate vectors (expect $EXPECT incl default)"
ls "$WORK/vectors/default.pt" >/dev/null 2>&1 || { echo "FATAL: default.pt missing — axis undefined"; exit 1; }
if [ "$N" -lt "$EXPECT" ]; then
  echo "WARNING: coverage $N/$EXPECT — roles below min_count or workers incomplete; listing missing:"
  python3 - "$WORK/vectors" "$HERE/fleet_shards.json" <<'PY'
import json, os, sys
have = {f[:-3] for f in os.listdir(sys.argv[1]) if f.endswith(".pt")}
want = {r for w in json.load(open(sys.argv[2]))["workers"].values() for r in w["roles"]}
missing = sorted(want - have)
print(f"missing {len(missing)}: {' '.join(missing[:40])}{' ...' if len(missing) > 40 else ''}")
PY
  [ "${ALLOW_PARTIAL:-0}" = "1" ] || { echo "set ALLOW_PARTIAL=1 to proceed anyway (documented in manifest)"; exit 2; }
fi

echo "[assemble] 2/5 running 5_axis.py (local, CPU)"
(cd "$AA" && PYTHONPATH="$AA" .venv/bin/python pipeline/5_axis.py \
  --vectors_dir "$WORK/vectors" --output "$WORK/upload/axis.pt")

echo "[assemble] 3/5 verifying axis.pt"
"$AA/.venv/bin/python" - "$WORK/upload/axis.pt" <<'PY'
import sys
import torch
t = torch.load(sys.argv[1], map_location="cpu", weights_only=False)
ten = t if torch.is_tensor(t) else next(v for v in (t.values() if isinstance(t, dict) else [t]) if torch.is_tensor(v))
assert ten.ndim == 2 and ten.shape[0] == 48 and ten.shape[1] == 5120, f"shape {tuple(ten.shape)} != (48,5120)"
assert torch.isfinite(ten.float()).all(), "non-finite values in axis"
print(f"[assemble] axis OK: shape {tuple(ten.shape)} dtype {ten.dtype} "
      f"norm {ten.float().norm():.3f}")
PY

echo "[assemble] 4/5 writing RUN_MANIFEST.json + copying fleet_shards.json"
N_NOW=$(ls "$WORK/vectors"/*.pt | wc -l | tr -d ' ')
python3 - "$HERE" "$WORK" "$N_NOW" <<'PY'
import datetime as dt
import json
import sys
here, work, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
shards = json.load(open(f"{here}/fleet_shards.json"))
m = {
  "run": "axis run 2 (DeepSeek-R1-Distill-Qwen-14B), FLEET execution",
  "date_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
  "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
  "protocol": "FULL upstream protocol: 276 roles x 5 prompts x 240 questions, max_tokens 512, temp 0.7, top_p 0.9, min_count 50",
  "code": "assistant-axis branch persona-redteaming @ 3bb4ef1 (fork unrulyabstractions/assistant-axis; upstream 8ad523e + optimization merge temp/48: S1 role pooling, S2 byte-compat default path, exact-rate judge limiter, S4 workers)",
  "judge": "gemini-flash-lite-latest via OpenAI-compatible generativelanguage endpoint (hub convention)",
  "execution": "8-worker vast.ai fleet, roles sharded contiguously over the sorted role list, weighted by card class; each worker ran S1->S2||S3->S4 on its shard and uploaded through the xet-verified gate; S5 assembled locally from bucket aggregates",
  "fleet": shards,
  "artifact_conventions": {
    "layer_rows": "decoder layers 0..47, NO embedding row (upstream convention)",
    "dtype": "bf16",
    "per_rollout": "vectors/rollouts/{role}.safetensors + {role}.manifest.json (scores + n_response_tokens embedded)",
    "aggregates": "vectors/{role}.pt (may be < 276 where min_count=50 failed)",
    "worker_meta": "meta/r1d-qwen-14b-wN/ (env fingerprints, stage logs)",
  },
  "coverage_aggregate_vectors": n,
  "caveats": [
    "activations/{role}.pt resume stores are NOT in the bucket (byte-dup of rollouts safetensors); they are in the byte-verified local captures",
    "judge model alias -latest: served version unresolvable via API; cost bands documented in temp/45-run2-exec.md",
  ],
}
json.dump(m, open(f"{work}/upload/RUN_MANIFEST.json", "w"), indent=1)
json.dump(shards, open(f"{work}/upload/fleet_shards.json", "w"), indent=1)
print("[assemble] manifest written")
PY

echo "[assemble] 5/5 uploading via xet-verified gate"
uv run --no-project "$HERE/../../hf_upload.py" \
  "$WORK/upload" "hf://buckets/$BUCKET_ID/$PREFIX" --allow-extra
echo "[assemble] ASSEMBLY_GATE_RC=$? (0 = axis.pt + manifests verified on bucket)"
