# Verification log — AXIS-RUNBOOK (runbook/ scope)

## 2026-09-04 (authoring session)

- WHAT: vast.ai market prices for the four GPU tiers (A100_SXM4-80GB,
  H100-class, 48GB-class, RTX_4090). HOW: ran `vastai search offers ...
  --raw` (read-only) at 09:41 UTC, computed stats with a local script, and
  re-filtered the saved JSON client-side at the runbook's quality bar; raw
  JSON archived in the session scratchpad, headline rows copied into
  README.md §2 and temp/43. RESULT: VERIFIED (MEASURED).
- WHAT: account credit $10.11. HOW: `vastai show user --raw` parsed for
  `credit`. RESULT: VERIFIED (MEASURED).
- WHAT: Gemini flash-lite judge behavior. HOW: ran
  `tools/measure_gemini_limits.py` twice from this machine (4 sequential +
  10-burst, then 4 sequential + 20-burst; 38 tiny calls total; key read from
  env, never printed) and re-opened `tools/gemini_limits_measured.json`:
  all statuses 200, zero 429, burst walls 0.92s/1.05s, no rate-limit
  headers. RESULT: VERIFIED (MEASURED); sustained TPM behavior remains OPEN.
- WHAT: Gemini docs facts (per-project limits, dashboard-only tiers;
  flash-lite paid $0.10/M in $0.40/M out for 2.5). HOW: fetched
  ai.google.dev rate-limits and pricing pages. RESULT: VERIFIED (docs);
  `-latest` alias target OPEN (temp/41 §5 price spread carried into README).
- WHAT: Docker Hub tag `vllm/vllm-openai:v0.13.0` exists (matches the
  assistant-axis uv.lock vllm pin). HOW: Docker Hub v2 tags API fetch:
  last_updated 2025-12-19, amd64 8.9GB. RESULT: VERIFIED. The image's
  internal torch/transformers versions are asserted by 02 on the box — OPEN
  until the canary.
- WHAT: local capture headroom. HOW: `df -h` on this machine — 643GiB free
  vs ~350GB worst-case 14B capture. RESULT: VERIFIED (MEASURED).
- WHAT: every runbook script is syntactically sound and secret-safe. HOW:
  `bash -n` on 11 shell scripts (all pass), `python3 -m py_compile` on both
  python tools (pass), `shellcheck -S warning -x` on all shell scripts
  (CLEAN; one SC2034 waived inline — CAPTURE_DIR is consumed by
  05_capture.sh after sourcing), grep confirms no `set -x` and a
  key-pattern scan over runbook/ returns zero hits. RESULT: VERIFIED
  (static only).
- WHAT: runtime behavior of 01-06, watchdog, remote scripts, and the
  vast+vllm-image entrypoint interaction. HOW: not executed (no instance
  may be created by this agent). RESULT: UNVERIFIED — first execution is
  the run-1 canary (README §4.3), which is itself a gate.
- WHAT: self-review iteration. HOW: line-by-line pass against the CLOUD.md
  laws + temp/13 do-better list; found and fixed 4 real defects (capture
  sweep would have double-pulled the hardlink staging tree; HF_TOKEN
  checked only after secrets push; gate-env smoke failed without a marker;
  resume pre-seed could restore partially-pushed per-role files that stages
  would then treat as done). RESULT: VERIFIED (fixes re-checked by the
  static suite above).

## 2026-09-04 RUN2-EXEC fleet session (temp/45-run2-exec.md)

- WHAT: submodule code for run 2 is the optimization merge. HOW: git in
  submodules/assistant-axis — branch persona-redteaming, HEAD 3bb4ef1,
  `merge-base --is-ancestor 3bb4ef1 HEAD` passes; only untracked
  benchmarks/slice/ (regenerable). RESULT: VERIFIED (MEASURED).
- WHAT: S1/S2 auto-data-parallel across GPUs at tp=1 (multi-GPU boxes need
  no code changes). HOW: read pipeline/1_generate.py (num_workers =
  total_gpus // tensor_parallel_size, roles split per worker, per-worker
  CUDA_VISIBLE_DEVICES) and 2_activations.py (same pattern, lines 307-360).
  RESULT: VERIFIED from source; CUDA runtime leg UNVERIFIED until canary.
- WHAT: TX single A100 offer 47239991 fails the egress-cost cap. HOW: raw
  offer JSON re-opened — inet_up_cost = inet_down_cost = $0.0390625/GB >
  $0.02 cap. RESULT: VERIFIED (MEASURED); offer rejected, not blacklisted.
- WHAT: fleet mix v2 offers pass every client-side constraint. HOW: full
  re-sweep 12:47-12:49 UTC with the 01_launch filter logic incl. inet
  costs; archived per-tier JSONs in /tmp/fs_*.json + .state offer archives.
  RESULT: VERIFIED at sweep time (marketplace drifts).
- WHAT: fleet scripts (03b_fleet.sh, SKIP_S5/SLICE_ROLES in run_stages.sh,
  meta-staging in pusher/04, SKIP_CREDIT_GUARD in 01). HOW: bash -n on all
  edited scripts (pass); backward-compat for run-1 configs preserved by
  guarding every new behavior on fleet-only vars (SKIP_S5, SLICE_ROLES,
  SKIP_CREDIT_GUARD default off); 03_stages.sh/watchdog.sh deliberately
  NOT edited (run-1 has live executors of those files). RESULT: VERIFIED
  static; runtime pending canary.
