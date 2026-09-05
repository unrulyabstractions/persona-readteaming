"""CLI: `replay harvest <run_dir> ...` and `replay gate-report <acts_dir>`."""

from __future__ import annotations

import argparse
import sys

from .gate import GateConfig
from .store import load_manifest


def _parse_layers(s: str):
    if s.strip().lower() == "all":
        return "all"  # resolved to every decoder block once the model loads
    try:
        return [int(x) for x in s.split(",") if x.strip() != ""]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bad layer list {s!r}") from e


def _parse_units(s: str) -> list[str]:
    return [u.strip() for u in s.split(",") if u.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="replay", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest", help="replay a genlog run and store activations")
    h.add_argument("run_dir", help="run directory containing generations.jsonl")
    h.add_argument("--model", required=True, help="HF model id for the harvest")
    h.add_argument("--revision", default=None, help="pin the HF revision")
    h.add_argument(
        "--layers",
        required=True,
        type=_parse_layers,
        help="comma-separated 0-based decoder block indices (e.g. 7,14,21) "
        "or 'all' for every block",
    )
    h.add_argument(
        "--units",
        type=_parse_units,
        default=["sentence", "message", "last"],
        help="comma-separated span kinds: sentence,message,last,tokens "
        "(tokens = full per-token dump, opt-in)",
    )
    h.add_argument("--out", default=None, help="output dir (default <run_dir>/acts)")
    h.add_argument("--device", default=None, help="mps|cuda|cpu (default: auto)")
    h.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float32"])
    h.add_argument(
        "--text-mode",
        action="store_true",
        help="tokenize completion_text_raw when completion ids are null "
        "(contract: refused otherwise); flagged fidelity=text",
    )
    h.add_argument(
        "--skip-reverted",
        action="store_true",
        help="exclude reverted records (default: harvest them, flagged)",
    )
    h.add_argument(
        "--no-cache-reuse",
        action="store_true",
        help="disable KV-cache LCP reuse (fresh forward per record)",
    )
    h.add_argument(
        "--verify-lcp",
        action="store_true",
        help="verify every record's LCP-path states against a fresh full "
        "forward (bitwise + f64 rel_l2 + logprob deltas into the manifest); "
        "~2x compute — run once per (model, stack)",
    )
    h.add_argument("--gate-config", default=None, help="JSON file of GateConfig overrides")

    g = sub.add_parser("gate-report", help="print the gate report for a harvest")
    g.add_argument("acts_dir", help="directory containing manifest.json")
    return p


def cmd_harvest(args) -> int:
    from .harvest import harvest

    gate_cfg = GateConfig.from_json(args.gate_config) if args.gate_config else None
    res = harvest(
        args.run_dir,
        args.model,
        args.layers,
        args.units,
        out_dir=args.out,
        device=args.device,
        dtype=args.dtype,
        revision=args.revision,
        text_mode=args.text_mode,
        include_reverted=not args.skip_reverted,
        gate_cfg=gate_cfg,
        cache_reuse=False if args.no_cache_reuse else None,
        verify_lcp=args.verify_lcp,
    )
    t = res.manifest["totals"]
    print(f"harvest -> {res.out_dir}")
    print(
        f"records={t['n_records']} harvested={t['n_harvested']} "
        f"refused={t['n_refused']} skipped={t['n_skipped']} "
        f"gate_passed={t['n_gate_passed']} gate_failed={t['n_gate_failed']}"
    )
    print(
        f"tokens: naive={t['tokens_naive']} processed={t['tokens_processed']} "
        f"savings={t['savings_fraction']}"
    )
    if args.verify_lcp:
        for r in res.manifest["records"]:
            v = r.get("lcp_verification")
            if v:
                worst = max(pl["rel_l2_max"] for pl in v["per_layer"].values())
                print(
                    f"  lcp-verify invoke {r['invoke_idx']}: "
                    f"bitwise={v['all_layers_bitwise']} rel_l2_max={worst:.3e} "
                    f"lp_delta_max={v['logprob_delta_vs_fresh']['max']:.3e}"
                )
    for name, sha in sorted(res.file_hashes.items()):
        print(f"  {name}  sha256={sha[:16]}...")
    _warn_missing_deltas(res.manifest)
    return 0 if t["n_gate_failed"] == 0 and t["n_refused"] == 0 else 2


def cmd_gate_report(args) -> int:
    m = load_manifest(args.acts_dir)
    t = m["totals"]
    print(f"run {m['run_id']}  genlog sha256={m['genlog_sha256'][:16]}...")
    print(f"gate calibration: {m['harvest_params']['gate_config']['calibration_label']}")
    hdr = (
        f"{'idx':>4} {'status':>9} {'harv':>5} {'ntok':>5} {'mean_lp':>9} "
        f"{'greedy':>7} {'d_p99':>8} {'d_max':>8} {'verdict':>8}  notes"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in m["records"]:
        gate = r.get("gate")
        if not r["harvested"]:
            note = (
                f"REFUSED: {r['refusal']['reason']}"
                if r["refusal"]
                else f"skipped: {r['skip_reason']}"
            )
            print(
                f"{r['invoke_idx']:>4} {r['resolved_status']:>9} {'no':>5} "
                f"{'-':>5} {'-':>9} {'-':>7} {'-':>8} {'-':>8} {'-':>8}  {note}"
            )
            continue
        no_delta = gate["delta_max"] is None and gate["n_tokens"] > 0
        d_p99 = f"{gate['delta_p99']:.4f}" if gate["delta_p99"] is not None else "-"
        d_max = f"{gate['delta_max']:.4f}" if gate["delta_max"] is not None else "-"
        verdict = ("PASS*" if no_delta else "PASS") if gate["passed"] else "FAIL"
        notes = "; ".join(gate["failed_checks"])
        if no_delta:
            notes = "NO LOGGED LOGPROBS - delta checks SKIPPED. " + notes
        if r["resolved_status"] != "retained":
            notes = f"[{r['resolved_status']}] " + notes
        print(
            f"{r['invoke_idx']:>4} {r['resolved_status']:>9} {'yes':>5} "
            f"{gate['n_tokens']:>5} {gate['mean_logprob']:>9.4f} "
            f"{gate['greedy_agreement']:>7.4f} {d_p99:>8} {d_max:>8} "
            f"{verdict:>8}  {notes}"
        )
    print(
        f"totals: harvested={t['n_harvested']}/{t['n_records']} "
        f"refused={t['n_refused']} skipped={t['n_skipped']} "
        f"gate_passed={t['n_gate_passed']} gate_failed={t['n_gate_failed']} "
        f"savings={t['savings_fraction']}"
    )
    _warn_missing_deltas(m)
    return 0 if t["n_gate_failed"] == 0 and t["n_refused"] == 0 else 2


def _warn_missing_deltas(manifest: dict) -> int:
    """D7 (temp/53): a PASS without logged logprobs is a WEAKER pass —
    the per-token delta checks never ran, and corrupted-context records of
    the tripwire class (attacks A3/A4) are undetectable in that gap."""
    n_skipped = sum(
        1
        for r in manifest["records"]
        if r["harvested"]
        and r.get("gate")
        and r["gate"]["delta_max"] is None
        and r["gate"]["n_tokens"] > 0
    )
    if n_skipped:
        n_harv = manifest["totals"]["n_harvested"]
        print(
            "\n"
            "!!! WARNING: per-token delta checks were SKIPPED on "
            f"{n_skipped}/{n_harv} harvested records (no logged logprobs, or "
            "text-mode)\n"
            "!!! Those rows show PASS* on greedy agreement alone; corrupted "
            "context of the tripwire class (temp/53 A3/A4) is UNDETECTABLE "
            "there.\n"
            "!!! Arm the provider with logprobs (e.g. request_logprobs=1) so "
            "every record carries completion_logprobs."
        )
    return n_skipped


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "harvest":
        return cmd_harvest(args)
    if args.cmd == "gate-report":
        return cmd_gate_report(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
