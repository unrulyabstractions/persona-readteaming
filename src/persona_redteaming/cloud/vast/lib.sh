#!/usr/bin/env bash
# lib.sh — shared helpers for one vast.ai vLLM campaign. The campaign's
# identity lives in the QC_CONFIG .env file ($WORKSTREAM, $INSTANCE_LABEL);
# this file is shared by every campaign. Sourced, never executed.
#
# ADAPTED from workspace/axis-run/runbook/lib.sh (battle-tested on a real
# launch/capture/destroy cycle). Every safety property is preserved:
#   - three-state instance truth (present | absent | api-error)
#   - all-pages pagination on instances-v1 (a box on page 2 is not "gone")
#   - label-based ownership, plus a hard foreign-instance blacklist
#   - box-identity assertion (shared proxy endpoints hand two boxes one port)
#   - secrets by NAME only; redact() before anything reaches a terminal
# Changes for this campaign are listed in README.md §8.
#
# SECRET POLICY (hard):
#   - No `set -x` anywhere. The guard below turns tracing OFF if a caller
#     enabled it (xtrace expands and logs secret VALUES).
#   - Keys are referenced by env var NAME only. Keys never appear in argv;
#     ssh delivers them via stdin to a 0600 file.
case $- in *x*) set +x ;; esac

QC_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# QC_CONFIG names the campaign: a path to a .env file, or a bare name looked
# up as configs/<name>.env. There is no default; every campaign is explicit.
[ -n "${QC_CONFIG:-}" ] || {
  echo "FATAL: set QC_CONFIG=<path.env | name> (presets: $(ls "$QC_HERE/configs" 2>/dev/null | tr '\n' ' '))" >&2
  exit 1
}
case "$QC_CONFIG" in
  */*|*.env) QC_CONFIG_PATH="$QC_CONFIG" ;;
  *)         QC_CONFIG_PATH="$QC_HERE/configs/$QC_CONFIG.env" ;;
esac
[ -f "$QC_CONFIG_PATH" ] || {
  echo "FATAL: config '$QC_CONFIG' not found at $QC_CONFIG_PATH (see README.md §2)" >&2
  exit 1
}
# shellcheck disable=SC1090  # path is user-selectable by design (QC_CONFIG)
. "$QC_CONFIG_PATH"

# State and captures live NEXT TO THE CONFIG FILE, never inside the package,
# so one script set drives many campaigns and each keeps its own .state/.
QC_CAMPAIGN_DIR="$(cd "$(dirname "$QC_CONFIG_PATH")" && pwd)"
STATE_DIR="${STATE_DIR:-$QC_CAMPAIGN_DIR/.state/$WORKSTREAM}"
# shellcheck disable=SC2034  # consumed by 04_capture.sh after sourcing
# Overridable: point CAPTURE_DIR at a bigger volume when the pull does not fit.
CAPTURE_DIR="${CAPTURE_DIR:-$QC_CAMPAIGN_DIR/capture/$WORKSTREAM}"
mkdir -p "$STATE_DIR"

SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
# Vast reuses proxy endpoints across boxes with different host keys; a
# persistent known_hosts guarantees MITM-warning hangs on throwaway GPU boxes.
SSH_OPTS="-F /dev/null -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no \
-o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=8 -o BatchMode=yes"

# Scrub anything that looks like KEY=value, a bearer token, or a JSON-shaped
# credential from a line before it can reach a terminal or a log file.
redact() {
  printf '%s' "$*" | sed -E \
    -e 's/([A-Za-z_]*(KEY|TOKEN|SECRET)[A-Za-z_]*=)[^ ]+/\1<redacted>/g' \
    -e 's/(Authorization: *Bearer +)[^ "]+/\1<redacted>/g' \
    -e 's/(hf_|sk-|key-|AIza)[A-Za-z0-9_-]{8,}/<redacted>/g' \
    -e 's/("[A-Za-z_]*([Kk]ey|[Tt]oken|[Ss]ecret)[A-Za-z_]*" *: *")[^"]+/\1<redacted>/g'
}

log() { printf '[%s %s %s] %s\n' "$(basename "$0" .sh)" "${WORKSTREAM:-?}" \
        "$(date -u +%H:%M:%S)" "$(redact "$*")"; }
die() { log "FATAL: $*"; exit 1; }

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

require_resolved() {
  local name="$1" val="$2"
  case "$val" in
    *PENDING*|"") die "$name is unresolved ('$val') — fill it in $QC_CONFIG first" ;;
  esac
}

# ---- capture exclusions --------------------------------------------------
# ONLY provably-reproducible trees. 04's manifest sweep and 05's freshness
# check MUST use the identical list or the two gates disagree (that is how a
# TOCTOU hole opens). NOTHING THE RUN WROTE MAY APPEAR HERE.
#
#   1. $REMOTE_HF_HOME/*                    HF hub cache (re-downloadable weights)
#   2. */.cache/huggingface/hub/models*     default-location HF hub model cache
#   3. */site-packages/*                    installed packages (image + pip)
#   4. */.cache/pip/*  */.cache/uv/*        package download caches
#      */.local/share/uv/*
capture_excludes_find() {
  printf '%s' "-not -path '$REMOTE_HF_HOME/*' \
 -not -path '*/.cache/huggingface/hub/models*' \
 -not -path '*/site-packages/*' \
 -not -path '*/.cache/pip/*' \
 -not -path '*/.cache/uv/*' \
 -not -path '*/.local/share/uv/*'"
}

