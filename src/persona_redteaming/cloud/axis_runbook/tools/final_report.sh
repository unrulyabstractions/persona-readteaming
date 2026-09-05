#!/usr/bin/env bash
# final_report.sh — assemble the run-2 stop-order close-out report from the
# GATE ARTIFACTS themselves (UPLOAD_OK / CAPTURE_OK / DESTROY_LOG / local
# capture trees), never from memory or from a script's claim.
#
# Every number printed here is re-derived at call time:
#   - roles preserved      = actual jsonl files counted in the local capture
#   - rollouts preserved   = actual line counts of those files
#   - bytes captured       = actual bytes on local disk
#   - files lost           = manifest rows minus verified rows (must be 0)
#   - spend                = live dph x measured lifetime per box
case $- in *x*) set +x ;; esac
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE" || exit 1
WORKERS="${WORKERS:-w1 w2 w3 w4 w5 w6 w7 w8}"

printf '%-5s %-11s %7s %9s %10s %12s %7s %8s %s\n' \
  W INSTANCE ROLES ROLLOUTS FILES_CAP BYTES_CAP LOST SPEND STATE
tot_roles=0; tot_roll=0; tot_bytes=0; tot_files=0; tot_lost=0; tot_spend=0
for w in $WORKERS; do
  tag="r1d-qwen-14b-$w"; sd=".state/$tag"
  iid="$(cat "$sd/instance_id" 2>/dev/null || ls "$sd" 2>/dev/null | sed -n 's/^destroyed_\([0-9]*\)_.*/\1/p' | head -1)"
  [ -n "$iid" ] || { printf '%-5s %-11s %s\n' "$w" "-" "NO INSTANCE RECORD"; continue; }

  cap="capture/$tag/$iid"
  roles=0; roll=0; files=0; bytes=0; lost="?"
  if [ -d "$cap/files" ]; then
    roles=$(find "$cap/files" -path '*/out/responses/*.jsonl' -type f 2>/dev/null | wc -l | tr -d ' ')
    roll=$(find "$cap/files" -path '*/out/responses/*.jsonl' -type f -exec cat {} \; 2>/dev/null | wc -l | tr -d ' ')
    files=$(find "$cap/files" -type f 2>/dev/null | wc -l | tr -d ' ')
    bytes=$(find "$cap/files" -type f -exec stat -f %z {} \; 2>/dev/null | awk '{s+=$1} END {printf "%d", s+0}')
    # files-lost is only meaningful once 05 has FINISHED: mid-rsync the
    # manifest legitimately exceeds what is on local disk (the 14:14 smoke
    # run showed w8 as "lost=15" while its capture was still streaming).
    if [ -f "$cap/remote_manifest.tsv" ] && [ -f "$sd/CAPTURE_OK_$iid" ]; then
      man=$(wc -l < "$cap/remote_manifest.tsv" | tr -d ' ')
      lost=$(( man - files ))
    else
      lost="in-flight"
    fi
  fi

  state="ALIVE"
  [ -f "$sd/CAPTURE_OK_$iid" ] && state="CAPTURED"
  ls "$sd"/destroyed_"$iid"_* >/dev/null 2>&1 && state="DESTROYED"
  [ -f ".state/${w}_reap_failed" ] && state="REAP_FAILED"

  spend=0
  t0="$(cat "$sd/launch_epoch" 2>/dev/null || echo 0)"
  dph="$(cat "$sd/dph" 2>/dev/null || echo 0)"
  tend="$t0"
  if [ "$state" = "DESTROYED" ]; then
    # 06_destroy renames instance_id -> destroyed_<iid>_<UTC stamp>. `mv`
    # PRESERVES mtime (it would read back as the LAUNCH time and report a
    # $0.00 spend — caught in the 14:14 smoke run), so the authoritative
    # end time is the stamp in the FILENAME.
    f=$(ls "$sd"/destroyed_"$iid"_* 2>/dev/null | head -1)
    stamp="${f##*_}"; stamp="${stamp%Z}"
    tend=$(python3 -c "
import calendar, time
try:
    print(calendar.timegm(time.strptime('$stamp', '%Y%m%dT%H%M%S')))
except Exception:
    print($t0)" 2>/dev/null || echo "$t0")
  else
    tend=$(date +%s)
  fi
  spend=$(python3 -c "print(f'{($tend-$t0)/3600*$dph:.2f}')" 2>/dev/null || echo 0)

  printf '%-5s %-11s %7s %9s %10s %12s %7s %8s %s\n' \
    "$w" "$iid" "$roles" "$roll" "$files" "$bytes" "$lost" "\$$spend" "$state"
  tot_roles=$((tot_roles+roles)); tot_roll=$((tot_roll+roll))
  tot_files=$((tot_files+files)); tot_bytes=$((tot_bytes+bytes))
  case "$lost" in ''|*[!0-9-]*) ;; *) tot_lost=$((tot_lost+lost)) ;; esac
  tot_spend=$(python3 -c "print(f'{$tot_spend+$spend:.2f}')")
done
echo
printf 'TOTALS roles=%s rollouts=%s capture_files=%s capture_bytes=%s (%.2f GB) files_lost=%s spend=$%s\n' \
  "$tot_roles" "$tot_roll" "$tot_files" "$tot_bytes" \
  "$(python3 -c "print($tot_bytes/1e9)")" "$tot_lost" "$tot_spend"
