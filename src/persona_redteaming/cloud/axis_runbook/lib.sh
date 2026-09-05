#!/usr/bin/env bash
# lib.sh — shared helpers for the axis-run session scripts. Sourced, not executed.
#
# VENDORED from workspace/vast-harness/vast/lib.sh @0c418be (harness v3,
# post-CRITIC-ENG). Changes for axis-run:
#   - config comes from $AXIS_CONFIG (per-model env file); no default.
#   - state is per model: .state/<MODEL_TAG>/ (two runs may overlap).
#   - FOREIGN_INSTANCES guard: this workstream must never touch workstream 1's
#     box (49841102) or any instance not labeled $INSTANCE_LABEL.
#   - capture excludes extended for the prebuilt-image layout.
#
# SECRET POLICY (hard rules, learned from the bluedot key leak):
#  - No `set -x` anywhere. The guard below turns tracing OFF if a caller
#    enabled it (xtrace expands and logs secret values — that is exactly how a
#    prior project leaked an HF token into on-box logs).
#  - Keys are referenced by env var NAME only. redact() scrubs values from any
#    line we echo. Keys never appear in argv (ssh delivers them via stdin).
case $- in *x*) set +x ;; esac

AXIS_LIB_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -n "${AXIS_CONFIG:-}" ] || {
  echo "FATAL: set AXIS_CONFIG=config.<model>.env (see runbook/README.md)" >&2
  exit 1
}
# shellcheck disable=SC1090  # path is user-provided by design
. "$AXIS_LIB_HERE/$(basename "$AXIS_CONFIG")"

# Refuse to run with unresolved [41-PENDING] placeholders in critical knobs.
require_resolved() {
  local name="$1" val="$2"
  case "$val" in
    *PENDING*|"") echo "FATAL: $name is unresolved ('$val') — fill it from temp/41-axis-repo-study.md first" >&2; exit 1 ;;
  esac
}

STATE_DIR="$AXIS_LIB_HERE/.state/$MODEL_TAG"
# shellcheck disable=SC2034  # CAPTURE_DIR is consumed by 05_capture.sh after sourcing
CAPTURE_DIR="$AXIS_LIB_HERE/capture/$MODEL_TAG"
mkdir -p "$STATE_DIR"

SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
# Vast reuses proxy endpoints across boxes with different host keys; a
# persistent known_hosts guarantees MITM-warning hangs. Throwaway GPU boxes:
# discard host keys entirely (prior fleet-killer, documented in bluedot).
SSH_OPTS="-F /dev/null -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no \
-o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=8 -o BatchMode=yes"

log() { printf '[%s %s %s] %s\n' "$(basename "$0" .sh)" "${MODEL_TAG:-?}" "$(date -u +%H:%M:%S)" "$(redact "$*")"; }
die() { log "FATAL: $*"; exit 1; }

# Scrub anything that looks like KEY=value, a bearer token, or a JSON-shaped
# credential from a line before it can reach a terminal or log.
redact() {
  printf '%s' "$*" | sed -E \
    -e 's/([A-Za-z_]*(KEY|TOKEN|SECRET)[A-Za-z_]*=)[^ ]+/\1<redacted>/g' \
    -e 's/(Authorization: *Bearer +)[^ "]+/\1<redacted>/g' \
    -e 's/("[A-Za-z_]*([Kk]ey|[Tt]oken|[Ss]ecret)[A-Za-z_]*" *: *")[^"]+/\1<redacted>/g'
}