- WHAT: five fleet instances created (49859016/17/18/19/20) with correct
  labels and live dph recorded. HOW: 01_launch logs + instance_id state
  files re-read; per-box dph read from live instance rows (+2.9-12% over
  offer). RESULT: VERIFIED (MEASURED); ssh-up pending.
- WHAT: auto-top-up is real (basis for SKIP_CREDIT_GUARD=1). HOW:
  `vastai show user --raw` — autobill_amount=10.0, autobill_threshold=5.0,
  balance_threshold_enabled=True, has_billing=True. RESULT: VERIFIED
  (MEASURED): account auto-charges $10 whenever credit < $5. Note: $10
  increments vs ~$13/hr fleet burn = top-up every ~45 min; a failed charge
  stops instances -> credit monitored every cycle regardless.
- WHAT: run-2 w1 canary (4xA100, 8-role slice). HOW: watched 03b foreground
  output; S1..S5 RC=0; COUNTS 8/8/7/6/8/axis=1; artifacts re-opened ON THE
  BOX (axis.pt (48,5120) bf16 finite; aberration.safetensors 10x(48,5120)
  bf16; manifest keys; scores 10/10 parsed ints). Pooled CUDA S1 leg
  VERIFIED via "Pooled generation for 2 roles: [...]" x4 (one per GPU
  worker). RESULT: VERIFIED.
- WHAT: run-2 w3 canary (1xAda 48GB, 2-role slice) — KV fit for 14B bf16
  on the 48GB tier. HOW: 03b foreground output; all stages RC=0, COUNTS
  2/2/1/2/2/axis=1. RESULT: VERIFIED.
- WHAT: S2 defaults to single-worker spanning all GPUs on multi-GPU boxes.
  HOW: canary log "Single-worker mode: Using 4 GPU(s)"; 2_activations.py
  source (tp default None -> all GPUs). RESULT: VERIFIED; fixed by passing
  --tensor_parallel_size 1 in run_stages.sh (data-parallel S2).
- WHAT: judge cost projection for run 2. HOW: 5 roles x 4 REAL
  gemini-flash-lite-latest calls from w1 with exact stage-3 prompts
  (remote/judge_cost_probe.py); usage read from API responses: mean_in
  852-868, mean_out 1.0. 330k calls => $28.3 (2.5-flash-lite pricing) /
  $85.8 (3.5). RESULT: MEASURED; alias band OPEN (billing console only).
- WHAT: first durable run-2 data (w1 pool 1, 64 roles). HOW: on-box count
  `ls responses/*.jsonl` = 64; sample aberration.jsonl re-opened and
  PARSED: 1200 JSON rows, keys {conversation,label,prompt_index,question,
  question_index,system_prompt}; conversation is a 3-message list
  (system/user/assistant); assistant content over all 1200 rows: mean 2567
  chars, min 1507, ZERO empty. Independent bucket re-list at 13:54: 48
  responses objects / 182,483,296 bytes (pusher mid-catch-up vs 64 on box).
  RESULT: VERIFIED (content, not just existence).
- WHAT: my own false alarm on that check. HOW: a first probe reported
  "nonempty_responses 0" because it read a `response` key that does not
  exist in this schema; the generations live in conversation[-1]. Re-probed
  against the real structure before reporting. RESULT: probe defect, NOT a
  data defect — recorded so the number is not later misread as a finding.
- WHAT: w7 (49859018) full stop-order chain — the CONTROLLED live test of
  04b before any other box was chained. HOW, each step read directly:
  (a) box identity C.49859018 asserted at the shared proxy endpoint;
  (b) WRITERS_STOPPED, pusher exited after ~145 s, then a positive check
      that NO pipeline/pusher/judge process remained;
  (c) xet gate: "VERIFIED: 19 files, 50,500,095 bytes (all sizes + all xet
      content hashes)", FINAL_GATE_RC=0, counts responses=13;
  (d) capture: sweep_epoch stamped first, quiescence after 2 identical
      passes, manifest 627 files, "627 files size-checked, 627
      content-hashed", #bad=0;
  (e) MY OWN independent recount of the local capture: 627 files,
      58,326,298 bytes; 13 responses jsonl at EXACTLY 1200 lines each =
      15,600 rollouts; sample swarm.jsonl parsed, assistant content 2537
      chars;
  (f) destroy: "VERIFIED gone (all pages walked)", and my own separate
      instances-v1 walk confirms 49859018 absent, 7 boxes remain.
  RESULT: VERIFIED end to end. Files lost: 0.
- WHAT: "EXTRA ON REMOTE" warnings during w7's gate (scores/consultant.json,
  contrarian, coordinator, coral_reef, cosmopolitan, counselor, wanderer
  journals). HOW: cross-checked against fleet_shards.json — every name
  belongs to ANOTHER worker's shard on the shared prefix. RESULT: expected
  and correct under --allow-extra (append-style shared prefix), NOT a defect.
- WHAT: w8 freeze at the S1 boundary. HOW: on-box check — resp=25,
  driver_alive=no, stage_procs=0, activations=0 (proving S2 never started),
  pusher_alive=yes. RESULT: VERIFIED; the freeze does exactly what it claims.
- WHAT: w7/w8 MONITOR_FATAL alerts. HOW: read the 03b monitor tail — "stage
  driver is NOT running and no STAGES_DONE exists" at 14:04:19, i.e. the
  watcher correctly reacting to 04b/freeze deliberately killing the driver.
  RESULT: benign by design, no effect on box or data.
