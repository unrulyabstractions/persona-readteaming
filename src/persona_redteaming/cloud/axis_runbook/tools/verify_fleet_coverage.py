#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["huggingface_hub>=1.7.0"]
# ///
"""Fleet coverage + judged-role verification for run 2 (temp/45-run2-exec.md).

Independently RE-LISTS the shared bucket prefix and answers, per role:
  responses present? scores present? aggregate vector present?
  per-rollout safetensors + manifest present?
Then downloads a sample of scores files and checks they really parse as
judge scores (ints 0-3, no nulls) — the "judged-role verification" gate,
because a scores file existing proves nothing about its content (the
silent-all-null 401 incident, hub convention note).

Expected full-protocol coverage: 276 roles (275 judged; `default` is not
judged), 276 rollouts + manifests, aggregates <= 276 (roles failing
min_count=50 legitimately have no vectors/{role}.pt — reported, not hidden).

Usage: verify_fleet_coverage.py [--sample N] [--shards fleet_shards.json]
Exit: 0 complete; 2 incomplete (details printed); 3 judged-content defect.
"""
import argparse
import json
import os
import random
import sys
import tempfile

from huggingface_hub import HfApi

BUCKET = "unrulyabstractions/persona-redteaming"
PREFIX = "axis/r1d-qwen-14b"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--shards", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "fleet_shards.json"))
    a = ap.parse_args()

    shards = json.load(open(a.shards))
    want = sorted({r for w in shards["workers"].values() for r in w["roles"]})
    judged = [r for r in want if r != "default"]
    print(f"expected roles: {len(want)} ({len(judged)} judged + default)")

    api = HfApi()
    files = [f for f in api.list_bucket_tree(BUCKET, PREFIX, recursive=True)
             if getattr(f, "size", None) is not None]
    print(f"remote objects under {PREFIX}: {len(files)}")

    def names(sub, suffix):
        p = f"{PREFIX}/{sub}/"
        return {os.path.basename(f.path)[: -len(suffix)]: f
                for f in files
                if f.path.startswith(p) and f.path.endswith(suffix)
                and "/" not in f.path[len(p):]}

    resp = names("responses", ".jsonl")
    scores = names("scores", ".json")
    vecs = names("vectors", ".pt")
    rolls = names("vectors/rollouts", ".safetensors")
    mans = names("vectors/rollouts", ".manifest.json")

    rows = [("responses", resp, want), ("scores", scores, judged),
            ("aggregate vectors", vecs, want),
            ("rollout safetensors", rolls, want),
            ("rollout manifests", mans, want)]
    incomplete = False
    for label, have, expect in rows:
        missing = [r for r in expect if r not in have]
        tot = sum(have[r].size for r in have)
        print(f"{label:20s} {len(have):4d}/{len(expect):4d}  {tot/1e9:8.2f} GB"
              f"{'  MISSING ' + str(len(missing)) if missing else ''}")
        if missing:
            print(f"   missing[:25]: {' '.join(missing[:25])}")
            # aggregates may legitimately miss roles that failed min_count
            if label != "aggregate vectors":
                incomplete = True
    if len(vecs) < len(want):
        frac = 1 - len(vecs) / len(want)
        print(f"NOTE min_count attrition: {frac:.1%} of roles have no aggregate "
              f"vector (abort rule is >10%)")

    # ---- judged-content verification on a real sample --------------------
    pool = [r for r in judged if r in scores]
    pick = random.Random(0).sample(pool, min(a.sample, len(pool))) if pool else []
    print(f"\njudged-content check on {len(pick)} sampled roles:")
    tmp = tempfile.mkdtemp(prefix="cov_")
    pairs = [(scores[r].path, os.path.join(tmp, f"{r}.json")) for r in pick]
    if pairs:
        api.download_bucket_files(BUCKET, files=pairs)
    bad = 0
    tot_keys = tot_valid = 0
    for r in pick:
        try:
            d = json.load(open(os.path.join(tmp, f"{r}.json")))
        except Exception as e:
            print(f"  {r}: UNREADABLE {e!r}")
            bad += 1
            continue
        vals = list(d.values())
        ints = [v for v in vals if isinstance(v, int) and 0 <= v <= 3]
        tot_keys += len(vals)
        tot_valid += len(ints)
        nulls = sum(1 for v in vals if v is None)
        ok = vals and len(ints) == len(vals)
        print(f"  {r:16s} n={len(vals):5d} valid={len(ints):5d} nulls={nulls:4d}"
              f" dist={sorted(set(ints))}{'' if ok else '  <-- DEFECT'}")
        if not ok:
            bad += 1
    if tot_keys:
        print(f"sampled parse rate: {tot_valid}/{tot_keys} = "
              f"{tot_valid/tot_keys:.4%}")
    if bad:
        print(f"JUDGED-CONTENT DEFECT in {bad}/{len(pick)} sampled roles")
        return 3
    if incomplete:
        print("COVERAGE INCOMPLETE")
        return 2
    print("COVERAGE COMPLETE + judged content verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
