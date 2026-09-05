#!/usr/bin/env bash
# 01_launch.sh — pick an offer from the ranked tier list, create ONE instance,
# wait for ssh (with key-reinjection recovery), arm the watchdog.
#
# ADAPTED from workspace/axis-run/runbook/01_launch.sh. Everything before the
# `vastai create` is read-only; the create is the only line that spends money,
# and the cost guard prints before it.
#
# Idempotent: refuses to launch if a live instance is already recorded, and
# treats an API error as ALIVE (never double-launches on a blip).
#
# Usage:
#   bash 01_launch.sh                      # interactive confirm
#   YES=1 bash 01_launch.sh                # no prompt
#   SKIP_OFFER_IDS=123,456 bash 01_launch.sh   # blacklist bad hosts
#   GPU_TIERS='H100_SXM:79:1.20:6.00' bash 01_launch.sh   # take an H100 at any price
set -euo pipefail
case "$-" in *x*) echo "FATAL: xtrace on in a secret-adjacent script"; exit 90;; esac
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_cmd vastai
require_cmd python3
require_resolved IMAGE "$IMAGE"
require_resolved GPU_TIERS "$GPU_TIERS"

CLI_VER="$(vastai --version 2>/dev/null | head -1)"
if [ "$CLI_VER" != "1.0.13" ]; then
  log "WARNING: vastai CLI is '$CLI_VER', this harness was validated against 1.0.13 —"
  log "WARNING: re-test instance_row pagination + create flags before trusting this run"
fi

# ---- ssh-key preflight BEFORE spending: is our pubkey on the account? ----
[ -f "$SSH_KEY.pub" ] || die "no public key at $SSH_KEY.pub"
LOCAL_KEY_MATERIAL="$(awk '{print $2}' "$SSH_KEY.pub")"
if vastai show ssh-keys --raw 2>/dev/null | grep -qF "$LOCAL_KEY_MATERIAL"; then
  log "ssh-key preflight OK: $SSH_KEY.pub is registered on the vast account"
else
  die "ssh-key preflight FAILED: register $SSH_KEY.pub first (a prior project lost 3 boxes to this)"
fi

# ---- credit preflight (read-only) ----------------------------------------
CREDIT="$(vastai show user --raw 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("credit",""))
except Exception: print("")')"
if [ -n "$CREDIT" ]; then
  log "account credit: \$${CREDIT} (this run MAX_DOLLARS=\$${MAX_DOLLARS})"
  if ! python3 -c "exit(0 if float('$CREDIT') >= float('$MAX_DOLLARS') else 1)"; then
    if [ "${SKIP_CREDIT_GUARD:-0}" = "1" ]; then
      log "WARNING: credit \$${CREDIT} < MAX_DOLLARS \$${MAX_DOLLARS} — proceeding on SKIP_CREDIT_GUARD=1"
    else
      die "credit \$${CREDIT} < MAX_DOLLARS \$${MAX_DOLLARS} — top up, or set SKIP_CREDIT_GUARD=1 if auto-top-up is on"
    fi
  fi
else
  log "WARNING: could not read credit; proceeding on the MAX_DOLLARS guard alone"
fi

# ---- idempotency: never double-launch (three-state: error != absent) -----
if [ -n "$(instance_id)" ]; then
  row="$(instance_row_retry "$(instance_id)")"
  case "$row" in
    row*)   die "instance $(instance_id) already exists ($(printf '%s' "$row" | cut -f2)); finish that session first" ;;
    error*) die "cannot verify instance $(instance_id) (API error persists) — ASSUMING ALIVE, refusing to launch" ;;
  esac
  log "stale instance_id $(instance_id) (absent after 3 clean page-walks) — archiving"
  mv "$STATE_DIR/instance_id" "$STATE_DIR/instance_id.stale.$(date -u +%s)"
fi

