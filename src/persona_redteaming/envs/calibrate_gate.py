"""Measure THIS stack's numerical noise floor, then re-judge the gate on it.

The harvester's default per-token logprob thresholds were calibrated on
Qwen3-0.6B on MPS (its own gate report says so, and tells you to recalibrate
before any CUDA harvest). Applying them to a 14B or 32B checkpoint on CUDA
against a vLLM generation is a category error: the previous campaign measured
per-token deltas of ~1.0 nats against a 0.5 threshold, i.e. the threshold sat
BELOW the stack's own bf16 noise.

Two numbers are needed to say anything honest about that.

**The floor.** Replay the same records twice on the SAME weights and the SAME
engine, once through the KV-cache LCP path and once as a fresh full forward,
and measure how far the harvester disagrees with ITSELF. That is pure bf16
rounding: any gate threshold below it is measuring nothing.

**The signal.** The cross-engine delta the gate actually reports, replay
against the logprobs vLLM recorded at generation time.

Usage:
  python -m persona_redteaming.envs.calibrate_gate --root ROOT --cells a,b \
      --base-model M --floor-runs sb_control/run-01,... --n-layers 64

A threshold is only meaningful above the floor. This script measures the floor
on real campaign records, writes it out, and then RE-JUDGES every stored
manifest against a floor-derived threshold — reading the per-record statistics
the harvest already stored, so no model is re-run. Both verdicts are kept: the
original report is never overwritten.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def measure_floor(root: Path, base_model: str, runs: list[str],
                  device: str, out: Path, layers: list[int],
                  revision: str | None = None) -> dict:
    """Replay `runs` twice on the same weights (LCP path and fresh forward) and
    return the per-record self-disagreement, the stack's bf16 noise floor."""
    from agent_interp_envs.providers.r1_distill_template import patch_tokenizer

    from persona_redteaming.replay.harvest import harvest, load_model_and_tokenizer

    tok, model = load_model_and_tokenizer(base_model, device, "bfloat16", revision)
    # Same correction the rollout and the harvest applied; without it every R1
    # record is refused on template-sha-mismatch and the floor is measured on
    # nothing.
    patched = patch_tokenizer(tok, base_model)
    if patched:
        print(f"[floor] R1 template correction applied: {patched}", flush=True)
    per_record = []
    for rel in runs:
        run_dir = root / "runs" / rel
        if not (run_dir / "generations.jsonl").exists():
            print(f"[skip] {rel}: no genlog", flush=True)
            continue
        res = harvest(
            run_dir, base_model, layers, ["message"],
            out_dir=out / "throwaway" / rel.replace("/", "_"),
            device=device, dtype="bfloat16", model=model, tok=tok,
            verify_lcp=True,
        )
        for r in res.manifest["records"]:
            v = r.get("lcp_verification")
            if not v:
                continue
            d = v["logprob_delta_vs_fresh"]
            per_record.append({
                "run": rel, "invoke_idx": r["invoke_idx"],
                "n_tokens": r["gate"]["n_tokens"] if r.get("gate") else None,
                "bitwise": v["all_layers_bitwise"],
                "rel_l2_max": max(pl["rel_l2_max"] for pl in v["per_layer"].values()),
                "lp_delta_max": d["max"], "lp_delta_p99": d["p99"],
                "lp_delta_mean": d["mean"],
            })
            print(f"  {rel} idx={r['invoke_idx']} bitwise={v['all_layers_bitwise']} "
                  f"lp_max={d['max']} lp_p99={d['p99']}", flush=True)
    maxes = [r["lp_delta_max"] for r in per_record if r["lp_delta_max"] is not None]
    p99s = [r["lp_delta_p99"] for r in per_record if r["lp_delta_p99"] is not None]
    floor = {
        "n_records": len(per_record),
        "n_bitwise": sum(1 for r in per_record if r["bitwise"]),
        "lp_delta_max_worst": max(maxes) if maxes else None,
        "lp_delta_max_median": statistics.median(maxes) if maxes else None,
        "lp_delta_p99_worst": max(p99s) if p99s else None,
        "rel_l2_max_worst": max(r["rel_l2_max"] for r in per_record) if per_record else None,
        "per_record": per_record,
        "what_this_is": (
            "Same weights, same engine, same tokens: LCP/KV-reuse path vs a "
            "fresh full forward. This is the harvester disagreeing with "
            "itself at bf16. Any gate threshold at or below these numbers "
            "cannot distinguish a fidelity failure from rounding."
        ),
    }
    return floor


