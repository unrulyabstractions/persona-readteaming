# CLOUD.md — self-contained vast.ai playbook (drop into any project)

Audience: an autonomous agent or human with NO other context. Follow this
top-to-bottom and you will not repeat any incident in the log at the bottom.
Everything here was measured or paid for on real instances (Sept 2026).
Reference scripts are inline; adapt paths, keep the logic.

Prerequisites: `vastai` CLI (`pipx install vastai` or `uv tool install
vastai`) with an API key configured (`vastai set api-key ...` — key value
never echoed), an ssh keypair registered (`vastai show ssh-keys`), `jq`,
`python3`, `rsync`.

---

## 0. The ten laws

1. **Research online first.** On ANY error: web-search the exact error string
   + the tool's official docs/issues BEFORE local trial-and-error. Log the
   search and the fix. Most "mysterious" failures are documented.
2. **Prebuilt images only.** Never pip-install a serving/ML stack on a
   billing box. Boot a pinned official image (e.g. `vllm/vllm-openai:v0.27.1`
   for vLLM ≥0.10.2 features like `return_token_ids`). Provisioning is then:
   download models, copy scripts. Match the image's CUDA requirement to the
   offer's `cuda_max_good` BEFORE creating the instance.
3. **No bottom-fishing.** Verified hosts, `reliability > 0.995`. Pay the
   $1–1.5/hr band for real work; the cheapest offers cost more in retries and
   dead hosts. For 14B+ models prefer A100_SXM4/H100; avoid PCIE
   power-capped hosts (a 14B job once ran 8h that should have run ~2h).
4. **Secrets never touch argv, logs, or xtrace.** Deliver via stdin to a 0600
   file or ssh-agent. Guard every script (snippet in §6). Never use
   `vastai create ... --env '-e KEY=...'` for secrets. No `set -x` in any
   file that could see env.
5. **Capture before destroy, byte-verified.** Full-filesystem manifest with a
   pre-stamped epoch, double sweep until stable, byte-size (+hash) compare,
   destroy REFUSES without the green marker. Scripts in §5. The only bypass
   is `--never-provisioned` (verified: your code never reached the box).
6. **Three-state instance truth**: `present | absent | api-error`. An API
   error is never "gone" (don't double-launch) and never "destroyed" (don't
   stop watching). Verify destroys by walking ALL pages of the v1 API.