# Exclusions shared by 05's manifest sweep and 06's freshness check (the two
# lists MUST be identical or the gates disagree). ONLY provably-reproducible
# trees belong here. NOTE: $OUT_DIR (responses/activations/scores/vectors/axis)
# is deliberately NOT excluded — per-rollout activations are a deliverable.
# $UPLOAD_DIR IS excluded: it is a pure hardlink mirror of $OUT_DIR built by
# rsync --link-dest (reproducible by construction, and its content is what
# the bucket gate already content-hash-verified) — sweeping it would double
# the pull.
capture_excludes_find() {
  printf '%s' "-not -path '$REMOTE_HF_HOME/*' -not -path '$UPLOAD_DIR/*' \
 -not -path '/root/.cache/*' -not -path '/root/.local/share/uv/*' \
 -not -path '/root/.local/bin/*' -not -path '/var/lib/apt/*' \
 -not -path '/root/.nv/*' -not -path '/root/.triton/*' \
 -not -path '/root/.vast_containerlabel*' \
 -not -path '$REMOTE_AA/.venv/*' -not -path '$REMOTE_AA/.git/*'"
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

# ---- instance state ------------------------------------------------------
instance_id() { cat "$STATE_DIR/instance_id" 2>/dev/null; }

# Workstream isolation (hub rule): 49841102 is workstream 1's wave-3 box.
FOREIGN_INSTANCES="49841102"
assert_ours() {
  local iid="$1" f
  for f in $FOREIGN_INSTANCES; do
    [ "$iid" = "$f" ] && die "instance $iid belongs to ANOTHER WORKSTREAM — refusing to touch it"
  done
  return 0
}

require_instance() {
  INSTANCE="${INSTANCE:-$(instance_id)}"
  [ -n "$INSTANCE" ] || die "no instance id (run 01_launch.sh, or set INSTANCE=)"
  assert_ours "$INSTANCE"
}

# instances-v1 paginates at 25/page. ANY per-instance lookup must walk every
# page or a box on page 2 reads as gone (this destroyed healthy boxes in the
# prior project). THREE-STATE output (an API blip must never read as
# "absent", which double-launched and false-verified destroys):
#   "row\t<status>\t<ssh_host>\t<ssh_port>\t<dph_total>"  found
#   "absent"                                              walked ALL pages, not found
#   "error\t<reason>"                                     could not determine
instance_row() {
  local iid="$1"
  IID="$iid" python3 - <<'PY'
import json, os, subprocess, sys
iid = int(os.environ["IID"])
token = None
for _ in range(40):  # hard page cap so a runaway token loop can never hang
    cmd = ["vastai", "show", "instances-v1", "--raw"]
    if token:
        cmd += ["--next-token", token]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        print("error\tvastai rc=%d stderr=%s" % (p.returncode, p.stderr.strip()[:120]))
        sys.exit(0)
    try:
        d = json.loads(p.stdout)
    except Exception as e:
        print("error\tjson parse: %r" % (e,)); sys.exit(0)
    rows = d if isinstance(d, list) else d.get("instances", d.get("results", []))
    for r in rows:
        if isinstance(r, dict) and r.get("id") == iid:
            print("row\t%s\t%s\t%s\t%s" % (
                r.get("actual_status") or r.get("cur_state") or "unknown",
                r.get("ssh_host") or "", r.get("ssh_port") or "",
                r.get("dph_total") or ""))
            sys.exit(0)
    token = d.get("next_token") if isinstance(d, dict) else None
    if not token or not rows:
        break
print("absent")
PY
}

# Retry wrapper: only returns "absent" after N clean (non-error) walks agree;
# transient API errors are retried, and a persistent error is returned as
# "error" for the caller to handle explicitly.
instance_row_retry() {
  local iid="$1" tries="${2:-3}" row="" i
  for i in $(seq 1 "$tries"); do
    row="$(instance_row "$iid")"
    case "$row" in error*) log "instance_row error (try $i/$tries): $(printf '%s' "$row" | cut -f2-)"; sleep 5 ;;
                   *) printf '%s' "$row"; return 0 ;;
    esac
  done
  printf '%s' "$row"
}

