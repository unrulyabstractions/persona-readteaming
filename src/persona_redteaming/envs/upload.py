"""Verified upload of a campaign to the project's HF bucket.

Every byte goes through `persona_redteaming.cloud.hf_upload.upload_verified`,
the project's upload gate: it re-lists the destination and compares per-file
size and Xet content hash. A plain uploader returning 0 has published zero
files before, so an exit code is not evidence here; the gate's own
`VERIFIED: N files, M bytes` result is, and this driver refuses to record a
run as uploaded without it.

Destination layout under the bucket:
    envs/<model-short>/<env>/<cell>/<run-NN>/   one prefix per rollout
    envs/<model-short>/_campaign/               configs, summaries, report

The environment of a run is read from its own `run_meta.json`, never inferred
from the cell name, so a mislabelled cell cannot land under the wrong prefix.

Usage:
  python -m persona_redteaming.envs.upload --root ROOT --cells a,b \\
      --model-short qwen3-32b [--bucket hf://buckets/ns/name] [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from persona_redteaming.cloud.hf_upload import (
    DEFAULT_BUCKET,
    VerificationFailed,
    upload_verified,
)

#: Campaign-level files staged under `_campaign/` when present.
CAMPAIGN_FILES = (
    "campaign_summary.json", "grading_summary.json", "upload_log.json",
    "env_fingerprint.json", "report.md", "report.json", "serving_probe.json",
    "r1_template_verification.json",
    # The gate's on-stack re-judgement and the floor it was derived from. Both
    # ship; the shipped-threshold verdict lives in each run's own
    # gate_report.txt and is never overwritten.
    "gate_onstack_summary.json",
    # The pilot's mechanical c table: the evidence the in-context
    # construction was chosen on.
    "c_table_phase1.json",
)


def upload_one(local: Path, dest: str) -> dict:
    """Upload one directory to one prefix; a dict that is only `verified`
    when the gate proved every byte."""
    try:
        res = upload_verified(local, dest)
    except VerificationFailed as exc:
        return {"dest": dest, "verified": False, "files": None, "bytes": None,
                "tail": "\n".join(exc.failures[-20:])}
    except Exception as exc:  # network, auth: recorded, never a silent success
        return {"dest": dest, "verified": False, "files": None, "bytes": None,
                "tail": f"{type(exc).__name__}: {exc}"}
    return {"dest": dest, "verified": True, "files": res["files"],
            "bytes": res["bytes"], "tail": None}


def stage_campaign_files(root: Path) -> Path:
    """Copy the campaign-level artifacts into `<root>/_campaign_upload` so the
    remote prefix mirrors one directory exactly (the gate fails on
    unexplained remote extras)."""
    staging = root / "_campaign_upload"
    staging.mkdir(parents=True, exist_ok=True)
    for name in CAMPAIGN_FILES:
        src = root / name
        if src.exists():
            (staging / name).write_bytes(src.read_bytes())
    for pattern in ("harvest_summary*.json", "campaign_summary_*.json",
                    "debug_summary_*.json"):
        for src in root.glob(pattern):
            (staging / src.name).write_bytes(src.read_bytes())
    floor = root / "gate_calibration" / "floor.json"
    if floor.exists():
        (staging / "gate_floor.json").write_bytes(floor.read_bytes())
    for sub in ("configs", "icl_blocks"):
        dst = staging / sub
        dst.mkdir(exist_ok=True)
        for src in (root / sub).glob("*.yaml" if sub == "configs" else "*.json"):
            (dst / src.name).write_bytes(src.read_bytes())
    return staging


def upload_runs(root: Path, cells: list[str], model_short: str, *,
                bucket: str = DEFAULT_BUCKET, campaign_only: bool = False,
                force: bool = False) -> tuple[dict, int]:
    """Upload every completed run of `cells` plus the campaign files.

    Inputs: the campaign root (holds `runs/<cell>/run-NN`), the cells to ship,
    and the bucket path segment for the model. Output: `(log, n_failed)`; the
    log is also written to `<root>/upload_log.json`, one entry per prefix
    with `verified`, `files`, `bytes`. A run without `run_meta.json` is skipped as
    incomplete; a prefix already `verified` in the log is skipped unless
    `force`. Nothing is ever marked verified on the uploader's say-so.
    """
    root = Path(root).resolve()
    log_path = root / "upload_log.json"
    log = json.loads(log_path.read_text()) if log_path.exists() else {}
    failures = 0

    def record(key: str, entry: dict) -> None:
        nonlocal failures
        log[key] = entry
        log_path.write_text(json.dumps(log, indent=1))
        if entry["verified"]:
            print(f"[VERIFIED] {key}: {entry['files']} files, "
                  f"{entry['bytes']} bytes", flush=True)
        else:
            failures += 1
            print(f"[FAIL] {key}\n{entry['tail']}", flush=True)

    if not campaign_only:
        for cell in cells:
            for run_dir in sorted((root / "runs" / cell).glob("run-*")):
                meta_path = run_dir / "run_meta.json"
                if not meta_path.exists():
                    print(f"[skip] {cell}/{run_dir.name}: no run_meta.json "
                          "(incomplete rollout, not uploaded)", flush=True)
                    continue
                env = json.loads(meta_path.read_text())["env"]
                key = f"{env}/{cell}/{run_dir.name}"
                if log.get(key, {}).get("verified") and not force:
                    print(f"[skip] {key} already verified", flush=True)
                    continue
                dest = f"{bucket}/envs/{model_short}/{env}/{cell}/{run_dir.name}"
                record(key, upload_one(run_dir, dest))

    staging = stage_campaign_files(root)
    record("_campaign", upload_one(staging, f"{bucket}/envs/{model_short}/_campaign"))

    n_ok = sum(1 for e in log.values() if e.get("verified"))
    tot_f = sum(e["files"] or 0 for e in log.values() if e.get("verified"))
    tot_b = sum(e["bytes"] or 0 for e in log.values() if e.get("verified"))
    print(f"total verified: {n_ok} prefixes, {tot_f} files, {tot_b} bytes", flush=True)
    return log, failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--cells", required=True)
    ap.add_argument("--model-short", required=True,
                    help="bucket path segment for the model, e.g. r1-distill-qwen-14b")
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--campaign-only", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    cells = [a.strip() for a in args.cells.split(",") if a.strip()]
    _, n_failed = upload_runs(Path(args.root), cells, args.model_short, bucket=args.bucket,
                              campaign_only=args.campaign_only, force=args.force)
    return 0 if n_failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
