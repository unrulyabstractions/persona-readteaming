#!/usr/bin/env python3
"""Independent read-back of what actually landed in the bucket.

The upload gate verifies from the box that wrote the files. This verifies from a
different machine, against the bucket's own listing, because rc=0 from an
uploader has published zero files on this project before. It counts objects and
bytes per cell, and asserts the shapes the campaign expects: one genlog, one
grading, one manifest and N layer files per run.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

LAYER_RE = re.compile(r"/acts/layer_(\d+)\.safetensors$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default="unrulyabstractions/persona-redteaming")
    ap.add_argument("--prefix", required=True, help="e.g. envs/qwen3-32b")
    ap.add_argument("--expect-layers", type=int, required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from huggingface_hub import HfApi

    api = HfApi()
    files = list(api.list_bucket_tree(args.bucket, args.prefix, recursive=True))
    total_bytes = sum(f.size or 0 for f in files)

    per_run: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter)
    per_cell = collections.Counter()
    per_cell_bytes = collections.Counter()
    layers_per_run: dict[str, set] = collections.defaultdict(set)
    run_seg = re.compile(r"^run-\d+$")
    for f in files:
        rel = f.path[len(args.prefix):].lstrip("/")
        parts = rel.split("/")
        # Find the `run-NN` segment; everything before it names the cell, and
        # the prefix depth then does not matter.
        idx = next((i for i, p in enumerate(parts) if run_seg.match(p)), None)
        if idx is None:
            per_cell["_campaign"] += 1
            per_cell_bytes["_campaign"] += f.size or 0
            continue
        run = "/".join(parts[: idx + 1])
        cell = "/".join(parts[:idx]) or "(root)"
        per_run[run][parts[-1]] += 1
        per_cell[cell] += 1
        per_cell_bytes[cell] += f.size or 0
        m = LAYER_RE.search(f.path)
        if m:
            layers_per_run[run].add(int(m.group(1)))

    bad_layers = {r: sorted(s) for r, s in layers_per_run.items()
                  if len(s) != args.expect_layers}
    missing = {}
    for run, counts in per_run.items():
        need = ["generations.jsonl", "run_meta.json", "state.json",
                "messages.json", "grading.json"]
        gone = [n for n in need if counts.get(n, 0) == 0]
        if gone:
            missing[run] = gone

    out = {
        "bucket": args.bucket, "prefix": args.prefix,
        "n_objects": len(files), "total_bytes": total_bytes,
        "total_gb": round(total_bytes / 1e9, 3),
        "n_runs": len(per_run),
        "expect_layers_per_run": args.expect_layers,
        "runs_with_wrong_layer_count": bad_layers,
        "runs_missing_required_files": missing,
        "objects_per_cell": dict(per_cell),
        "bytes_per_cell": dict(per_cell_bytes),
    }
    print(json.dumps({k: v for k, v in out.items()
                      if k not in ("objects_per_cell", "bytes_per_cell")}, indent=1))
    for cell in sorted(per_cell):
        print(f"  {cell:40s} {per_cell[cell]:6d} objects "
              f"{per_cell_bytes[cell] / 1e9:8.3f} GB")
    ok = not bad_layers and not missing and len(per_run) > 0
    print(f"\nVERDICT: {'OK' if ok else 'PROBLEMS ABOVE'}")
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