def regate(root: Path, arms: list[str], delta_max: float, delta_p99: float,
           greedy_min: float, label: str) -> dict:
    """Re-judge stored per-record stats against on-stack thresholds."""
    rows = []
    for arm in arms:
        for run_dir in sorted((root / "runs" / arm).glob("run-*")):
            mp = run_dir / "acts" / "manifest.json"
            if not mp.exists():
                continue
            m = json.loads(mp.read_text())
            recs = []
            for r in m["records"]:
                g = r.get("gate")
                if not r.get("harvested") or not g:
                    continue
                failed = []
                if g["delta_max"] is not None and g["delta_max"] > delta_max:
                    failed.append(f"delta_max {g['delta_max']:.4f} > {delta_max}")
                if g["delta_p99"] is not None and g["delta_p99"] > delta_p99:
                    failed.append(f"delta_p99 {g['delta_p99']:.4f} > {delta_p99}")
                if g["greedy_agreement"] < greedy_min:
                    failed.append(
                        f"greedy {g['greedy_agreement']:.4f} < {greedy_min}")
                recs.append({
                    "invoke_idx": r["invoke_idx"], "n_tokens": g["n_tokens"],
                    "delta_max": g["delta_max"], "delta_p99": g["delta_p99"],
                    "greedy": g["greedy_agreement"],
                    "orig_passed": g["passed"], "onstack_passed": not failed,
                    "failed_checks": failed,
                })
            rows.append({
                "arm": arm, "run": run_dir.name,
                "n_records": len(recs),
                "n_orig_pass": sum(1 for r in recs if r["orig_passed"]),
                "n_onstack_pass": sum(1 for r in recs if r["onstack_passed"]),
                "records": recs,
            })
            report = [
                f"run {arm}/{run_dir.name}  on-stack gate: {label}",
                f"thresholds: delta_max<={delta_max} delta_p99<={delta_p99} "
                f"greedy>={greedy_min}",
                f"{'idx':>4} {'ntok':>5} {'d_p99':>8} {'d_max':>8} {'greedy':>7} "
                f"{'orig':>6} {'onstack':>8}  notes",
            ]
            for r in recs:
                dp = f"{r['delta_p99']:.4f}" if r["delta_p99"] is not None else "-"
                dm = f"{r['delta_max']:.4f}" if r["delta_max"] is not None else "-"
                report.append(
                    f"{r['invoke_idx']:>4} {r['n_tokens']:>5} {dp:>8} {dm:>8} "
                    f"{r['greedy']:>7.4f} {'PASS' if r['orig_passed'] else 'FAIL':>6} "
                    f"{'PASS' if r['onstack_passed'] else 'FAIL':>8}  "
                    + "; ".join(r["failed_checks"]))
            (run_dir / "gate_report_onstack.txt").write_text("\n".join(report) + "\n")
    return {"rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--base-model", required=True)
    ap.add_argument("--revision", default=None)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--floor-runs", required=True,
                    help="comma-separated <cell>/<run-NN> paths to measure the "
                         "floor on; pick real campaign records, not a fixture")
    ap.add_argument("--cells", required=True)
    ap.add_argument("--n-layers", type=int, required=True,
                    help="decoder layers in this checkpoint; the floor is "
                         "measured on the first, middle and last")
    ap.add_argument("--greedy-min", type=float, default=0.25)
    ap.add_argument("--skip-floor", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    out = root / "gate_calibration"
    out.mkdir(parents=True, exist_ok=True)
    floor_path = out / "floor.json"

    layers = sorted({0, args.n_layers // 2, args.n_layers - 1})
    if args.skip_floor and floor_path.exists():
        floor = json.loads(floor_path.read_text())
    else:
        floor = measure_floor(root, args.base_model,
                              [r.strip() for r in args.floor_runs.split(",")],
                              args.device, out, layers, args.revision)
        floor_path.write_text(json.dumps(floor, indent=1))
    print(json.dumps({k: v for k, v in floor.items() if k != "per_record"},
                     indent=1))

    # The threshold is the measured floor, not a number chosen to make things
    # pass. Anything at or below the floor is rounding; the margin is one
    # factor of two above the worst same-engine disagreement observed.
    worst = floor["lp_delta_max_worst"] or 0.0
    p99w = floor["lp_delta_p99_worst"] or 0.0
    delta_max = round(2.0 * worst, 3)
    delta_p99 = round(2.0 * p99w, 3)
    label = (f"on-stack {args.base_model}/{args.device}/bf16 vs vLLM v0.27.1; "
             f"floor from {floor['n_records']} same-engine LCP-vs-fresh "
             f"comparisons on layers {layers} (worst {worst:.4f} nats), "
             f"threshold = 2x floor")
    res = regate(root, [a.strip() for a in args.cells.split(",")],
                 delta_max, delta_p99, args.greedy_min, label)
    summary = {
        "label": label,
        "thresholds": {"delta_max": delta_max, "delta_p99": delta_p99,
                       "greedy_min": args.greedy_min},
        "floor": {k: v for k, v in floor.items() if k != "per_record"},
        "runs": res["rows"],
        "totals": {
            "n_runs": len(res["rows"]),
            "n_records": sum(r["n_records"] for r in res["rows"]),
            "n_orig_pass": sum(r["n_orig_pass"] for r in res["rows"]),
            "n_onstack_pass": sum(r["n_onstack_pass"] for r in res["rows"]),
        },
    }
    (root / "gate_onstack_summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary["totals"], indent=1))
    print(f"thresholds: {summary['thresholds']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