7. **Watchdog from launch.** A local loop that force-runs capture→destroy at
   MAX_HOURS, reading the LIVE instance `dph_total` (measured ~+7% over the
   offer's dph). An unenforced budget cap is decoration.
8. **One box, one owner.** Always `--label <workstream>`; recover instance
   ids by label when CLI output parsing fails; never touch unlabeled or
   foreign instances.
9. **Verify uploads by content.** rc=0 from an uploader is NOT proof (four
   documented rc=0 false-successes, incl. a run that published zero files).
   Re-list remotely and compare per-file bytes/hashes; for HuggingFace
   buckets compare local `hf_xet.hash_files` vs server `xet_hash` per file;
   buckets are unversioned and may be public — check visibility first.
10. **Preflight cheap before spending.** ssh key registered? API endpoints
    reachable FROM the box with a REAL request (some hosts are geo-blocked:
    `/v1/models` succeeds, completions 403)? New image/stack gets one canary
    box before a campaign.

---

## 1. vast.ai facts you must know (all measured)

- **API v0 is dead**: `vastai show instances` → HTTP 410. Use
  `vastai show instances-v1 --raw` and WALK ALL PAGES; the JSON may be
  `{"instances": [...]}` or a bare list — handle both.
- **Instance types**: default instances are unprivileged Docker containers —
  **no docker-in-docker**. Nested Docker needs VM offers
  (`vms_enabled=true` in the query): consumer cards only in practice
  (measured: zero 80GB A100/H100 VM offers), KVM image
  (`docker.io/vastai/kvm`), SSH-only, slower boot.
- **Offer fields that matter**: `dph_total`, `reliability`, `cuda_max_good`
  (host driver's max CUDA — your image must not need more),
  `inet_down`/`inet_up` (Mbps) and `inet_down_cost`/`inet_up_cost` ($/GB —
  cap at ≤$0.02), `disk_space`, `cpu_ram` (MB, not GB), `gpu_name`,
  `verified`. Re-verify every filtered field client-side after search — CLI
  filter behavior drifts.
- **Actual price ≠ offer price**: live `dph_total` on the created instance
  ran +6.9% over the offer (disk premium). Read it from the instance row.
- **ssh key propagation is flaky**: a created box may refuse your key, or
  sshd may never start. `vastai attach ssh <id> "$(cat ~/.ssh/id_ed25519.pub)"`
  forces re-injection; probe with BOTH your default key and a recovery key.
  If sshd never appears within ~15 min total, the HOST is bad: destroy
  (`--never-provisioned`), blacklist the offer id, pick the next offer.
- **Marketplace flake is normal**: offers can fail to materialize into
  instances at all ($0 cost). Launch loops must tolerate 2–3 attempts;
  blacklist via a `SKIP_OFFER_IDS` list.
- **`--onstart-cmd`** exists (16KB limit) for boot-time commands;
  `--label` is supported and is your recovery handle.
- **uv + multi-index trap** (only relevant if you ignore law 2): the pytorch
  index carries stale copies of common packages; uv's first-index pinning
  then breaks resolution. Fix is uv's own `--index-strategy
  unsafe-best-match` — but the real fix is the prebuilt image.

---

## 2. Offer selection

```bash
# GPU_QUERY examples:
#  probe box : 'gpu_name=RTX_4090 num_gpus=1'
#  14B+ work : 'gpu_name=A100_SXM4 num_gpus=1'   (or H100_SXM)
vastai search offers --raw \
  "$GPU_QUERY reliability>0.995 verified=true cuda_max_good>=12.4 \
   inet_down>500 disk_space>$DISK_GB cpu_ram>32000 rentable=true" \
  -o 'dph_total' | jq -r '.[:15][] |
   "\(.id) $\(.dph_total|tostring[:5])/hr rel=\(.reliability2|tostring[:6]) \
cuda=\(.cuda_max_good) inet=\(.inet_down)Mbps disk=\(.disk_space)GB \
inetcost=\(.inet_down_cost)"'
```

Rules: `DISK_GB >= 6 * model_size_GB + 250`. Reject `inet_*_cost > 0.02`.
Client-side re-check every field on the chosen offer JSON; archive that JSON.
Skip ids in your blacklist. For docker-in-docker add `vms_enabled=true` and
accept consumer-GPU-only reality.

## 3. Launch (with recovery)

```bash
# create (prebuilt image; secrets NOT here)
vastai create instance "$OFFER_ID" \
  --image "$IMAGE" --disk "$DISK_GB" --label "$WORKSTREAM" \
  --ssh --direct --raw > create.json   # parse .new_contract as INSTANCE_ID
# If parse fails: recover by label:
#   vastai show instances-v1 --raw | jq '..|select(.label?=="'$WORKSTREAM'")|.id'
```

ssh-wait loop (max ~7 min, then recovery, then give up on the HOST):

```bash
deadline=$(( $(date +%s) + 420 ))
until ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
      -p "$SSH_PORT" "root@$SSH_HOST" true 2>/dev/null; do
  [ "$(date +%s)" -gt "$deadline" ] && {
    vastai attach ssh "$INSTANCE_ID" "$(cat ~/.ssh/id_ed25519.pub)"  # force re-inject
    deadline=$(( $(date +%s) + 480 )); RECOVERED=1
    [ "${GAVE_UP:-}" ] && break
    GAVE_UP=$RECOVERED
  }
  sleep 10
done
# Both windows exhausted => bad host: 05_destroy --never-provisioned,
# add OFFER_ID to SKIP_OFFER_IDS, relaunch with next offer.
```

Arm the watchdog THE MOMENT create returns (§7). Get ssh host/port from the
instance row (`ssh_host`, `ssh_port`), not from memory.

## 4. Provision + run

- Only: model downloads (`hf download <repo>`), script copy (rsync/scp),
  tiny extras (`pytest`) if truly needed. If you find yourself resolving
  torch/vLLM versions on the box, STOP — wrong image (law 2).
- Write `env_fingerprint.json` on the box: python/torch/cuda/driver/library
  versions + GPU name. Every result artifact carries or references it.
- Touch a provision marker file locally (e.g. `.state/provisioned_$ID`) —
  it switches destroy semantics from `--never-provisioned` to capture-gated.
- Long stages: write a status file per stage with its exit code; poll VRAM
  (`nvidia-smi`) instead of sleeping; overlap GPU-bound stages with
  API-bound stages (serial ordering bills idle GPUs for hours).
- Preflight any external API FROM the box with a real request before
  depending on it mid-run (geo-blocks pass shallow checks).
- Upload finished artifacts continuously (every ~5 min) through your
  verifier, so a dying box loses minutes, not hours. Resume-from-remote
  makes relaunches incremental.

## 5. Capture then destroy (the gate)

Capture (`04_capture.sh` essence):

```bash
SWEEP_EPOCH=$(date +%s)                      # stamp BEFORE the find (TOCTOU fix)
sweep() { ssh ... "find /root /workspace -xdev -type f \
   -not -path '*/site-packages/*' -not -path '*/.cache/huggingface/hub/models*' \
   -printf '%s\t%p\n' | sort" ; }
sweep > m1; sleep 3; sweep > m2
until cmp -s m1 m2; do mv m2 m1; sleep 5; sweep > m2; done   # stable manifest
rsync -az --files-from=<(cut -f2 m2) ... "$LOCAL_CAPTURE/"
# verify EVERY file: local byte size == manifest size; sha256 for files <100MB
python3 verify_capture.py m2 "$LOCAL_CAPTURE" || exit 2
date +%s > ".state/capture_ok_${INSTANCE_ID}"   # the green marker
```

Destroy (`05_destroy.sh` essence):

```bash
if [ ! -f ".state/capture_ok_${INSTANCE_ID}" ]; then
  if [ "$1" = "--never-provisioned" ] && [ ! -f ".state/provisioned_${INSTANCE_ID}" ]
  then :   # nothing of ours ever reached the box
  else echo "REFUSING: no verified capture"; exit 2; fi
fi
vastai destroy instance "$INSTANCE_ID"
# verify gone: walk ALL instances-v1 pages; api-error != gone (retry);
# log: id, files swept, files captured, files lost (MUST be 0), timestamp.
```

Excluded-from-sweep trees must be provably reproducible (package caches,
model hub caches) — never exclude anything your run WROTE.

## 6. Secret handling (put at top of every script)

```bash
set -euo pipefail
case "$-" in *x*) echo "FATAL: xtrace on in secret-adjacent script"; exit 90;; esac
set +x 2>/dev/null || true
# deliver a secret to the box: NEVER on a command line:
printf '%s' "$MY_TOKEN" | ssh ... 'umask 077 && cat > ~/.token'
log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$(sed -E 's/(hf_|sk-|key-)[A-Za-z0-9_-]+/[REDACTED]/g' <<<"$*")"; }
```

## 7. Watchdog (runs on YOUR machine, started at create)

```bash
( while sleep 300; do
    row=$(instance_row_retry "$INSTANCE_ID")            # 3-state: row|absent|error
    [ "$row" = absent ] && exit 0
    [ "$row" = error  ] && continue                     # never conclude on error
    dph=$(jq -r .dph_total <<<"$row")                   # LIVE price
    el_h=$(awk "BEGIN{print ($(date +%s)-$LAUNCH_EPOCH)/3600}")
    over_cap=$(awk "BEGIN{print ($el_h>$MAX_HOURS)?1:0}")
    heartbeat_fresh && continue                          # experiments still running
    [ "$over_cap" = 1 ] && { bash 04_capture.sh && bash 05_destroy.sh; exit 0; }
  done ) & echo $! > .state/watchdog_pid
```

MAX_HOURS from your measured stage timings + 50% margin. A failed capture
never auto-destroys — it alerts.

## 8. Money defaults

| Work | GPU | Cap |
|---|---|---|
| Probe/smoke | RTX 4090 (~$0.35–0.45/hr) | $4 / 3h |
| 1–8B serving/harvest | 4090/5090 or A100-40GB | $8 / 6h |
| 14B+ extraction | A100_SXM4-80GB / H100 ($1–2.9/hr measured) | timings + 50% |

Idle GPU time is the main waste: batch 20+ concurrent requests against a
server; overlap stages; never let a box sit while you debug locally.

## 9. Known failure modes → exact remedy

| Symptom | Cause | Do |
|---|---|---|
| Offer accepted, instance never appears | marketplace flake | retry next offer; $0 lost; blacklist not needed |
| Box `running`, ssh times out forever | dead host sshd | attach-ssh recovery once; then `--never-provisioned` destroy + blacklist offer id |
| `Connection closed by <ip>` on probe | container sshd not up yet | keep waiting inside window; it's the proxy accepting, backend not ready |
| pip/uv resolution errors on box | you violated law 2 | switch to prebuilt image; interim: uv's `--index-strategy unsafe-best-match` |
| CUDA "no kernel image" / wheel mismatch | image needs CUDA > host `cuda_max_good` | filter offers on `cuda_max_good` ≥ image requirement |
| `show instances` → 410 | v0 API removed | `show instances-v1 --raw`, walk pages |
| Upload "succeeded", remote empty/partial | rc=0 false success | content-verify per file (law 9); re-list independently |
| Destroy "confirmed" but billing continues | api-error read as absent | 3-state check; walk all pages until truly absent |
| Costs exceed estimate | offer dph ≠ live dph; idle time | read live `dph_total`; watchdog; overlap stages |
| Keys in logs | xtrace / argv / --env | §6 guard; stdin-only delivery; rotate the key NOW |

## 10. Incident log (provenance of every rule)

| Incident | Rule |
|---|---|
| API keys leaked via `set -x` into on-box launch logs | 4, §6 |
| 7 runs destroyed after summary-only rsync passed a file-exists gate | 5, §5 |
| 3 healthy boxes abandoned due to flaky ssh-key injection | §3 recovery |
| Run published ZERO files after ALL_DONE, rc=0 | 9 |
| A100_PCIE power cap: 14B stage ran 8h | 3 |
| Capture gate TOCTOU (files written between sweep and marker) | §5 epoch+double sweep |
| Launcher heredoc bug: every search returned "no offer" (caught by shellcheck) | static-check scripts before spend |
| Host sshd never came up; $0.10 lost | §3, `--never-provisioned` |
| cu129 wheel mismatch + uv index pin: 4 provision attempts | 1, 2 |
| Live dph +6.9% over offer dph | 7 |