# ---- ranked tier search --------------------------------------------------
OFFER_LINE=""
CHOSEN_TIER=""
SEARCH_SUMMARY=""
for TIER in $GPU_TIERS; do
  GPU_NAME="${TIER%%:*}"; rest="${TIER#*:}"
  MIN_GPU_RAM="${rest%%:*}"; rest="${rest#*:}"
  PRICE_FLOOR="${rest%%:*}"; PRICE_CAP="${rest#*:}"
  # gpu_ram and cpu_ram in the DSL are GB (raw JSON is MB) — the 40/80GB trap.
  QUERY="gpu_name=${GPU_NAME} num_gpus=${NUM_GPUS} verified=true rentable=true \
gpu_ram>=${MIN_GPU_RAM} reliability>${MIN_RELIABILITY} inet_down>${MIN_INET_DOWN} \
inet_up>=${MIN_INET_UP} disk_space>=${DISK_GB} cpu_ram>${MIN_CPU_RAM_GB} \
cuda_vers>=${IMAGE_CUDA_MIN} direct_port_count>=${MIN_DIRECT_PORTS} \
dph_total<=${PRICE_CAP} ${GEO_FILTER}"
  log "tier $GPU_NAME (>=${MIN_GPU_RAM}GB VRAM, disk>=${DISK_GB}GB): searching [floor \$${PRICE_FLOOR}, cap \$${PRICE_CAP}]"
  OFFERS_JSON="$STATE_DIR/offers_${GPU_NAME}_$(date -u +%Y%m%dT%H%M%SZ).json"
  if ! vastai search offers "$QUERY" -o dph_total+ --limit 200 --raw > "$OFFERS_JSON"; then
    log "tier $GPU_NAME: search FAILED (CLI/API error) — trying next tier"
    SEARCH_SUMMARY="${SEARCH_SUMMARY}  $GPU_NAME: search error\n"
    continue
  fi
  N_RAW="$(python3 -c "import json;print(len(json.load(open('$OFFERS_JSON')) or []))")"
  log "tier $GPU_NAME: $N_RAW offers returned by the DSL query; re-checking every field client-side"

  # Client-side re-check of EVERY constraint. The DSL silently drops drifted
  # field names, and the raw JSON uses different units and field names than the
  # DSL. Offers travel via FILE, never a pipe into a heredoc (a pipe into
  # `python3 - <<EOF` is silently discarded — that bug once made every offer
  # search return "no offer").
  OFFER_LINE="$(MIN_RELIABILITY="$MIN_RELIABILITY" MIN_INET_DOWN="$MIN_INET_DOWN" \
    MIN_INET_UP="$MIN_INET_UP" PRICE_FLOOR="$PRICE_FLOOR" PRICE_CAP="$PRICE_CAP" \
    DISK_GB="$DISK_GB" MIN_CPU_RAM_MB="$MIN_CPU_RAM_MB" MAX_INET_COST="$MAX_INET_COST" \
    MIN_GPU_RAM="$MIN_GPU_RAM" IMAGE_CUDA_MIN="$IMAGE_CUDA_MIN" \
    MIN_DIRECT_PORTS="$MIN_DIRECT_PORTS" SKIP_OFFER_IDS="${SKIP_OFFER_IDS:-}" \
    python3 - "$OFFERS_JSON" <<'PY'
import json, os, sys
with open(sys.argv[1]) as fh:
    offers = json.load(fh) or []
E = os.environ
skip = set(int(x) for x in E.get("SKIP_OFFER_IDS", "").split(",") if x.strip())
mr = float(E["MIN_RELIABILITY"]); mi = float(E["MIN_INET_DOWN"]); mu = float(E["MIN_INET_UP"])
floor = float(E["PRICE_FLOOR"]); cap = float(E["PRICE_CAP"])
disk = float(E["DISK_GB"]); cpu_mb = float(E["MIN_CPU_RAM_MB"])
inet_cost = float(E["MAX_INET_COST"]); gram = float(E["MIN_GPU_RAM"])
cuda_min = float(E["IMAGE_CUDA_MIN"]); ports = float(E["MIN_DIRECT_PORTS"])

def reasons(o):
    r = []
    if o.get("id") in skip: r.append("blacklisted")
    if (o.get("cuda_max_good") or 0) < cuda_min: r.append("cuda_max_good=%s" % o.get("cuda_max_good"))
    rel = o.get("reliability2") or o.get("reliability") or 0
    if rel < mr: r.append("reliability=%.4f" % rel)
    if (o.get("inet_down") or 0) < mi: r.append("inet_down=%s" % o.get("inet_down"))
    if (o.get("inet_up") or 0) < mu: r.append("inet_up=%s" % o.get("inet_up"))
    d = o.get("dph_total")
    if d is None or not (floor <= d <= cap): r.append("dph_total=%s" % d)
    if (o.get("disk_space") or 0) < disk: r.append("disk_space=%.0f" % (o.get("disk_space") or 0))
    # raw JSON cpu_ram and gpu_ram are MB; the DSL is GB
    if (o.get("cpu_ram") or 0) < cpu_mb: r.append("cpu_ram_mb=%s" % o.get("cpu_ram"))
    if (o.get("gpu_ram") or 0) < gram * 1024: r.append("gpu_ram_mb=%s" % o.get("gpu_ram"))
    if (o.get("inet_down_cost") or 0) > inet_cost: r.append("inet_down_cost=%s" % o.get("inet_down_cost"))
    if (o.get("inet_up_cost") or 0) > inet_cost: r.append("inet_up_cost=%s" % o.get("inet_up_cost"))
    if (o.get("direct_port_count") or 0) < ports: r.append("direct_port_count=%s" % o.get("direct_port_count"))
    # raw JSON has no boolean `verified`; the field is verification == "verified"
    if not (o.get("verified") is True or o.get("verification") == "verified"): r.append("unverified")
    return r

