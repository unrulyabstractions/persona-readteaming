# Axis-run runbook: assistant-axis extraction on vast.ai

Now at `persona_redteaming/cloud/axis_runbook/` (moved from
`workspace/axis-run/runbook/`, which stays on `proto/axis-run` with the eight
fleet configs and the run's `.state/` archives). Two paths changed: the
upload gate is `../hf_upload.py` (`persona_redteaming.cloud.hf_upload`,
staged on the box as `hf_upload_verified.py` so the remote scripts are
unchanged), and `LOCAL_AA` resolves four levels up to
`submodules/assistant-axis`. Every knob is in `config.<model>.env`, selected
by `AXIS_CONFIG`; `tools/gen_fleet_configs.py` regenerates the per-worker
fleet configs from `fleet_shards.json`.

Executable runbook for the two extraction runs (workstream 2, temp/00-hub.md):

| Run | Model | GPU plan | Config |
|---|---|---|---|
| 1 | Qwen/Qwen3-1.7B | RTX 4090 24GB ($0.31-0.54/hr measured) | `config.qwen3-1.7b.env` |
| 2 | deepseek-ai/DeepSeek-R1-Distill-Qwen-14B | A100_SXM4-80GB ($1.03-1.07/hr measured), H100_SXM fallback | `config.r1d-qwen-14b.env` |

Run 1 FIRST: it doubles as the campaign canary for the image/stack/judge/gate
(CLOUD.md law 10). Both runs: pipeline = `submodules/assistant-axis` branch
`persona-redteaming` @ 8ad523e (judge swap 70af387, per-rollout export 893bb05,
R1 span fix 8ad523e — temp/41), judge = gemini-flash-lite-latest (hub
convention), every artifact through the xet-verified bucket gate (temp/42),
destroy gated on capture AND upload both green.

Sources of every number: temp/40 (bluedot lessons), temp/41 (pipeline anatomy
+ workload), temp/42 (bucket gate), temp/13 + CLOUD.md (vast discipline),
plus this runbook's own measurements (2026-09-04): offer searches in
`.state/` archives, Gemini probe in `tools/gemini_limits_measured.json`.

---

## 1. Preconditions (all BEFORE 01_launch)

1.1 **Secrets in the local shell env**: `HF_TOKEN` (bucket writes),
`GEMINI_API_KEY` (judge). Never printed, never in argv; 02 ships them
stdin -> 0600 `/root/.axis_env`. `vastai` CLI configured (1.0.13), ssh key
registered (`vastai show ssh-keys` — 01 checks).

1.2 **Submodule state**: `submodules/assistant-axis` on branch
`persona-redteaming` containing 8ad523e (02 verifies branch + ancestry and
greps the judge swap + rollout export ON THE BOX after rsync). The branch is
never pushed; the box only ever gets it via 02's rsync.

1.3 **Bucket decision (REQUIRED, temp/42)**: the bucket
`unrulyabstractions/persona-redteaming` is PUBLIC (world-readable) and the
account is FREE (public storage best-effort beyond a few GB; private tier
caps at 100GB free). The two runs upload ~40GB + ~165GB (§3). Storage needs
PRO (~$9/mo, 10TB public) either way; visibility is a user call. 03 refuses
to start without `ACK_PUBLIC_BUCKET=1`.

1.4 **Credit (MEASURED 2026-09-04: $10.31 -> $10.11)**: top up before run 2
— 01 refuses when credit < the run's MAX_DOLLARS. Recommended top-up: >= $75
total for both runs (§2 cost model). Judge spend bills to the GEMINI key's
Google project, not vast (§5).

1.5 **Local disk for capture (MEASURED: 643GiB free)**: run 2's capture
pulls ~350GB. Re-check `df -h` before 05.

1.6 **Foreign instances**: 49841102 (workstream 1) is hard-refused by
`lib.sh:assert_ours`. Every box we create is labeled `axis-run`.

## 2. GPU plan + cost model (offers re-measured 2026-09-04, read-only)

Search archives: `.state/<tag>/offers_*.json` at launch; this session's
market sweep (`vastai search offers`, 09:41 UTC):

| Tier | n | $/hr min/med | Best candidates above the bar (rel>=0.995 in-query at launch; snapshot bar rel>=0.98) |
|---|---|---|---|
| A100_SXM4-80GB | 5 | 0.95 / 1.03 | id 29019348 $1.028 rel .991 7.8Gbps 1178GB cuda13.0 CZ; id 29019332 $1.068 rel .999 8.0Gbps 2238GB cuda13.0 CZ |
| H100-class | 16 | 1.74 / 2.79 | id 34047942 $2.00 rel .998 884Mbps 2960GB cuda12.8 US (the $1.74 offer has only 64GB disk — rejected) |
| 48GB (A6000/6000Ada/L40S/A40) | 15 | 0.34 / 0.63 | id 49507681 RTX 6000Ada $0.603 rel .998 11.9Gbps 505GB US |
| RTX 4090 24GB | 45 | 0.31 / 0.39 | n=23 above the quality bar; e.g. $0.335-0.349 NL, 1.0-1.9TB disk |

Decisions (CLOUD.md law 3 + coordinator sizing + bluedot §2.2):
- **Run 1 (1.7B): RTX 4090 tier**, floor $0.30 / cap $0.60 (no
  bottom-fishing), fallback 48GB pro cards. 24GB is bluedot's proven tier for
  this size; stage-2 batch 8.
- **Run 2 (14B): A100_SXM4-80GB**, floor $0.90 / cap $1.60 ($1-1.5 policy
  band; both measured candidates land at $1.03-1.07). Fallback H100_SXM
  $1.60-2.60. A100_PCIE is EXCLUDED (295W power-cap incident: 8h instead of
  ~2.5h); 40GB is proven-insufficient; 48GB unproven for a 14B axis run.
- In-query: `verified=true reliability>0.995 cuda_vers>=12.8 inet_down>=1000
  inet_up>=500 disk_space>=DISK cpu_ram>=32 dph_total<=cap`, then client-side
  re-check of EVERY field incl. the floor and `cuda_max_good>=IMAGE_CUDA_MIN`
  (DSL silently drops unknown clauses). Supply is thin (n=5 at 80GB) — if
  both A100 candidates are gone at launch, re-run the sweep; do not loosen
  the reliability bar.

Cost model (stage estimates from temp/41 §5; wall = S1 + max(S2, S3) + tail):

| | Run 1 (1.7B, 4090) | Run 2 (14B, A100_SXM4) |
|---|---|---|
| S1 generate (331,200 rollouts, <=512 tok) | 4-9 h | 12-24 h |
| S2 activations (concurrent with S3) | ~40 min | 3-7 h |
| S3 judge 330k calls @20rps (GPU-free, overlapped) | 4.6 h | 4.6 h |
| S4+S5 + final upload + capture | ~1 h | ~2-4 h |
| Est wall | 6-12 h | 16-30 h |
| MAX_HOURS (est + ~50%, CLOUD.md §7) | 18 | 38 |
| GPU $ (typical / MAX_DOLLARS cap) | $2.5-6.5 / $11 | $17-32 / $60 |
| Egress $ (<=$0.004/GB hosts) | ~$0.2 | ~$0.7 |
| Judge $ (OPEN — §5.4) | $29-90 | $29-90 |

Knob if cost must shrink: `STAGE1_EXTRA='--question_count 120'` halves
S1/S3/storage for that run (orchestrator call; changes the vector estimate's
sample count per role from ~1200 to ~600).

## 3. Storage budget + bucket layout

3.1 **Per-rollout bytes (temp/41 §5, bf16, exact arithmetic)**: one
`(n_layers, hidden)` bf16 tensor per rollout x 1200 rollouts/role x 276
roles: **1.7B (28x2048) = 37.1GB; 14B (48x5120) = 159.1GB**. The pipeline
holds these bytes TWICE on disk (stage-2 `activations/*.pt` resume-state and
stage-4 `vectors/rollouts/*.safetensors`, torch.equal-proven identical).
Upstream-fp32 would double these (bluedot's 163GB@14B figure was fp32);
bf16 is what the pipeline saves — no cast step needed.

3.2 **What goes to the bucket** (prefix `axis/<MODEL_TAG>/`, i.e.
`axis/qwen3-1.7b/`, `axis/r1d-qwen-14b/`):
- `vectors/rollouts/{role}.safetensors` + `{role}.manifest.json` — THE
  per-rollout role-vector deliverable (276 files + manifests; scores,
  n_response_tokens, layer convention embedded)
- `responses/{role}.jsonl` (276), `scores/{role}.json` (275),
  `vectors/{role}.pt` (aggregates; may be <276 if roles fail min_count),
  `activations/{role}.tokens.json`, `axis.pt`, `env_fingerprint.json`,
  `stages.log`, `pusher.log`
- NOT uploaded: `activations/{role}.pt` — byte-duplicates of the rollouts
  safetensors (temp/41 §3.2); they still reach the byte-verified LOCAL
  capture (05) before any destroy, so nothing exists in fewer than 2 places.
- Bucket totals: run 1 ~40GB, run 2 ~165GB (drives precondition 1.3).

3.3 **Caveats**: bucket is PUBLIC until the user flips it (temp/42);
buckets are unversioned and mutable — the pusher and gate never pass
`--delete`; never `rm -R` a prefix. Artifact shape tag: rows are decoder
layers 0..L-1, NO embedding row (upstream convention) — bluedot's
`(n_layers+1)` hidden_states-indexed objects are a DIFFERENT convention;
never mix (temp/40 DO-THIS 12).

## 4. Session flow (exact commands)

All from `workspace/axis-run/runbook/`; every script needs
`AXIS_CONFIG=config.<model>.env` in front. State per model in
`.state/<tag>/`; a watchdog auto-starts at launch.

```
# 4.1 launch (read-only until the create; prints cost guard first)
AXIS_CONFIG=config.qwen3-1.7b.env YES=1 bash 01_launch.sh

# 4.2 provision + preflights (secrets stdin->0600; stack ASSERTED from the
#     prebuilt image vllm/vllm-openai:v0.13.0 — matches the aa uv.lock pin;
#     pinned pure-python extras only; REAL Gemini completion FROM the box)
AXIS_CONFIG=config.qwen3-1.7b.env bash 02_provision.sh

# 4.3 CANARY (mandatory on run 1; cheap insurance on run 2): 1 role x 2
#     questions through all 5 stages. vLLM stage 1 has never run anywhere
#     yet (temp/41 §6) and the vast+custom-image interaction is OPEN.
AXIS_CONFIG=config.qwen3-1.7b.env SLICE=1 bash 03_stages.sh

# 4.4 full run: resume-preseed from bucket, on-box 5-min verified pusher,
#     detached stage driver (S2 || S3), local monitor (Ctrl-C safe)
AXIS_CONFIG=config.qwen3-1.7b.env ACK_PUBLIC_BUCKET=1 bash 03_stages.sh

# 4.5 quiescent final upload through the xet gate (exit 0 or no marker)
AXIS_CONFIG=config.qwen3-1.7b.env bash 04_upload_final.sh

# 4.6 byte-verified full capture (sweep-epoch, quiescence, size+sha256)
AXIS_CONFIG=config.qwen3-1.7b.env bash 05_capture.sh

# 4.7 destroy — refuses without CAPTURE_OK + UPLOAD_OK both green
AXIS_CONFIG=config.qwen3-1.7b.env bash 06_destroy.sh \
  --marker .state/qwen3-1.7b/CAPTURE_OK_<id> --yes-i-am-really-sure

# then repeat 4.1-4.7 with AXIS_CONFIG=config.r1d-qwen-14b.env
# dud box that never provisioned:
#   bash 06_destroy.sh --never-provisioned --yes-i-am-really-sure
```

Failure rule (CLOUD.md law 1, binding): on ANY tooling/setup error,
web-search the exact error + official docs FIRST, log what was found in
temp/44, and only then iterate on the box (it bills while you debug).

Resume: all five stages skip existing per-role outputs; 03 pre-seeds
`$OUT_DIR` from the bucket (bluedot's 32B run resumed at 122/275 this way).
Because "exists" means "done" to the stages, the pre-seed restores ONLY
resume-safe artifacts: complete `responses/*.jsonl` (line count ==
5 x question_count; partial files are regenerated), `scores/*.json`
(stage-3 merges by key), and `*.tokens.json`. `activations/*.pt` are not in
the bucket and vectors/rollouts/axis are cheap CPU recomputes — so a dead
box costs the in-flight roles plus an S2 recompute pass (S2 is the price of
not uploading the 159GB duplicate store).

## 5. Monitoring, judge ops, abort criteria

5.1 **Logged where**: box — `$OUT_DIR/stages.log` (every `S<N>_RC=`,
`COUNTS`, `RUN_FAILED_*`), `$OUT_DIR/pusher.log` (`PUSH_CYCLE_RC=` each 5
min). Local — 03's monitor echo (3-min cadence, cost line each cycle),
`.state/<tag>/watchdog.log`, markers (`UPLOAD_OK_*`, `CAPTURE_OK_*`,
`DESTROY_LOG.txt`). `ALL_STAGES_RC=0` is printed only when every stage rc
is 0 — there is no ALL_DONE to false-trust.

5.2 **Watchdog** (per-model config): MAX_HOURS 18 (run 1) / 38 (run 2),
grace 60/90 min while 03's heartbeat is <20 min old; at the cap it runs
04 -> 05 -> 06 and destroys ONLY with both gates green; a failed gate leaves
the box alive and re-alerts every 5 min. It refreshes $/hr from the live
instance row (measured +6.9% over offer price in wave 3).

5.3 **Abort criteria**:
- Judge preflight fails on the box -> destroy `--never-provisioned`,
  blacklist offer id, next host (geo-block; do not debug it).
- `RUN_FAILED_S1` twice on the same host after the web-search step -> treat
  host/image pairing as bad; capture-if-provisioned, destroy, blacklist.
- S3 finishing with widespread unparsed scores (spot-check
  `scores/*.json`; the 3-pass loop should fill gaps) or >10% of roles
  failing min_count at S4 (R1 truncation risk, temp/41 §2) -> STOP before
  S5, record the score distribution in temp/44, escalate: the choices
  (lower `--min_count`, re-generate with higher max_tokens at ~3x S1 cost)
  change the science and are not this runbook's call.
- `/workspace` free space < 25GB during S2 -> stop stages, force a pusher
  cycle, escalate.
- Credit balance approaching $2 -> watchdog chain immediately.

5.4 **Gemini judge ops (MEASURED from this machine, 2026-09-04;
`tools/gemini_limits_measured.json`)**: 4 sequential tiny calls 0.64-0.84s
latency; 10-concurrent burst in 0.92s; 20-concurrent burst in 1.05s
(~19 req/s instantaneous); 38 calls total, ZERO 429s, no rate-limit headers
returned. 24 calls inside one minute exceeds the historical free-tier 15 RPM
-> the key behaves as PAID tier. Google no longer publishes per-tier numbers
(VERIFIED ai.google.dev/gemini-api/docs/rate-limits: per-project limits,
dashboard-only). Matching evidence: temp/41's 20-call burst, ~18 rps clean.
- Semaphore sizing: start `JUDGE_RPS=20, JUDGE_BATCH=20` (330k calls =
  4.6h). Sustained-load unknown (20 rps x ~850 tok ~= 1.0M tok/min): if a
  TPM ceiling bites, S3 429s/Nones early and the 3-pass resume loop
  absorbs it — then HALVE JUDGE_RPS. After >=30 clean minutes (zero 429,
  <1% unparsed) it MAY be raised to 50 (1.8h); never past 50 without a new
  measurement (bluedot's 200 rps was an OpenAI-tier number).
- Cost (OPEN): ~280M input tokens/model; $0.10/M if the
  `gemini-flash-lite-latest` alias serves 2.5 Flash-Lite ($29/model) up to
  $0.30/$2.50 if 3.5 Flash-Lite (~$90/model) — the OpenAI-compat response
  echoes the alias, not the target (temp/41). Resolve in the Google billing
  console after the canary; a concrete-version judge pin would make cost
  deterministic but changes the hub convention -> orchestrator.

## 6. Claims register

MEASURED (this session): offer tables §2 (commands + archives in
`.state/`/scratch); credit $10.11; local free disk 643GiB; Gemini 38-call
probe (§5.4); shellcheck/bash -n/py_compile clean on every script.
VERIFIED: workload + stage anatomy + storage arithmetic (temp/41, from
code); bucket gate semantics (temp/42, adversarially tested); Docker Hub
tag `vllm/vllm-openai:v0.13.0` exists (v2 API, 2026-09-04); Gemini rate
docs + flash-lite price points (ai.google.dev, 2026-09-04); vast incident
rules (temp/13/CLOUD.md).
OPEN: vast `--ssh` x vllm-openai-image entrypoint interaction (canary 4.3;
recovery: `--never-provisioned` + docs search); image CUDA build vs
`cuda_vers>=12.8` filter (02 fingerprints; adjust IMAGE_CUDA_MIN if the
canary shows a mismatch); aa deps beyond the three pinned extras (02's
import smoke; extend EXTRA_PIP_PINS from uv.lock only); sustained Gemini
TPM behavior; judge alias/cost; S1 wall-clock at 331k rollouts (estimates
only until the canary + run 1 measure it).
