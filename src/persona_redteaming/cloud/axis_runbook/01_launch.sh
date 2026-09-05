#!/usr/bin/env bash
# 01_launch.sh — pick an offer by tiered query, create ONE instance, wait for ssh.
#
# Adapted from workspace/vast-harness/vast/01_launch.sh @0c418be. Changes:
# tiered GPU search with a PRICE FLOOR (CLOUD.md law 3: no bottom-fishing),
# cuda_max_good matched to the image in-query (law 2), per-model state.
#
# Idempotent: refuses to launch if a live instance is already recorded.
# Spends money once it creates; everything before the create is read-only.
#
# Usage:
#   AXIS_CONFIG=config.qwen3-1.7b.env bash 01_launch.sh          # interactive
#   AXIS_CONFIG=config.r1d-qwen-14b.env YES=1 bash 01_launch.sh
#   SKIP_OFFER_IDS=123,456 ... bash 01_launch.sh                 # blacklist
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_cmd vastai
require_cmd python3
require_resolved IMAGE "$IMAGE"

CLI_VER="$(vastai --version 2>/dev/null | head -1)"
if [ "$CLI_VER" != "1.0.13" ]; then
  log "WARNING: vastai CLI is '$CLI_VER', harness validated against 1.0.13 —"
  log "WARNING: re-test instance_row pagination + create flags before trusting this run"
fi

# ---- ssh-key preflight BEFORE spending: is our pubkey on the account? ----
[ -f "$SSH_KEY.pub" ] || die "no public key at $SSH_KEY.pub"
LOCAL_KEY_MATERIAL="$(awk '{print $2}' "$SSH_KEY.pub")"
if vastai show ssh-keys --raw 2>/dev/null | grep -qF "$LOCAL_KEY_MATERIAL"; then
  log "ssh-key preflight OK: $SSH_KEY.pub is registered on the vast account"
else
  die "ssh-key preflight FAILED: register $SSH_KEY.pub first (prior project lost 3 boxes to this)"
fi

# ---- credit preflight (read-only): refuse if MAX_DOLLARS exceeds balance -
CREDIT="$(vastai show user --raw 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("credit",""))
except Exception: print("")')"
if [ -n "$CREDIT" ]; then
  log "account credit: \$${CREDIT} (this run's MAX_DOLLARS=\$${MAX_DOLLARS})"
  if ! python3 -c "exit(0 if float('$CREDIT') >= float('$MAX_DOLLARS') else 1)"; then
    if [ "${SKIP_CREDIT_GUARD:-0}" = "1" ]; then
      # Run-2 fleet: orchestrator directive 2026-09-04 states auto-top-up is
      # ACTIVE and the credit floor is not a constraint. MAX_DOLLARS + the
      # watchdog remain the enforced money bound; credit is monitored each
      # cycle and 'credit ~$2 -> teardown chain' (README §5.3) stands.
      log "WARNING: credit \$${CREDIT} < MAX_DOLLARS \$${MAX_DOLLARS} — proceeding on SKIP_CREDIT_GUARD=1 (auto-top-up directive)"
    else
      die "credit \$${CREDIT} < MAX_DOLLARS \$${MAX_DOLLARS} — top up first (README §1.4)"
    fi
  fi
else
  log "WARNING: could not read credit; proceeding on MAX_DOLLARS guard alone"
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

# ---- tiered search -------------------------------------------------------
OFFER_LINE=""
CHOSEN_TIER=""
for TIER in $GPU_TIERS; do
  GPU_NAME="${TIER%%:*}"; rest="${TIER#*:}"
  MIN_GPU_RAM="${rest%%:*}"; rest="${rest#*:}"
  PRICE_FLOOR="${rest%%:*}"; PRICE_CAP="${rest#*:}"
  # gpu_ram in the DSL is GB (raw JSON is MB) — the A100 40/80GB trap.
  QUERY="gpu_name=${GPU_NAME} num_gpus=${NUM_GPUS} verified=true rentable=true \
gpu_ram>=${MIN_GPU_RAM} reliability>${MIN_RELIABILITY} inet_down>=${MIN_INET_DOWN} \
inet_up>=${MIN_INET_UP} disk_space>=${DISK_GB} cpu_ram>=${MIN_CPU_RAM_GB} \
cuda_vers>=${IMAGE_CUDA_MIN} direct_port_count>=1 dph_total<=${PRICE_CAP} ${GEO_FILTER}"
  log "tier $GPU_NAME: searching [floor \$${PRICE_FLOOR}, cap \$${PRICE_CAP}]"
  vastai search offers "$QUERY" -o dph_total+ --limit 200 --raw \
    > "$STATE_DIR/offers_${GPU_NAME}.json" || { log "search failed for $GPU_NAME"; continue; }

  # Client-side re-check of EVERY constraint (DSL silently ignores drifted
  # field names). Offers travel via file, not a pipe into a heredoc (the
  # SC2259 launcher bug: a pipe into `python3 - <<EOF` is silently discarded).
  OFFER_LINE="$(MIN_RELIABILITY="$MIN_RELIABILITY" MIN_INET_DOWN="$MIN_INET_DOWN" \
    MIN_INET_UP="$MIN_INET_UP" PRICE_FLOOR="$PRICE_FLOOR" PRICE_CAP="$PRICE_CAP" \
    DISK_GB="$DISK_GB" MIN_CPU_RAM_GB="$MIN_CPU_RAM_GB" MAX_INET_COST="$MAX_INET_COST" \
    MIN_GPU_RAM="$MIN_GPU_RAM" IMAGE_CUDA_MIN="$IMAGE_CUDA_MIN" \
    SKIP_OFFER_IDS="${SKIP_OFFER_IDS:-}" \
    python3 - "$STATE_DIR/offers_${GPU_NAME}.json" <<'PY'