good = [o for o in offers if not reasons(o)]
for o in offers[:12]:
    if reasons(o):
        print("REJECT %s $%.3f: %s" % (o.get("id"), o.get("dph_total") or 0,
              ", ".join(reasons(o))), file=sys.stderr)
if not good:
    sys.exit(0)
o = sorted(good, key=lambda x: x["dph_total"])[0]
print("%s\t%.4f\t%s\t%s\t%s\t%s\t%s" % (o["id"], o["dph_total"],
      o.get("reliability2") or o.get("reliability"), o.get("inet_down"),
      (o.get("geolocation") or "").strip(), o.get("cuda_max_good"),
      int(o.get("disk_space") or 0)))
PY
)"
  if [ -n "$OFFER_LINE" ]; then CHOSEN_TIER="$GPU_NAME"; break; fi
  log "tier $GPU_NAME: 0 of $N_RAW offers pass all client-side constraints (rejections above)"
  SEARCH_SUMMARY="${SEARCH_SUMMARY}  $GPU_NAME: $N_RAW returned, 0 passed (archive: $OFFERS_JSON)\n"
done

if [ -z "$OFFER_LINE" ]; then
  log "NO OFFER PASSES in any tier. Per-tier result:"
  printf '%b' "$SEARCH_SUMMARY" >&2
  die "no offer at DISK_GB=$DISK_GB with cuda_max_good>=$IMAGE_CUDA_MIN, reliability>$MIN_RELIABILITY, \
inet costs<=\$$MAX_INET_COST/GB in tiers [$GPU_TIERS]. DO NOT lower DISK_GB: it is the CLOUD.md \
rule 6*model_GB+250 applied to $MODEL_ID (bf16 weights are 29.5GB for DeepSeek-R1-Distill-Qwen-14B \
and 65.5GB for Qwen3-32B). Options: raise a tier's price cap, add a tier (H200/B200 both had \
passing offers on 2026-09-04), or re-run later — 80GB single-GPU supply is thin (n=5..9 measured)."
fi

OFFER_ID="$(printf '%s' "$OFFER_LINE" | cut -f1)"
DPH="$(printf '%s' "$OFFER_LINE" | cut -f2)"
log "chosen [$CHOSEN_TIER] offer $OFFER_ID: \$${DPH}/hr rel=$(printf '%s' "$OFFER_LINE" | cut -f3) \
inet_down=$(printf '%s' "$OFFER_LINE" | cut -f4)Mbps geo=$(printf '%s' "$OFFER_LINE" | cut -f5) \
cuda_max_good=$(printf '%s' "$OFFER_LINE" | cut -f6) disk=$(printf '%s' "$OFFER_LINE" | cut -f7)GB"

# ---- cost guard, printed BEFORE money is spent ---------------------------
EST="$(python3 -c "print(f'{$DPH*$MAX_HOURS:.2f}')")"
log "cost guard: \$${DPH}/hr x MAX_HOURS=${MAX_HOURS} = \$${EST} (cap \$${MAX_DOLLARS})"
python3 -c "exit(0 if $DPH*$MAX_HOURS <= $MAX_DOLLARS else 1)" \
  || die "estimated session cost \$${EST} exceeds MAX_DOLLARS=\$${MAX_DOLLARS}"

if [ "${YES:-0}" != "1" ]; then
  read -r -p ">> create instance from offer $OFFER_ID at \$${DPH}/hr for $WORKSTREAM? [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { log "aborted before spending"; exit 0; }
fi

# ---- create (the only line that spends) ----------------------------------
# Custom third-party image in --ssh mode: vast injects sshd; the image's own
# serving ENTRYPOINT is not what we run (02 starts vLLM explicitly). If the box
# shows a restart loop or no sshd inside the wait windows, treat it as a bad
# host/image pairing: destroy --never-provisioned, blacklist the offer, retry.
CREATE_JSON="$(vastai create instance "$OFFER_ID" \
  --image "$IMAGE" --disk "$DISK_GB" --ssh --direct \
  --label "$INSTANCE_LABEL" --raw)"
INSTANCE="$(printf '%s' "$CREATE_JSON" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("new_contract",""))
except Exception: print("")')"
if [ -z "$INSTANCE" ]; then
  # create may have SUCCEEDED with unparseable output — a billing box with no
  # local record. Recover by label before dying (CLOUD.md law 8).
  log "create output unparseable: $(redact "$CREATE_JSON")"
  log "searching all instance pages for label $INSTANCE_LABEL ..."
  FOUND="$(LABEL="$INSTANCE_LABEL" python3 - <<'PY'