resolve_ssh_target() {  # sets SSH_HOST, SSH_PORT for $INSTANCE
  # Per-instance override (shared-proxy-endpoint incident 2026-09-04): when
  # the API hands two instances one proxy endpoint, pin the DIRECT endpoint
  # (public_ipaddr:direct_port_start) in $STATE_DIR/ssh_override as host:port.
  if [ -f "$STATE_DIR/ssh_override" ]; then
    SSH_HOST="$(cut -d: -f1 "$STATE_DIR/ssh_override")"
    SSH_PORT="$(cut -d: -f2 "$STATE_DIR/ssh_override")"
    log "ssh override in effect: $SSH_HOST:$SSH_PORT (direct endpoint)"
    return 0
  fi
  local row
  row="$(instance_row_retry "$INSTANCE")"
  case "$row" in
    error*) die "cannot resolve instance $INSTANCE: API error persists ($(printf '%s' "$row" | cut -f2-))" ;;
    absent) die "instance $INSTANCE not listed (walked all pages, 3 clean reads)" ;;
  esac
  SSH_HOST="$(printf '%s' "$row" | cut -f3)"
  SSH_PORT="$(printf '%s' "$row" | cut -f4)"
  [ -n "$SSH_HOST" ] && [ -n "$SSH_PORT" ] || die "instance $INSTANCE has no ssh endpoint yet"
}

vssh() {  # vssh "<command>"  — run one command on the box; stdin passes through
  # shellcheck disable=SC2086  # SSH_OPTS word-splitting is deliberate
  ssh $SSH_OPTS -i "$SSH_KEY" -p "$SSH_PORT" "root@$SSH_HOST" "$@"
}

# The vast API can hand two instances the SAME proxy endpoint while one is
# still loading (MEASURED 2026-09-04: w7 49859018 and w8 49859019 both got
# ssh2.vast.ai:19018; two 02_provision runs interleaved on ONE box and
# corrupted /root/.axis_env_common). Every session that writes to or pulls
# from a box must first prove the endpoint routes to THIS instance: vast
# writes /root/.vast_containerlabel = "C.<instance_id>" on every box.
assert_box_identity() {
  local want="C.$INSTANCE" got
  got="$(vssh 'cat /root/.vast_containerlabel 2>/dev/null' </dev/null 2>/dev/null | tr -d '[:space:]')" || true
  if [ -z "$got" ]; then
    log "WARNING: cannot read /root/.vast_containerlabel — box identity UNVERIFIED"
    return 0
  fi
  [ "$got" = "$want" ] || die "BOX IDENTITY MISMATCH: endpoint $SSH_HOST:$SSH_PORT is $got, expected $want — \
stale/shared proxy endpoint (re-resolve later; do NOT touch this box)"
  log "box identity OK: $got at $SSH_HOST:$SSH_PORT"
}

wait_ssh() {
  local i
  for i in $(seq 1 40); do
    if vssh true </dev/null 2>/dev/null; then log "sshd up at $SSH_HOST:$SSH_PORT"; return 0; fi
    sleep 10
  done
  return 1
}

# ---- cost guard ----------------------------------------------------------
# 01_launch writes launch_epoch + dph. Elapsed cost is computed, never guessed.
cost_status() {
  local t0 dph now hrs cost
  t0="$(cat "$STATE_DIR/launch_epoch" 2>/dev/null)" || true
  dph="$(cat "$STATE_DIR/dph" 2>/dev/null)" || true
  [ -n "$t0" ] && [ -n "$dph" ] || { echo "cost: unknown (no launch record)"; return 0; }
  now=$(date +%s)
  hrs=$(python3 -c "print(f'{($now-$t0)/3600:.2f}')")
  cost=$(python3 -c "print(f'{($now-$t0)/3600*$dph:.2f}')")
  echo "cost: ${hrs}h elapsed at \$${dph}/hr = \$${cost} (caps: ${MAX_HOURS}h / \$${MAX_DOLLARS})"
}

cost_guard_or_die() {  # refuse to START new work past the cap; never blocks upload/capture/destroy
  local t0 now over
  t0="$(cat "$STATE_DIR/launch_epoch" 2>/dev/null)" || true
  [ -n "$t0" ] || return 0
  now=$(date +%s)
  over=$(python3 -c "print(1 if ($now-$t0)/3600 > $MAX_HOURS else 0)")
  if [ "$over" = "1" ]; then
    cost_status
    die "past MAX_HOURS=$MAX_HOURS — run 04_upload_final + 05_capture + 06_destroy, do not start new work"
  fi
}
