#!/usr/bin/env bash
# 04_capture.sh — byte-verified full-filesystem sweep of the box. THE gate.
# ADAPTED from workspace/axis-run/runbook/05_capture.sh.
#
# 1. SWEEP_EPOCH is stamped BEFORE the find. The freshness reference must
#    PREDATE the sweep, or a file created between the manifest and the marker
#    is invisible to both this manifest and 05's freshness check (that TOCTOU
#    hole is why the epoch is pre-stamped).
# 2. Manifest = `find <roots> -type f -printf '%s\t%p\n'` over EVERYTHING under
#    /root, /workspace and the box's $HOME, with NO extension filter, minus
#    only provably-reproducible trees (the shared list in lib.sh, PRINTED here
#    so the exclusions are auditable). The sweep re-runs until two CONSECUTIVE
#    sweeps are identical, so an active writer cannot slip a file in mid-sweep.
# 3. rsync every manifest path locally, with retries.
# 4. Compare local vs manifest: byte SIZE for EVERY file, plus sha256 CONTENT
#    hash for every file under SHA256_MAX_BYTES. Any miss => exit non-zero and
#    NO marker. The box stays ALIVE. Data outranks money.
# 5. Only with zero files lost, write $STATE_DIR/CAPTURE_OK_<instance>, which
#    carries the sweep epoch. 05_destroy.sh refuses without it.
#
# Idempotent: re-running re-verifies everything; rsync only moves deltas.
set -euo pipefail
case "$-" in *x*) echo "FATAL: xtrace on in a secret-adjacent script"; exit 90;; esac
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_cmd rsync
require_cmd python3
require_instance
resolve_ssh_target
assert_box_identity

SHA256_MAX_BYTES="${SHA256_MAX_BYTES:-100000000}"   # 100MB
SWEEP_MAX_TRIES="${SWEEP_MAX_TRIES:-6}"
STOP_PIDFILES="${STOP_PIDFILES:-/workspace/vllm.pid}"
WRITER_PIDFILES="${WRITER_PIDFILES:-/workspace/campaign.pid /workspace/driver.pid /workspace/harvest.pid}"

# ---- 0. quiesce the box --------------------------------------------------
# A live vLLM server appends to vllm.log forever, so the quiescence sweep would
# never stabilise and capture would deadlock while the box bills. Capture is
# the last step before destroy, so stopping the server here is correct.
# KEEP_SERVER=1 skips it (then you must stop the writers yourself).
for pf in $WRITER_PIDFILES; do
  if vssh "[ -f '$pf' ] && kill -0 \$(cat '$pf') 2>/dev/null" </dev/null 2>/dev/null; then
    die "a campaign writer is still running on the box ($pf) — stop it, then re-run 04"
  fi
done
if [ "${KEEP_SERVER:-0}" != "1" ]; then
  for pf in $STOP_PIDFILES; do
    if vssh "[ -f '$pf' ] && kill -0 \$(cat '$pf') 2>/dev/null" </dev/null 2>/dev/null; then
      log "stopping the server recorded in $pf (SIGTERM, then SIGKILL after 30s)"
      vssh "p=\$(cat '$pf'); kill -TERM \$p 2>/dev/null || true; \
for i in \$(seq 1 30); do kill -0 \$p 2>/dev/null || break; sleep 1; done; \
kill -0 \$p 2>/dev/null && kill -KILL \$p 2>/dev/null || true; true" </dev/null
    fi
  done
  sleep 3
else
  log "KEEP_SERVER=1 — not stopping anything; the sweep will refuse if the box is still writing"
fi