import json, os, subprocess
label = os.environ["LABEL"]
token = None; hits = []
for _ in range(40):
    cmd = ["vastai", "show", "instances-v1", "--raw"]
    if token: cmd += ["--next-token", token]
    p = subprocess.run(cmd, capture_output=True, text=True)
    try: d = json.loads(p.stdout)
    except Exception: break
    rows = d if isinstance(d, list) else d.get("instances", d.get("results", []))
    hits += [str(r.get("id")) for r in rows if isinstance(r, dict)
             and (r.get("label") or "") == label]
    token = d.get("next_token") if isinstance(d, dict) else None
    if not token or not rows: break
print(" ".join(hits))
PY
)"
  if [ -n "$FOUND" ]; then
    echo "$FOUND" | tr ' ' '\n' | tail -1 > "$STATE_DIR/instance_id"
    die "create response unparseable but instance(s) labeled $INSTANCE_LABEL exist: $FOUND — \
recorded the newest; VERIFY by hand, then continue or destroy it"
  fi
  die "create failed and no labeled instance found — check 'vastai show instances-v1 --raw' by hand NOW"
fi
assert_not_foreign "$INSTANCE"

# Crash-safe: record BEFORE polling so a failure here never orphans a billing box.
echo "$INSTANCE" > "$STATE_DIR/instance_id"
date +%s        > "$STATE_DIR/launch_epoch"
echo "$DPH"     > "$STATE_DIR/dph"
echo "$OFFER_ID" > "$STATE_DIR/offer_id"
printf '%s  %s  MINE  %s  %s\n' "$INSTANCE" "$CHOSEN_TIER" "$WORKSTREAM" "$(date -u +%FT%TZ)" \
  >> "$STATE_DIR/MY_VAST_INSTANCES.txt"
log "created instance $INSTANCE (recorded + labeled $INSTANCE_LABEL)"

# ---- wait running (three-state status every poll) ------------------------
st=""
for i in $(seq 1 60); do
  row="$(instance_row "$INSTANCE")"
  case "$row" in
    error*)  st="api-error"; log "[$i] status: API error (transient, not treating as gone)" ;;
    absent)  st="absent";    log "[$i] status: not listed yet" ;;
    *)       st="$(printf '%s' "$row" | cut -f2)"; log "[$i] status: $st" ;;
  esac
  [ "$st" = "running" ] && break
  sleep 15
done
[ "$st" = "running" ] || die "instance $INSTANCE never reached running; it is BILLING — tear it down: \
bash 05_destroy.sh --never-provisioned --yes-i-am-really-sure"

# The OFFER price excludes the disk premium — re-read the ACTUAL live rate
# (measured +6.9% in a prior wave; CLOUD.md law 7).
ACTUAL_DPH="$(instance_row_retry "$INSTANCE" | cut -f5)"
if [ -n "$ACTUAL_DPH" ] && [ "$ACTUAL_DPH" != "$DPH" ]; then
  log "actual instance dph_total=\$$ACTUAL_DPH (offer said \$$DPH) — recording actual"
  echo "$ACTUAL_DPH" > "$STATE_DIR/dph"
  echo "$DPH" > "$STATE_DIR/dph_offer"
fi

# ---- ssh wait, with the key-reinjection recovery -------------------------
resolve_ssh_target
if ! wait_ssh; then
  log "sshd not up after the first window — forcing key re-injection (CLOUD.md §3 recovery)"
  vastai attach ssh "$INSTANCE" "$(cat "$SSH_KEY.pub")" || true
  if [ -f "${RECOVERY_SSH_KEY:-$HOME/.ssh/vast_recover}.pub" ]; then
    vastai attach ssh "$INSTANCE" "$(cat "${RECOVERY_SSH_KEY:-$HOME/.ssh/vast_recover}.pub")" || true
    log "also re-injected the recovery key ${RECOVERY_SSH_KEY:-$HOME/.ssh/vast_recover}.pub"
  fi
  resolve_ssh_target
  wait_ssh || die "sshd never came up on $INSTANCE after two windows — BAD HOST. Run: \
bash 05_destroy.sh --never-provisioned --yes-i-am-really-sure ; then relaunch with \
SKIP_OFFER_IDS=$OFFER_ID"
fi
assert_box_identity

# ---- watchdog: the hard money bound, armed the moment the box is ours ----
if [ "${NO_WATCHDOG:-0}" != "1" ]; then
  QC_CONFIG="$QC_CONFIG" nohup bash "$QC_HERE/watchdog.sh" \
    >> "$STATE_DIR/watchdog.log" 2>&1 &
  echo $! > "$STATE_DIR/watchdog_pid"
  log "watchdog armed (pid $(cat "$STATE_DIR/watchdog_pid"), log $STATE_DIR/watchdog.log)"
else
  log "NO_WATCHDOG=1 — run 'bash watchdog.sh' in another terminal NOW (an unenforced cap is decoration)"
fi

cost_status
log "next: bash 02_provision.sh"
