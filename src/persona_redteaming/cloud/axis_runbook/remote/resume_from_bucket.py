#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["huggingface_hub>=1.7.0"]
# ///
"""Pre-seed a run's OUT_DIR from the HF bucket (resume-from-bucket idempotence).

All five pipeline stages skip existing per-role outputs (temp/41 §1), so
restoring what a previous box already pushed makes a relaunch incremental
(bluedot's 32B run resumed at 122/275 roles this way; temp/40 §2.4).

SAFETY: the stages treat an EXISTING per-role file as done, so restoring a
file that was pushed mid-write would silently truncate that role. Therefore
this tool restores ONLY resume-safe artifacts:
  - responses/{role}.jsonl   — only if its line count == EXPECTED_ROLLOUTS
                               (a partial stage-1 file must be regenerated)
  - scores/{role}.json       — safe: stage 3 merges by key, partial is fine
  - activations/{role}.tokens.json — additive metadata
Everything else (activations/*.pt is never in the bucket; vectors/, rollouts/,
axis.pt) is deliberately NOT restored: stages 4-5 are cheap CPU recomputes,
and stage 2 recomputes any role whose activations .pt is absent.

Usage: resume_from_bucket.py OUT_DIR hf://buckets/NS/NAME/PREFIX
Env:   EXPECTED_ROLLOUTS (default 1200 = 5 prompts x 240 questions; set to
       5 x question_count if STAGE1_EXTRA overrides it)
Token: read by huggingface_hub from HF_TOKEN; never printed.
"""
import os
import sys
import tempfile

from huggingface_hub import HfApi

PFX = "hf://buckets/"
SAFE_ALWAYS = ("scores/",)
SAFE_TOKENS = ("activations/",)  # only *.tokens.json under here


def main() -> int:
    if len(sys.argv) != 3 or not sys.argv[2].startswith(PFX):
        print(__doc__)
        return 1
    out_dir, url = sys.argv[1], sys.argv[2]
    expected = int(os.environ.get("EXPECTED_ROLLOUTS", "1200"))
    rest = url[len(PFX):].strip("/").split("/")
    if len(rest) < 2:
        print(f"ERROR: bad bucket url {url!r}")
        return 1
    bucket_id = "/".join(rest[:2])
    prefix = "/".join(rest[2:])

    api = HfApi()
    try:
        remote = [f for f in api.list_bucket_tree(bucket_id, prefix, recursive=True)
                  if getattr(f, "size", None) is not None]
    except Exception as e:
        print(f"RESUME: cannot list {bucket_id}/{prefix}: {e!r} — treating as empty")
        return 0

    candidates = []   # (remote_path, local_path, needs_line_gate)
    for f in remote:
        rel = f.path[len(prefix):].lstrip("/") if prefix and f.path.startswith(prefix) else f.path
        local = os.path.join(out_dir, rel)
        if os.path.exists(local) and os.path.getsize(local) == f.size:
            continue
        if rel.startswith("responses/") and rel.endswith(".jsonl"):
            candidates.append((f.path, local, True))
        elif rel.startswith(SAFE_ALWAYS) and rel.endswith(".json"):
            candidates.append((f.path, local, False))
        elif rel.startswith(SAFE_TOKENS) and rel.endswith(".tokens.json"):
            candidates.append((f.path, local, False))

    print(f"RESUME: {len(remote)} remote files under {prefix or '/'}; "
          f"{len(candidates)} resume-safe candidates (expected_rollouts={expected})")
    if not candidates:
        print("RESUME_OK nothing to do")
        return 0

    # Download to a temp area first; only complete responses files are promoted.
    tmp = tempfile.mkdtemp(prefix="resume_", dir=out_dir if os.path.isdir(out_dir) else None)
    pairs = [(r, os.path.join(tmp, str(i))) for i, (r, _, _) in enumerate(candidates)]
    api.download_bucket_files(bucket_id, files=pairs)

    restored = skipped_partial = missing = 0
    for i, (rpath, local, line_gate) in enumerate(candidates):
        staged = os.path.join(tmp, str(i))
        if not os.path.exists(staged):
            print(f"RESUME_WARN: download missing for {rpath}")
            missing += 1
            continue
        if line_gate:
            with open(staged, "rb") as fh:
                n_lines = sum(1 for _ in fh)
            if n_lines != expected:
                print(f"RESUME_SKIP partial responses file {rpath} ({n_lines}/{expected} rollouts)")
                skipped_partial += 1
                os.remove(staged)
                continue
        os.makedirs(os.path.dirname(local), exist_ok=True)
        os.replace(staged, local)
        restored += 1

    print(f"RESUME_OK restored={restored} skipped_partial={skipped_partial} missing={missing}")
    return 0 if missing == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