import json, os, sys
with open(sys.argv[1]) as fh:
    offers = json.load(fh) or []
E = os.environ
skip = set(int(x) for x in E.get("SKIP_OFFER_IDS", "").split(",") if x.strip())
mr = float(E["MIN_RELIABILITY"]); mi = float(E["MIN_INET_DOWN"])
mu = float(E["MIN_INET_UP"])
floor = float(E["PRICE_FLOOR"]); cap = float(E["PRICE_CAP"])
disk = float(E["DISK_GB"]); cpu_gb = float(E["MIN_CPU_RAM_GB"])
inet_cost = float(E["MAX_INET_COST"]); gram = float(E["MIN_GPU_RAM"])
cuda_min = float(E["IMAGE_CUDA_MIN"])
def ok(o):
    return (o.get("id") not in skip
        and (o.get("cuda_max_good") or 0) >= cuda_min
        and (o.get("reliability2") or o.get("reliability") or 0) >= mr
        and (o.get("inet_down") or 0) >= mi
        and (o.get("inet_up") or 0) >= mu
        # NO BOTTOM-FISHING (CLOUD.md law 3): floor as well as cap
        and floor <= (o.get("dph_total") or 9e9) <= cap
        and (o.get("disk_space") or 0) >= disk
        # raw JSON cpu_ram and gpu_ram are MB (the DSL is GB)
        and (o.get("cpu_ram") or 0) >= cpu_gb * 1024
        and (o.get("gpu_ram") or 0) >= gram * 1024
        and (o.get("inet_up_cost") or 0) <= inet_cost
        and (o.get("inet_down_cost") or 0) <= inet_cost
        and (o.get("direct_port_count") or 0) >= 1
        # raw JSON has no boolean `verified`; the field is
        # `verification: "verified"` (MEASURED 2026-09-04 on live offers —
        # the DSL/JSON field-name drift trap, again)
        and (o.get("verified") is True or o.get("verification") == "verified"))
good = [o for o in offers if ok(o)]
if not good:
    sys.exit(0)
o = sorted(good, key=lambda x: x["dph_total"])[0]
print("%s\t%.4f\t%s\t%s\t%s\t%s" % (o["id"], o["dph_total"],
      o.get("reliability2") or o.get("reliability"), o.get("inet_down"),
      (o.get("geolocation") or "").strip(), o.get("cuda_max_good")))
PY
)"
  if [ -n "$OFFER_LINE" ]; then CHOSEN_TIER="$GPU_NAME"; break; fi
  log "tier $GPU_NAME: no offer passes all constraints; trying next tier"
done
[ -n "$OFFER_LINE" ] || die "no offer in any tier passes; re-run the market search and revisit GPU_TIERS"

OFFER_ID="$(printf '%s' "$OFFER_LINE" | cut -f1)"
DPH="$(printf '%s' "$OFFER_LINE" | cut -f2)"
log "chosen [$CHOSEN_TIER] offer $OFFER_ID: \$${DPH}/hr rel=$(printf '%s' "$OFFER_LINE" | cut -f3) \
inet_down=$(printf '%s' "$OFFER_LINE" | cut -f4)Mbps geo=$(printf '%s' "$OFFER_LINE" | cut -f5) \
cuda_max_good=$(printf '%s' "$OFFER_LINE" | cut -f6)"

