#!/usr/bin/env bash
# 05_capture.sh — byte-verified sweep of the box. THE gate before destroy.
# Vendored from workspace/vast-harness/vast/04_capture.sh @0c418be; changes:
# per-model state/paths, axis-run exclude list (lib.sh), pusher-stop check.
#
# 1. SWEEP_EPOCH stamped BEFORE the find (the freshness reference must
#    predate the sweep, or files created between manifest and marker are
#    invisible to both the manifest and 06's freshness check). Manifest =
#    `find /root /workspace -type f -printf '%s\t%p\n'` over EVERYTHING,
#    minus only provably-reproducible trees (shared list in lib.sh). The
#    sweep re-runs until two CONSECUTIVE sweeps are identical (quiescence),
#    so an active writer cannot slip a file in mid-sweep.
# 2. rsync every manifest path locally (retries, --partial).
# 3. Compare local copy vs manifest: byte SIZE for every file, plus sha256
#    CONTENT hash for every file under SHA256_MAX_BYTES. ANY miss => exit 1
#    and NO marker.
# 4. On success write $STATE_DIR/CAPTURE_OK_<instance> carrying sweep_epoch —
#    06_destroy.sh refuses without it and freshness-checks against the epoch.
#
# Disk note: the 14B tree is ~350GB (activations 159 + rollouts 159 + rest);
# local free space MEASURED 2026-09-04: 643GiB. Re-check before running.
# Idempotent: re-running re-verifies everything; rsync only moves deltas.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_instance
resolve_ssh_target
assert_box_identity
require_cmd rsync

SHA256_MAX_BYTES="${SHA256_MAX_BYTES:-100000000}"
SWEEP_MAX_TRIES="${SWEEP_MAX_TRIES:-6}"

# Writers must be stopped or the quiescence sweep will (correctly) refuse.
# Pidfile liveness (never pgrep -f: self-matches the probing shell, 2026-09-04).
if vssh "{ [ -f $OUT_DIR/pusher.pid ] && kill -0 \$(cat $OUT_DIR/pusher.pid) 2>/dev/null; } || \
{ [ -f $OUT_DIR/driver.pid ] && kill -0 \$(cat $OUT_DIR/driver.pid) 2>/dev/null; }" </dev/null 2>/dev/null; then
  die "pusher or stage driver still running on the box — run 04_upload_final.sh first"
fi

DEST="$CAPTURE_DIR/$INSTANCE"
mkdir -p "$DEST"
MANIFEST="$DEST/remote_manifest.tsv"
EXCLUDES_FIND="$(capture_excludes_find)"

# ---- 1. quiescent manifest, epoch stamped FIRST --------------------------
SWEEP_EPOCH=$(date +%s)
log "1/5 sweep_epoch=$SWEEP_EPOCH (stamped BEFORE the find); building manifest"
sweep_once() {  # find remotely, sort LOCALLY (no remote-shell quoting games)
  vssh "find /root /workspace -type f $EXCLUDES_FIND -printf '%s\t%p\n' 2>/dev/null" \
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
[ "$STABLE" = "1" ] || die "box did not quiesce in $SWEEP_MAX_TRIES sweeps — stop writers and re-run 05"

N_REMOTE=$(wc -l < "$MANIFEST" | tr -d ' ')
[ "$N_REMOTE" -gt 0 ] || die "manifest is EMPTY — refusing to call that a capture"
# A path containing a tab would split into >2 fields and silently corrupt the
# byte-compare; refuse rather than mis-verify.
BADROWS=$(awk -F'\t' 'NF!=2' "$MANIFEST" | wc -l | tr -d ' ')
[ "$BADROWS" = "0" ] || die "$BADROWS manifest rows have embedded tabs — refusing (fix paths first)"
log "manifest: $N_REMOTE files, $(awk -F'\t' '{s+=$1} END {printf "%.1f GB", s/1e9}' "$MANIFEST")"

# ---- 2. pull -------------------------------------------------------------
log "2/5 rsync every manifested path -> $DEST/files/ (large trees: this can take hours)"
mkdir -p "$DEST/files"
awk -F'\t' '{print substr($2,2)}' "$MANIFEST" > "$DEST/.filelist"
n=0
until rsync -a --timeout=300 --partial \
      -e "ssh $SSH_OPTS -i $SSH_KEY -p $SSH_PORT" \
      --files-from="$DEST/.filelist" "root@$SSH_HOST:/" "$DEST/files/"; do
  n=$((n+1)); [ "$n" -ge 8 ] && die "rsync failed after 8 tries — box stays ALIVE"
  log "rsync retry $n/8 (transient drop)"; sleep 10
done

# ---- 3. byte-size compare ------------------------------------------------
log "3/5 byte-size compare local vs manifest"
MISMATCH="$DEST/mismatches.txt"
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
  die "byte verification failed; box stays ALIVE; fix and re-run 05"
fi

# ---- 4. content hash for small files -------------------------------------
log "4/5 sha256 content compare for files < $SHA256_MAX_BYTES bytes"
awk -F'\t' -v max="$SHA256_MAX_BYTES" '$1 < max {print $2}' "$MANIFEST" > "$DEST/.smallfiles"
N_SMALL=$(wc -l < "$DEST/.smallfiles" | tr -d ' ')
vssh 'while IFS= read -r f; do sha256sum "$f"; done' < "$DEST/.smallfiles" > "$DEST/remote_hashes.txt"
N_HASHED=$(wc -l < "$DEST/remote_hashes.txt" | tr -d ' ')
[ "$N_HASHED" = "$N_SMALL" ] || die "remote hashed $N_HASHED of $N_SMALL small files — incomplete; re-run 05"
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
  die "content verification failed; box stays ALIVE; fix and re-run 05"
fi

# ---- 5. marker -----------------------------------------------------------
log "5/5 writing capture marker"
SHA=$(shasum -a 256 "$MANIFEST" | cut -d' ' -f1)
{
  echo "instance=$INSTANCE"
  echo "model=$MODEL_TAG"
  echo "date=$(date -u +%FT%TZ)"
  echo "sweep_epoch=$SWEEP_EPOCH"
  echo "files=$N_REMOTE"
  echo "files_hashed=$N_HASHED"
  echo "manifest_sha256=$SHA"
  echo "dest=$DEST"
} > "$STATE_DIR/CAPTURE_OK_$INSTANCE"
log "CAPTURE VERIFIED: $N_REMOTE files size-checked, $N_HASHED content-hashed; marker $STATE_DIR/CAPTURE_OK_$INSTANCE"
cost_status
log "next: AXIS_CONFIG=$AXIS_CONFIG bash 06_destroy.sh --marker $STATE_DIR/CAPTURE_OK_$INSTANCE --yes-i-am-really-sure"