# Human-readable form of the SAME list, printed by 04 so the exclusions are
# auditable in the capture log (CLOUD.md law 5: exclusions must be provable).
capture_excludes_print() {
  cat <<EOF
capture exclusions (ONLY provably-reproducible trees; audit this list):
  1. $REMOTE_HF_HOME/*                  HF hub cache for \$MODEL_ID (re-downloadable)
  2. */.cache/huggingface/hub/models*   HF hub model cache at its default location
  3. */site-packages/*                  python packages (image-provided + pip)
  4. */.cache/pip/*                     pip download cache
  5. */.cache/uv/*                      uv download cache
  6. */.local/share/uv/*                uv tool/venv store
NOT excluded (deliberately swept): $REMOTE_PKG, $REMOTE_AIE,
  /workspace/*.log, /workspace/env_fingerprint.json, /root/*
EOF
}

# ---- instance state ------------------------------------------------------
instance_id() { cat "$STATE_DIR/instance_id" 2>/dev/null; }

# HARD workstream isolation: we refuse on id AND on label. Both lists come from
# config.env and there is NO fallback. A stale built-in short list would silently
# stop refusing the boxes it does not name, which is the failure this campaign
# cannot afford: two live campaigns share this account.
[ -n "${FOREIGN_INSTANCES:-}" ] \
  || die "FOREIGN_INSTANCES is unset or empty — set it in $QC_CONFIG to every instance id this campaign must never touch"
[ -n "${FOREIGN_LABELS:-}" ] \
  || die "FOREIGN_LABELS is unset or empty — set it in $QC_CONFIG to every foreign workstream label (matched as a PREFIX)"

assert_not_foreign() {
  local iid="$1" f
  for f in $FOREIGN_INSTANCES; do
    [ "$iid" = "$f" ] && die "instance $iid is on the foreign-instance blacklist — refusing to touch it"
  done
  return 0
}

# Given a "row\t..." line from instance_row, refuse unless the label is ours.
# An empty label is refused too: an unlabeled box is never ours to touch.
# FOREIGN_LABELS entries match as a PREFIX, because the live axis-run fleet
# labels its boxes axis-run-14b-w1 .. w8, not plain "axis-run".
assert_row_label() {
  local row="$1" iid="$2" lbl
  case "$row" in row*) ;; *) return 0 ;; esac
  lbl="$(printf '%s' "$row" | cut -f6)"
  local f
  for f in $FOREIGN_LABELS; do
    case "$lbl" in
      "$f"*) die "instance $iid is labeled '$lbl' (workstream '$f') — refusing to touch it" ;;
    esac
  done
  [ "$lbl" = "$INSTANCE_LABEL" ] \
    || die "instance $iid is labeled '$lbl', expected '$INSTANCE_LABEL' — refusing to touch it"
  return 0
}

require_instance() {
  INSTANCE="${INSTANCE:-$(instance_id)}"
  [ -n "$INSTANCE" ] || die "no instance id (run 01_launch.sh, or set INSTANCE=)"
  assert_not_foreign "$INSTANCE"
}

# instances-v1 paginates. ANY per-instance lookup must walk EVERY page or a box
# on page 2 reads as gone (that destroyed healthy boxes in a prior project).
# THREE-STATE output — an API blip must never read as "absent" (that
# double-launches) and never as "destroyed" (that stops the watchdog):
#   "row\t<status>\t<ssh_host>\t<ssh_port>\t<dph_total>\t<label>"  found
#   "absent"                                                       walked ALL pages
#   "error\t<reason>"                                              undetermined
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
            print("row\t%s\t%s\t%s\t%s\t%s" % (
                r.get("actual_status") or r.get("cur_state") or "unknown",
                r.get("ssh_host") or "", r.get("ssh_port") or "",
                r.get("dph_total") or "", r.get("label") or ""))
            sys.exit(0)
    token = d.get("next_token") if isinstance(d, dict) else None
    if not token or not rows:
        break
print("absent")
PY
}

# Only returns "absent" after a clean (non-error) walk; transient API errors are
# retried, and a persistent error is returned as "error" for explicit handling.
instance_row_retry() {
  local iid="$1" tries="${2:-3}" row="" i
  for i in $(seq 1 "$tries"); do
    row="$(instance_row "$iid")"
    case "$row" in
      error*) log "instance_row error (try $i/$tries): $(printf '%s' "$row" | cut -f2-)"; sleep 5 ;;
      *) printf '%s' "$row"; return 0 ;;
    esac
  done
  printf '%s' "$row"
}

resolve_ssh_target() {  # sets SSH_HOST, SSH_PORT for $INSTANCE; asserts the label
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
  assert_row_label "$row" "$INSTANCE"
  SSH_HOST="$(printf '%s' "$row" | cut -f3)"
  SSH_PORT="$(printf '%s' "$row" | cut -f4)"
  [ -n "$SSH_HOST" ] && [ -n "$SSH_PORT" ] || die "instance $INSTANCE has no ssh endpoint yet"
}

vssh() {  # vssh "<command>" — run one command on the box; stdin passes through
  # shellcheck disable=SC2086  # SSH_OPTS word-splitting is deliberate
  ssh $SSH_OPTS -i "$SSH_KEY" -p "$SSH_PORT" "root@$SSH_HOST" "$@"
}

# The vast API can hand two instances the SAME proxy endpoint while one is
# still loading (two provision runs once interleaved on ONE box). Vast writes
# /root/.vast_containerlabel = "C.<instance_id>" on every box: prove the
# endpoint routes to THIS instance before writing to it or pulling from it.
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
  for i in $(seq 1 "${SSH_WAIT_POLLS:-40}"); do
    if vssh true </dev/null 2>/dev/null; then log "sshd up at $SSH_HOST:$SSH_PORT"; return 0; fi
    sleep 10
  done
  return 1
}

# ---- cost guard ----------------------------------------------------------
# 01_launch writes launch_epoch + dph; the watchdog refreshes dph from the LIVE
# instance row (measured +6.9% over the offer price). Cost is computed, never
# guessed.
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

# Refuse to START new work past the cap. Never blocks capture or destroy.
cost_guard_or_die() {
  local t0 dph now over
  t0="$(cat "$STATE_DIR/launch_epoch" 2>/dev/null)" || true
  [ -n "$t0" ] || return 0
  dph="$(cat "$STATE_DIR/dph" 2>/dev/null || echo 0)"
  now=$(date +%s)
  over=$(python3 -c "
el=($now-$t0)/3600
print(1 if (el > $MAX_HOURS or el*$dph > $MAX_DOLLARS) else 0)")
  if [ "$over" = "1" ]; then
    cost_status
    die "past MAX_HOURS=$MAX_HOURS or MAX_DOLLARS=$MAX_DOLLARS — run 04_capture.sh then 05_destroy.sh, do not start new work"
  fi
}