# ---- cost guard, printed BEFORE money is spent ---------------------------
EST="$(python3 -c "print(f'{$DPH*$MAX_HOURS:.2f}')")"
log "cost guard: \$${DPH}/hr x MAX_HOURS=${MAX_HOURS} = \$${EST} (cap \$${MAX_DOLLARS})"
python3 -c "exit(0 if $DPH*$MAX_HOURS <= $MAX_DOLLARS else 1)" \
  || die "estimated session cost \$${EST} exceeds MAX_DOLLARS=\$${MAX_DOLLARS}"

if [ "${YES:-0}" != "1" ]; then
  read -r -p ">> create instance from offer $OFFER_ID at \$${DPH}/hr for $MODEL_TAG? [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { log "aborted before spending"; exit 0; }
fi

# ---- create --------------------------------------------------------------
# Custom third-party image (vllm/vllm-openai) in --ssh mode: vast injects
# sshd; the image's own serving ENTRYPOINT is not what we run (stages call
# python directly over ssh). If the created box shows a restart loop or no
# sshd within the wait window, treat as bad host/image interaction: destroy
# --never-provisioned, blacklist offer, and per the web-search-first rule
# check vast docs/templates for an entrypoint override before retrying.
CREATE_JSON="$(vastai create instance "$OFFER_ID" \
  --image "$IMAGE" --disk "$DISK_GB" --ssh --direct \
  --label "$INSTANCE_LABEL" --raw)"
INSTANCE="$(printf '%s' "$CREATE_JSON" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("new_contract",""))
except Exception: print("")')"
if [ -z "$INSTANCE" ]; then
  # create may have SUCCEEDED with unparseable output — a billing box with
  # no local record. Recover by label before dying.
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
recorded the newest; VERIFY by hand, then continue or 06-destroy it"
  fi
  die "create failed and no labeled instance found — check 'vastai show instances-v1' by hand NOW"
fi
assert_ours "$INSTANCE"

# Crash-safe: record BEFORE polling so a failure here never orphans a billing box.
echo "$INSTANCE" > "$STATE_DIR/instance_id"
date +%s        > "$STATE_DIR/launch_epoch"
echo "$DPH"     > "$STATE_DIR/dph"
printf '%s  %s  MINE  axis-run-%s  %s\n' "$INSTANCE" "$CHOSEN_TIER" "$MODEL_TAG" "$(date -u +%FT%TZ)" \
  >> "$STATE_DIR/MY_VAST_INSTANCES.txt"
log "created instance $INSTANCE (recorded + labeled $INSTANCE_LABEL)"

# ---- wait running, then wait ssh (three-state status every poll) ---------
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
[ "$st" = "running" ] || die "instance $INSTANCE never reached running; it is billing — \
tear it down: AXIS_CONFIG=$AXIS_CONFIG bash 06_destroy.sh --never-provisioned --yes-i-am-really-sure"

# The OFFER price excludes the disk premium — re-read the ACTUAL live rate
# (measured +6.9% in wave 3; CLOUD.md law 7).
ACTUAL_DPH="$(instance_row_retry "$INSTANCE" | cut -f5)"
if [ -n "$ACTUAL_DPH" ] && [ "$ACTUAL_DPH" != "$DPH" ]; then
  log "actual instance dph_total=\$$ACTUAL_DPH (offer said \$$DPH) — recording actual"
  echo "$ACTUAL_DPH" > "$STATE_DIR/dph"
  echo "$DPH" > "$STATE_DIR/dph_offer"
fi

resolve_ssh_target
if ! wait_ssh; then
  log "sshd not up after first window — forcing key re-injection (CLOUD.md §3 recovery)"
  vastai attach ssh "$INSTANCE" "$(cat "$SSH_KEY.pub")" || true
  wait_ssh || die "sshd never came up on $INSTANCE; destroy via 06_destroy.sh \
--never-provisioned, add offer $OFFER_ID to SKIP_OFFER_IDS, and try the next offer"
fi

# ---- watchdog: hard money bound, upload+capture-gated --------------------
if [ "${NO_WATCHDOG:-0}" != "1" ]; then
  AXIS_CONFIG="$AXIS_CONFIG" nohup bash "$AXIS_LIB_HERE/watchdog.sh" \
    >> "$STATE_DIR/watchdog.log" 2>&1 &
  echo $! > "$STATE_DIR/watchdog_pid"
  log "watchdog started (pid $(cat "$STATE_DIR/watchdog_pid"), log $STATE_DIR/watchdog.log)"
else
  log "NO_WATCHDOG=1 — run 'AXIS_CONFIG=$AXIS_CONFIG bash watchdog.sh' yourself NOW"
fi

cost_status
log "next: AXIS_CONFIG=$AXIS_CONFIG bash 02_provision.sh"