# ---- 1. sweep roots (no extension filter, ever) --------------------------
# shellcheck disable=SC2016  # $HOME must expand on the REMOTE shell, not here
REMOTE_HOME="$(vssh 'printf %s "$HOME"' </dev/null 2>/dev/null | tr -d '[:space:]')"
SWEEP_ROOTS="$CAPTURE_ROOTS"
if [ -n "$REMOTE_HOME" ]; then
  covered=0
  for r in $SWEEP_ROOTS; do
    case "$REMOTE_HOME/" in "$r"/*) covered=1 ;; esac
  done
  if [ "$covered" = "0" ]; then
    SWEEP_ROOTS="$SWEEP_ROOTS $REMOTE_HOME"
    log "box \$HOME=$REMOTE_HOME is outside $CAPTURE_ROOTS — added to the sweep roots"
  else
    log "box \$HOME=$REMOTE_HOME is already covered by $CAPTURE_ROOTS"
  fi
else
  log "WARNING: could not read the box's \$HOME; sweeping $SWEEP_ROOTS only"
fi

DEST="$CAPTURE_DIR/$INSTANCE"
mkdir -p "$DEST/files"
MANIFEST="$DEST/remote_manifest.tsv"
EXCLUDES_FIND="$(capture_excludes_find)"

log "sweep roots: $SWEEP_ROOTS"
capture_excludes_print | while IFS= read -r l; do log "$l"; done

# ---- 2. quiescent manifest, epoch stamped FIRST --------------------------
SWEEP_EPOCH=$(date +%s)
log "1/5 sweep_epoch=$SWEEP_EPOCH (stamped BEFORE the find); building the manifest"
sweep_once() {  # find remotely, sort LOCALLY (no remote-shell quoting games)
  vssh "find $SWEEP_ROOTS -type f $EXCLUDES_FIND -printf '%s\t%p\n' 2>/dev/null" \
    </dev/null | LC_ALL=C sort -t "$(printf '\t')" -k2
}
sweep_once > "$MANIFEST"
STABLE=0
for t in $(seq 2 "$SWEEP_MAX_TRIES"); do
  sleep 5
  sweep_once > "$MANIFEST.next"
  if cmp -s "$MANIFEST" "$MANIFEST.next"; then
    STABLE=1; rm -f "$MANIFEST.next"
    log "sweep stable after $t passes (two consecutive identical)"
    break
  fi
  log "sweep $((t-1)) vs $t differ ($(diff "$MANIFEST" "$MANIFEST.next" | grep -c '^[<>]' || true) rows) — box still writing; re-sweeping"
  mv "$MANIFEST.next" "$MANIFEST"
done
[ "$STABLE" = "1" ] || die "box did not quiesce in $SWEEP_MAX_TRIES sweeps — stop every writer and re-run 04"

N_REMOTE=$(wc -l < "$MANIFEST" | tr -d ' ')
[ "$N_REMOTE" -gt 0 ] || die "manifest is EMPTY — refusing to call that a capture"
# A path containing a tab would split into >2 fields and silently corrupt the
# byte-compare; refuse rather than mis-verify.
BADROWS=$(awk -F'\t' 'NF!=2' "$MANIFEST" | wc -l | tr -d ' ')
[ "$BADROWS" = "0" ] || die "$BADROWS manifest rows have embedded tabs — refusing (fix those paths first)"
log "manifest: $N_REMOTE files, $(awk -F'\t' '{s+=$1} END {printf "%.1f GB", s/1e9}' "$MANIFEST")"

# Local headroom check before a pull that can be hundreds of GB.
# df -Pk forces POSIX single-line output on both macOS and GNU coreutils.
NEED_GB=$(awk -F'\t' '{s+=$1} END {printf "%.0f", s/1e9}' "$MANIFEST")
FREE_GB=$(df -Pk "$DEST" | awk 'NR==2 {printf "%.0f", $4/1e6}')
log "SERIALIZE CONCURRENT CAPTURES: this reading is a snapshot of free space on the whole \
volume. A capture running in a sibling campaign directory sees the SAME free space, both pass \
this check, and the two pulls then race the disk to exhaustion. Run one capture at a time."
if [ -z "$FREE_GB" ]; then
  log "WARNING: could not read local free space for $DEST — skipping the headroom check"
else
  log "capture needs ~${NEED_GB}GB; $DEST has ${FREE_GB}GB free"
  [ "$FREE_GB" -gt "$NEED_GB" ] || die "not enough local disk (${FREE_GB}GB free, ~${NEED_GB}GB needed) — \
free space or point CAPTURE_DIR at a bigger volume. The box stays ALIVE."
fi

# ---- 3. pull -------------------------------------------------------------
log "2/5 rsync every manifested path -> $DEST/files/"
awk -F'\t' '{print substr($2,2)}' "$MANIFEST" > "$DEST/.filelist"
n=0
until rsync -a --timeout=300 --partial \
      -e "ssh $SSH_OPTS -i $SSH_KEY -p $SSH_PORT" \
      --files-from="$DEST/.filelist" "root@$SSH_HOST:/" "$DEST/files/"; do
  n=$((n+1)); [ "$n" -ge 8 ] && die "rsync failed after 8 tries — box stays ALIVE"
  log "rsync retry $n/8 (transient drop)"; sleep 10
done

# ---- 4. byte-size compare, EVERY file ------------------------------------
log "3/5 byte-size compare, local vs manifest, for all $N_REMOTE files"
MISMATCH="$DEST/mismatches.txt"
: > "$MISMATCH"
if ! DEST="$DEST" MANIFEST="$MANIFEST" python3 - > "$MISMATCH" <<'PY'
import os, sys
dest = os.environ["DEST"]; manifest = os.environ["MANIFEST"]
bad = 0; total = 0
with open(manifest) as f:
    for line in f:
        size_s, path = line.rstrip("\n").split("\t", 1)
        total += 1
        local = os.path.join(dest, "files", path.lstrip("/"))
        if not os.path.exists(local):
            print(f"MISSING\t{path}"); bad += 1
        elif os.path.getsize(local) != int(size_s):
            print(f"SIZE\t{path}\tremote={size_s}\tlocal={os.path.getsize(local)}")
            bad += 1
print(f"#checked={total} #bad={bad}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
then
  log "CAPTURE FAILED — size mismatches in $MISMATCH:"
  head -20 "$MISMATCH"
  die "byte verification failed; box stays ALIVE; fix and re-run 04"
fi

# ---- 5. content hash for files under the threshold -----------------------
log "4/5 sha256 content compare for files < $SHA256_MAX_BYTES bytes"
awk -F'\t' -v max="$SHA256_MAX_BYTES" '$1 < max {print $2}' "$MANIFEST" > "$DEST/.smallfiles"
N_SMALL=$(wc -l < "$DEST/.smallfiles" | tr -d ' ')
# shellcheck disable=SC2016  # $f must expand on the REMOTE shell, not here
vssh 'while IFS= read -r f; do sha256sum "$f"; done' < "$DEST/.smallfiles" > "$DEST/remote_hashes.txt"
N_HASHED=$(wc -l < "$DEST/remote_hashes.txt" | tr -d ' ')
[ "$N_HASHED" = "$N_SMALL" ] || die "remote hashed $N_HASHED of $N_SMALL small files — incomplete; re-run 04"
if ! DEST="$DEST" python3 - >> "$MISMATCH" <<'PY'
import hashlib, os, sys
dest = os.environ["DEST"]
bad = 0; total = 0
with open(os.path.join(dest, "remote_hashes.txt")) as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        rhash, path = line.split(None, 1)
        path = path.lstrip("*")  # sha256sum binary-mode marker
        total += 1
        local = os.path.join(dest, "files", path.lstrip("/"))
        h = hashlib.sha256()
        try:
            with open(local, "rb") as g:
                for chunk in iter(lambda: g.read(1 << 20), b""):
                    h.update(chunk)
        except FileNotFoundError:
            print(f"HASH_MISSING\t{path}"); bad += 1; continue
        if h.hexdigest() != rhash:
            print(f"HASH\t{path}\tremote={rhash}\tlocal={h.hexdigest()}")
            bad += 1
print(f"#hashed={total} #bad={bad}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
then
  log "CAPTURE FAILED — hash mismatches appended to $MISMATCH"
  tail -20 "$MISMATCH"
  die "content verification failed; box stays ALIVE; fix and re-run 04"
fi

# ---- 6. the green marker -------------------------------------------------
log "5/5 writing the capture marker"
SHA=$(shasum -a 256 "$MANIFEST" | cut -d' ' -f1)
{
  echo "instance=$INSTANCE"
  echo "workstream=$WORKSTREAM"
  echo "date=$(date -u +%FT%TZ)"
  echo "sweep_epoch=$SWEEP_EPOCH"
  echo "sweep_roots=$SWEEP_ROOTS"
  echo "files=$N_REMOTE"
  echo "files_lost=0"
  echo "files_hashed=$N_HASHED"
  echo "manifest_sha256=$SHA"
  echo "dest=$DEST"
} > "$STATE_DIR/CAPTURE_OK_$INSTANCE"
log "CAPTURE VERIFIED: $N_REMOTE files size-checked, $N_HASHED content-hashed, 0 lost"
log "marker: $STATE_DIR/CAPTURE_OK_$INSTANCE"
cost_status
log "next: bash 05_destroy.sh --marker $STATE_DIR/CAPTURE_OK_$INSTANCE --yes-i-am-really-sure"
