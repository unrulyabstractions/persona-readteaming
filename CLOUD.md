# CLOUD.md — binding rules for all cloud/GPU work in this project

Every rule below was paid for with a real incident (ours or bluedot's).
Agents and humans follow this document for ANY rented compute. The vast
harness (`workspace/vast-harness/vast/`, v3+) is the enforcement layer;
if a rule here has no script enforcing it yet, add the script before the run.

## The laws (non-negotiable)

1. **Research online first.** On ANY error: web-search the exact error string
   and check the tool's official docs/issues BEFORE local trial-and-error.
   Log what you searched and what fixed it.
   (Incident: 3 pip provision attempts burned on a uv multi-index quirk whose
   fix was in uv's own error text and docs.)
2. **Prebuilt images only.** Never pip-resolve a serving/training stack on a
   billing box. Use pinned official images (`vllm/vllm-openai:<pin>`, pipeline
   images) as the instance template; provisioning = model download + script
   copy only. Match the image/wheel CUDA variant to the offer's
   `cuda_max_good` field BEFORE launch.
   (Incident: cu129/cu126 wheel mismatch + stale `packaging` on the pytorch
   index → 4 provision attempts.)
3. **No bottom-fishing.** For anything beyond a throwaway probe, buy
   reliability: verified/datacenter-class hosts, rel ≥ 0.995, the $1–1.5/hr
   band before considering cheaper. Retries and wall-clock cost more than the
   savings. A100_SXM4 first for 14B-class; never PCIE-power-capped hosts.
   (Incidents: two offers never materialized; one host's sshd never came up;
   bluedot's PCIE power-cap run took 8h for a 14B stage.)
4. **Secrets never touch argv, logs, or xtrace.** Keys travel via stdin to
   0600 files or ssh-agent only. Every script that could touch a secret runs
   the anti-xtrace guard and the redacting logger. No `set -x` anywhere near
   env. Never pass secrets via `vastai create --env`.
   (Incident: bluedot leaked HF/OpenAI keys via `set -x` into on-box logs and
   SSH argv.)
5. **Capture before destroy, byte-verified, always.** No destroy while any
   uncaptured byte exists. The sweep: stamp `SWEEP_EPOCH` BEFORE the find;
   full-filesystem manifest (`find ... -printf '%s\t%p\n'`) over every root
   the work touched incl. home dirs; sha256 for files <100MB, size for
   larger; re-sweep until two consecutive manifests are identical; compare
   BYTES against local copies, never row counts. `05_destroy` refuses without
   the green capture marker. The only exception is `--never-provisioned`
   (verified: no provision marker = nothing of ours on the box).
   (Incidents: bluedot destroyed 7 runs after a summary-only rsync passed a
   file-exists gate; our own v2 gate had a TOCTOU a critic exploited.)
6. **Three-state instance truth.** API answers are `present | absent | error`
   — an API/network error is NEVER "gone" (don't double-launch) and NEVER
   "destroyed" (don't stop watching a billing box). Verify destroys by
   walking ALL pages of `instances-v1`.
7. **Watchdog armed from launch.** A local background loop with heartbeat
   gating that force-runs capture→destroy at MAX_HOURS. Cost guard uses the
   LIVE instance `dph_total` (measured +7% over offer dph), not the offer.
   A "$ cap" that no process enforces is decoration.
8. **One box, one owner.** Every instance carries a `--label` naming its
   workstream; agents never touch instances they don't own. Recovery from CLI
   parse failures goes through the label, not guesswork.
9. **Verify uploads by content, not success codes.** HF classic repos: per-file
   size vs `get_paths_info`, 12-attempt retry, background partial pusher,
   resume-from-remote idempotence. HF buckets: `hf buckets sync` +
   independent re-list + local `hf_xet.hash_files` vs server `xet_hash` for
   EVERY file (`workspace/axis-run/hf/hf_upload_verified.py`). Buckets are
   unversioned (deletes final) and may be PUBLIC — check visibility before
   real artifacts go up. rc=0 is not proof; bluedot logged four rc=0
   false-successes, including one run that published ZERO files after
   ALL_DONE.
10. **Preflight everything cheap before anything expensive.** SSH keys vs
    `vastai show ssh-keys` before create; judge/API endpoints from ON the box
    with a REAL completion (geo-blocked hosts pass `/v1/models` then 403 on
    completions); template pin re-check (`round1.py pin`) on launch day;
    canary-provision a new image/stack once before committing a campaign to it.

## Per-phase checklist

**Choose platform.** Modal for env containers (repo-native, CPU-pinned
calibrations depend on it). vast.ai for GPU serving/harvest. Docker-in-docker
on vast needs VM offers (`vms_enabled=true`) — consumer cards only, zero
80GB VM offers existed on 2026-09-04; 30B+ campaigns therefore split shape
(envs elsewhere, GPU box serves vLLM only).

**Select offer.** Query must pin: GPU model + VRAM, rel ≥ 0.995,
`cuda_max_good` ≥ image requirement, `inet_down` ≥ 500 Mbps, disk ≥
6×model_GB + 250, inet cost ≤ $0.02/GB, CPU RAM ≥ 32GB. Re-check every field
client-side after the search (CLI output drifts). Archive the offer JSON.
Keep `SKIP_OFFER_IDS` for known-bad hosts.

**Launch.** `--label`, `--onstart-cmd` from a template, prebuilt image.
ssh-wait with BOTH keys and the `vastai attach ssh` recovery path; a host
whose sshd never appears within the window gets `--never-provisioned`
destroy and its offer id added to SKIP_OFFER_IDS. Watchdog starts the moment
create returns, not after provisioning.

**Provision.** Models + scripts only (law 2). Write `env_fingerprint.json`
(python/torch/cuda/driver/library versions) — every result JSON carries it.
Provision marker gates the capture/destroy semantics.

**Run.** Stage-RC gating; overlap GPU stages with API-bound stages (bluedot
billed idle GPUs for hours running them serially); status files per probe;
VRAM-poll instead of sleeps; continuous background upload of finished
artifacts through the verify gate so a dying box loses minutes, not hours.

**Capture & destroy.** Law 5, then law 6 verification, then log to
VERIFICATION_LOG.md: instance id, files swept, files captured, files lost
(must be 0), destroy timestamp.

## Money defaults

Probe-class session: 4090, cap $4 / 3h. Extraction campaign: A100_SXM4-80GB
band $1–1.5/hr, cap set from the measured stage timings + 50% margin, never
uncapped. Expected-vs-actual dph logged per run (D4). Idle-GPU time is the
enemy: batch rollouts 20+ concurrent, overlap stages.

## Incident log (why these rules exist)

| Date | Incident | Rule |
|---|---|---|
| 2026-07-29 (bluedot) | `set -x` leaked API keys into launch logs | 4 |
| 2026-08 (bluedot) | 7 runs destroyed after summary-only rsync passed gate | 5 |
| 2026-08 (bluedot) | 3 healthy boxes lost to flaky ssh-key injection | 10, launch recovery path |
| 2026-08 (bluedot) | ALL_DONE run published zero files, rc=0 | 9 |
| 2026-08 (bluedot) | A100_PCIE power cap: 14B stage 8h | 3 |
| 2026-09-04 | Capture gate TOCTOU constructed by critic | 5 (sweep-epoch-first, double sweep) |
| 2026-09-04 | Offer heredoc bug: every launch would find "no offer" (shellcheck) | static-check before spend |
| 2026-09-04 | Host sshd never came up; $0.10 lost | 3, 10, `--never-provisioned` |
| 2026-09-04 | cu129 wheel mismatch + uv index pin: 4 provision attempts | 1, 2 |
| 2026-09-04 | Actual dph +6.9% over offer dph | 7 |
