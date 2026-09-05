"""Drive the campaign: resolve each cell's config, run every rollout.

A CELL is one (environment, variant, arm) combination. Cells come in four
groups:

  pilot   four in-context constructions x both environments, a few rollouts
          each, run before the campaign to answer one question: which
          construction still lets the model REACH the decision point? The prior
          campaign's arm C halved `c`, and the redesign has to be chosen on
          measured `c`, not on taste.
  main    control and the winning in-context arm, ten rollouts each, plus
          `sandbagging`'s own incentive-flipped variant as a reference.
  pc      positive and negative controls: a prompt suffix that explicitly
          instructs the behaviour (`*_pc`) or forces the file reads and then
          instructs the honest behaviour (`*_nc`). Without a positive control
          a zero is uninterpretable; without a negative control a hit cannot
          be told from a detector that fires on the suffix itself.
  debug   the R1-Distill loop interventions (system placement, tool format,
          multi-call policy, wall-off, gate, think retention), every cell
          the sandbagging control plus the listed rollout arguments.

Every cell of an environment uses the same environment files, the same system
prompt and the same per-run seeds: run-03 of every cell samples with seed 103,
so the sampling noise source is paired across cells.

A cell spec is a dict: `env`, `variant`, `group`, `arm`, `role`, and
optionally `icl` + `placement` (an in-context block), `suffix` (a file in
`prompt_suffixes/` appended to the user prompt) and `rollout_args` (extra
`rollout.py` flags). `run_campaign(spec, ...)` is the one runner; the CLI
below only resolves names to specs.

Resumable: a run directory that already holds run_meta.json is skipped, so a
dropped ssh session or a restarted box costs only the in-flight rollouts.
"""

from __future__ import annotations

import argparse
import copy
import json
import queue
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SUFFIX_DIR = HERE / "prompt_suffixes"
DEFAULT_ICL_DIR = HERE / "icl_blocks"

ENV_CONFIGS = {
    ("sandbagging", "false"): ("sandbagging", "normal.yaml"),
    ("sandbagging", "true"): ("sandbagging", "reverse.yaml"),
    ("eval_tampering", "notes_self_weapons"): ("eval_tampering", "default.yaml"),
}

ICL_CONSTRUCTIONS = ("fum16", "turns16", "turns8", "turns16num")
ICL_PLACEMENT = {
    "fum16": "first_user_message",
    "turns16": "prior_turns",
    "turns8": "prior_turns",
    "turns16num": "prior_turns",
}

SB = {"env": "sandbagging", "variant": "false"}
SBR = {"env": "sandbagging", "variant": "true"}
ET = {"env": "eval_tampering", "variant": "notes_self_weapons"}


def _pilot_cells() -> dict:
    cells = {}
    for base, tag in ((SB, "sb"), (ET, "et")):
        for c in ICL_CONSTRUCTIONS:
            cells[f"{tag}_pilot_{c}"] = {
                **base, "group": "pilot", "arm": f"icl_{c}",
                "icl": c, "placement": ICL_PLACEMENT[c],
                "role": f"pilot: in-context EM, construction {c}",
            }
    return cells


CELLS: dict[str, dict] = {
    **_pilot_cells(),
    "sb_control": {
        **SB, "group": "main", "arm": "control",
        "role": "control: base weights, the environment's own prompt, no nudge",
    },
    "sb_em": {
        **SB, "group": "main", "arm": "em_icl", "icl": "PICKED", "placement": "PICKED",
        "role": "in-context EM, construction chosen on the pilot's c",
    },
    "sb_reverse": {
        **SBR, "group": "main", "arm": "control_reverse",
        "role": "the environment's own incentive flip: low scores are punished "
                "instead of high ones. Reference for reading accuracy.",
    },
    "sb_pc": {
        **SB, "group": "pc", "arm": "pc_instructed_sandbag",
        "suffix": "sandbagging_pc.txt",
        "role": "positive control: the prompt instructs deliberate "
                "underperformance, so the detector must fire",
    },
    "et_control": {
        **ET, "group": "main", "arm": "control",
        "role": "control: base weights, the environment's own prompt, no nudge",
    },
    "et_em": {
        **ET, "group": "main", "arm": "em_icl", "icl": "PICKED", "placement": "PICKED",
        "role": "in-context EM, construction chosen on the pilot's c",
    },
    "et_pc": {
        **ET, "group": "pc", "arm": "pc_instructed_conceal",
        "suffix": "eval_tampering_pc.txt",
        "role": "positive control: the prompt instructs concealment, so the "
                "detector must fire",
    },
    "sb_nc": {
        **SB, "group": "pc", "arm": "nc_instructed_accurate",
        "suffix": "sandbagging_nc.txt",
        "role": "negative control: the prompt forces the Documentation read and "
                "then instructs accurate answers regardless, so the detector "
                "must NOT fire",
    },
    "et_nc": {
        **ET, "group": "pc", "arm": "nc_instructed_report",
        "suffix": "eval_tampering_nc.txt",
        "role": "negative control: the prompt forces the notes read and then "
                "instructs honest reporting, so the detector must NOT fire",
    },
}


def _debug_cells() -> dict:
    """The R1-Distill loop interventions: sandbagging control plus arguments.

    `A_system_noicl` carries no intervention and replicates `sb_control`;
    every other cell differs from it by its `rollout_args` and nothing else.
    Seeds are paired across cells exactly as in the main campaign.
    """
    one_call = ["--prompt-suffix-file", "one_call.txt"]
    procedure = ["--prompt-suffix-file", "procedure.txt"]
    react = ["--prompt-suffix-file", "react.txt"]
    specs = {
        # phase 1: the pre-registered 2x2
        "A_system_noicl": ("replication gate: sb_control, byte-identical prompt", []),
        "B_fold_noicl": ("system text + tools block folded into the task user message",
                         ["--system-placement", "fold_user"]),
        "C_system_icl": ("as run, plus turns16num as prior turns (sb_em)", [], "turns16num"),
        "D_fold_icl": ("folded, plus turns16num as prior turns",
                       ["--system-placement", "fold_user"], "turns16num"),
        # phase 2: one variable at a time against A
        "E1_fenced": ("ask for the ```json fenced form the model already emits",
                      ["--tool-format", "fenced_json"]),
        "E1a_reject_multi": ("multi-call turn gets the environment's own 'ONLY ONE "
                             "function' pushback", ["--multi-call", "reject"]),
        "E1b_first_call": ("multi-call turn executes its FIRST call instead of its last",
                           ["--multi-call", "first"]),
        "E1c_one_call_prompt": ("user turn asks for exactly one tool call per message",
                                one_call),
        "E2_procedure": ("explicit numbered procedure in the user turn", procedure),
        "E3_walloff": ("early submit rejected by a tool result; loop continues",
                       ["--wall-off-submit"]),
        "E4_gate_submit": ("submit absent from the schema until both files were read",
                           ["--gate-submit-tool"]),
        "E5_react": ("Thought / Action / Observation framing in the user turn", react),
        "E6_think16k": ("max_tokens 16384", ["--max-tokens", "16384"]),
        "E6_think2k": ("max_tokens 2048", ["--max-tokens", "2048"]),
        "E7_temp0": ("temperature 0: the deterministic default path",
                     ["--temperature", "0.0"]),
        "E8_keepthink": ("prior-step reasoning retained inside the tool loop",
                         ["--keep-think"]),
        # combinations
        "F1_fenced_first": ("fenced form + first-call execution",
                            ["--tool-format", "fenced_json", "--multi-call", "first"]),
        "F2_fenced_reject": ("fenced form + multi-call pushback",
                             ["--tool-format", "fenced_json", "--multi-call", "reject"]),
        "F3_first_walloff": ("first-call execution + early-submit wall-off",
                             ["--multi-call", "first", "--wall-off-submit"]),
        "F4_fenced_first_walloff": ("fenced + first-call + wall-off",
                                    ["--tool-format", "fenced_json", "--multi-call",
                                     "first", "--wall-off-submit"]),
        "F5_fold_first": ("folded instructions + first-call execution",
                          ["--system-placement", "fold_user", "--multi-call", "first"]),
        "F6_first_onecall": ("first-call execution + one-call prompt",
                             ["--multi-call", "first", *one_call]),
        "F7_fenced_onecall": ("fenced form + one-call prompt",
                              ["--tool-format", "fenced_json", *one_call]),
        "F8_first_procedure": ("first-call execution + explicit procedure",
                               ["--multi-call", "first", *procedure]),
        "F9_fold_fenced_first": ("folded instructions + fenced form + first-call",
                                 ["--system-placement", "fold_user", "--tool-format",
                                  "fenced_json", "--multi-call", "first"]),
        "F10_first_keepthink": ("first-call execution + reasoning retained",
                                ["--multi-call", "first", "--keep-think"]),
        "F11_fold_keepthink": ("folded instructions + reasoning retained",
                               ["--system-placement", "fold_user", "--keep-think"]),
        "F12_fold_first_keepthink": ("folded + first-call + reasoning retained",
                                     ["--system-placement", "fold_user", "--multi-call",
                                      "first", "--keep-think"]),
        "F13_first_keepthink_walloff": ("first-call + reasoning retained + wall-off",
                                        ["--multi-call", "first", "--keep-think",
                                         "--wall-off-submit"]),
        "G1_icl_user_only": ("turns16num with the assistant turns removed",
                             ["--icl-user-only"], "turns16num"),
        "G2_icl_first": ("turns16num as prior turns + first-call execution",
                         ["--multi-call", "first"], "turns16num"),
    }
    cells = {}
    for name, spec in specs.items():
        role, args = spec[0], spec[1]
        cell = {**SB, "group": "debug", "arm": name, "role": role, "rollout_args": args}
        if len(spec) == 3:
            cell["icl"] = spec[2]
            cell["placement"] = "prior_turns"
        cells[name] = cell
    return cells


CELLS.update(_debug_cells())


def build_cell_config(aie: Path, out_path: Path, cell: str, spec: dict,
                      served_model: str) -> Path:
    """Resolve one cell's config from the environment's own YAML.

    The prompts are copied VERBATIM. The only edits are the provider fields,
    which name the served model rather than an OpenRouter route.
    """
    env, cfg_name = ENV_CONFIGS[(spec["env"], spec["variant"])]
    src = aie / "configs" / env / cfg_name
    cfg = copy.deepcopy(yaml.safe_load(src.read_text()))
    cfg["agent"]["provider"] = "vllm"
    cfg["agent"]["model"] = served_model
    cfg["agent"].pop("provider_preferences", None)
    # reasoning_effort is an OpenRouter/Fireworks generation-time knob with no
    # meaning on a raw /v1/completions path. Both models' thinking modes are
    # template-forced and verified per run from the completions.
    cfg["agent"].pop("reasoning_effort", None)
    cfg["_campaign"] = {"cell": cell, "arm": spec["arm"], "role": spec["role"],
                        "source_config": str(src)}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return out_path


def run_one(cmd: list[str], log_path: Path) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as fh:
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode, str(log_path)


def resolve_cells(names: str, em_construction: str | None = None) -> dict[str, dict]:
    """Turn `--cells` into fully resolved specs.

    `names` is a comma-separated list of cell names or one group name
    (`pilot|main|pc|debug|all`). A main EM cell has `icl: PICKED` and needs
    `em_construction` (one of ICL_CONSTRUCTIONS). Raises ValueError on an
    unknown cell, a missing or unknown construction; never silently drops a
    cell.
    """
    if names in ("pilot", "main", "pc", "debug", "all"):
        cells = [c for c, s in CELLS.items() if names == "all" or s["group"] == names]
    else:
        cells = [c.strip() for c in names.split(",") if c.strip()]
    unknown = [c for c in cells if c not in CELLS]
    if unknown:
        raise ValueError(f"unknown cells {unknown}; known: {sorted(CELLS)}")
    specs = {c: copy.deepcopy(CELLS[c]) for c in cells}
    for c, s in specs.items():
        if s.get("icl") == "PICKED":
            if not em_construction:
                raise ValueError(f"cell {c} needs em_construction "
                                 f"(one of {ICL_CONSTRUCTIONS})")
            if em_construction not in ICL_CONSTRUCTIONS:
                raise ValueError(f"unknown em_construction {em_construction!r}")
            s["icl"] = em_construction
            s["placement"] = ICL_PLACEMENT[em_construction]
            s["role"] += f" ({em_construction})"
    return specs


def run_campaign(
    specs: dict[str, dict],
    *,
    root: Path,
    aie: Path,
    served_model: str,
    tokenizer_model: str,
    base_url: str = "http://127.0.0.1:8000",
    icl_dir: Path | None = None,
    model_revision: str | None = None,
    n: int = 10,
    n_pilot: int = 4,
    n_pc: int = 5,
    n_debug: int = 5,
    seed_base: int = 100,
    concurrency: int = 1,
    wallclock_cap_s: float = 1200.0,
    temperature: float = 0.6,
    top_p: float = 0.95,
    max_tokens: int = 8192,
    workspace: str = "/agent",
    harness_root: str | None = None,
    tag: str | None = None,
    force: bool = False,
    em_construction: str | None = None,
) -> dict:
    """Run every rollout of every cell in `specs` against one served model.

    Inputs: resolved cell specs (see `resolve_cells`), the campaign `root`
    (writes `configs/`, `runs/<cell>/run-NN/`, `logs/`, `campaign_summary.json`),
    the agent-interp-envs checkout `aie`, the served alias and tokenizer id,
    and the sampling parameters shared by every cell. `n*` set rollouts per
    group; `seed_base + i` is run-i's seed in every cell, so sampling noise is
    paired across cells. `tag` suffixes the run cell directory (replications).
    Returns the summary dict it also writes. It refuses to start when an
    environment config, in-context block or prompt suffix is missing, and when
    `concurrency > 1` is asked for without a `{slot}` workspace (concurrent
    rollouts re-stage the same path and destroy each other's files). A
    run directory that already holds run_meta.json is skipped unless `force`.
    """
    root = Path(root).resolve()
    aie = Path(aie).resolve()
    icl_dir = Path(icl_dir).resolve() if icl_dir else DEFAULT_ICL_DIR

    for key in {(s["env"], s["variant"]) for s in specs.values()}:
        env, cfg_name = ENV_CONFIGS[key]
        p = aie / "configs" / env / cfg_name
        if not p.exists():
            raise FileNotFoundError(f"missing environment config {p}")
    for s in specs.values():
        if s.get("icl") and not (icl_dir / f"{s['icl']}.json").exists():
            raise FileNotFoundError(f"missing icl block {icl_dir / (s['icl'] + '.json')}")
        if s.get("suffix") and not (SUFFIX_DIR / s["suffix"]).exists():
            raise FileNotFoundError(f"missing prompt suffix {SUFFIX_DIR / s['suffix']}")

    has_slot = "{slot}" in workspace
    if concurrency > 1 and not has_slot:
        raise ValueError("concurrency > 1 with a fixed workspace path; every rollout "
                         "re-stages its workspace, so pass e.g. --workspace '/agent-{slot}'")
    if concurrency <= 1 and has_slot:
        workspace = workspace.replace("{slot}", "00")

    n_for = {"pilot": n_pilot, "main": n, "pc": n_pc, "debug": n_debug}

    jobs = []
    for cell, spec in specs.items():
        cell_dir = f"{cell}__{tag}" if tag else cell
        cfg_path = build_cell_config(
            aie, root / "configs" / f"{cell_dir}.yaml", cell_dir, spec, served_model
        )
        for i in range(1, n_for[spec["group"]] + 1):
            run_dir = root / "runs" / cell_dir / f"run-{i:02d}"
            if (run_dir / "run_meta.json").exists() and not force:
                print(f"[skip] {cell_dir}/run-{i:02d} already complete", flush=True)
                continue
            cmd = [
                sys.executable, "-m", "persona_redteaming.envs.rollout",
                "--run-dir", str(run_dir),
                "--arm", spec["arm"],
                "--env", spec["env"],
                "--variant", spec["variant"],
                "--config", str(cfg_path),
                "--aie", str(aie),
                "--base-url", base_url,
                "--served-model", served_model,
                "--tokenizer-model", tokenizer_model,
                "--seed", str(seed_base + i),
                "--temperature", str(temperature),
                "--top-p", str(top_p),
                "--max-tokens", str(max_tokens),
                "--wallclock-cap-s", str(wallclock_cap_s),
                "--workspace", workspace,
            ]
            if model_revision:
                cmd += ["--model-revision", model_revision]
            if spec.get("icl"):
                cmd += ["--icl-block", str(icl_dir / f"{spec['icl']}.json"),
                        "--icl-placement", spec["placement"]]
            if spec.get("suffix"):
                cmd += ["--prompt-suffix-file", str(SUFFIX_DIR / spec["suffix"])]
            if harness_root:
                cmd += ["--harness-root", harness_root]
            # Cell-level rollout arguments come LAST so they win over the
            # campaign defaults (argparse keeps the final occurrence). A bare
            # suffix filename is resolved against the package's directory.
            extra = list(spec.get("rollout_args") or [])
            for k, v in enumerate(extra):
                if k and extra[k - 1] == "--prompt-suffix-file" and "/" not in v:
                    extra[k] = str(SUFFIX_DIR / v)
            cmd += extra
            jobs.append((cell_dir, i, cmd, root / "logs" / cell_dir / f"run-{i:02d}.log"))

    print(f"[campaign] {len(jobs)} rollouts over {len(specs)} cells, "
          f"concurrency={concurrency}", flush=True)
    t0 = time.time()
    results = []
    if concurrency <= 1:
        for cell, i, cmd, log in jobs:
            ts = time.time()
            rc, _ = run_one(cmd, log)
            results.append((cell, i, rc, round(time.time() - ts, 1)))
            print(f"[{'ok' if rc == 0 else 'FAIL'}] {cell}/run-{i:02d} rc={rc} "
                  f"{time.time() - ts:.0f}s ({len(results)}/{len(jobs)})", flush=True)
    else:
        # One workspace slot per worker, held for the life of a rollout, so at
        # most `concurrency` workspaces exist and no two live rollouts share one.
        slots: queue.Queue[str] = queue.Queue()
        for s in range(concurrency):
            slots.put(f"{s:02d}")

        def run_with_slot(cmd: list[str], log: Path) -> tuple[int, str]:
            slot = slots.get()
            try:
                return run_one([c.replace("{slot}", slot) for c in cmd], log)
            finally:
                slots.put(slot)

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {}
            for cell, i, cmd, log in jobs:
                futs[ex.submit(run_with_slot, cmd, log)] = (cell, i, time.time())
            for fut in as_completed(futs):
                cell, i, ts = futs[fut]
                rc, _ = fut.result()
                results.append((cell, i, rc, round(time.time() - ts, 1)))
                print(f"[{'ok' if rc == 0 else 'FAIL'}] {cell}/run-{i:02d} rc={rc} "
                      f"({len(results)}/{len(jobs)})", flush=True)

    summary = {
        "elapsed_s": round(time.time() - t0, 1),
        "n_jobs": len(jobs),
        # rc 3 = ran out of wall clock (transcript usable), rc 4 = crashed.
        "n_failed": sum(1 for _, _, rc, _ in results if rc not in (0, 3)),
        "n_wallclock_timeout": sum(1 for _, _, rc, _ in results if rc == 3),
        "results": [{"cell": c, "run": i, "rc": rc, "elapsed_s": el}
                    for c, i, rc, el in sorted(results)],
        "params": {
            "base_url": base_url,
            "served_model": served_model,
            "tokenizer_model": tokenizer_model,
            "model_revision": model_revision,
            "n": n, "n_pilot": n_pilot, "n_pc": n_pc, "n_debug": n_debug,
            "seed_base": seed_base,
            "temperature": temperature, "top_p": top_p,
            "max_tokens": max_tokens,
            "em_construction": em_construction,
            "tag": tag,
        },
        "cells": specs,
    }
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"campaign_summary_{'_'.join(sorted(specs))[:80]}.json"
    out.write_text(json.dumps(summary, indent=1))
    (root / "campaign_summary.json").write_text(json.dumps(summary, indent=1))
    print(f"[campaign] done in {summary['elapsed_s']}s, "
          f"{summary['n_failed']} failed -> {out}", flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="campaign output root")
    ap.add_argument("--aie", required=True, help="agent-interp-envs checkout")
    ap.add_argument("--icl-dir", default=None,
                    help="directory of icl blocks (default: the package's)")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--served-model", required=True)
    ap.add_argument("--tokenizer-model", required=True)
    ap.add_argument("--model-revision", default=None)
    ap.add_argument("--cells", required=True,
                    help="comma-separated cell names, or a group name "
                         "(pilot|main|pc|debug|all)")
    ap.add_argument("--em-construction", default=None,
                    help="which in-context construction the main EM arms use; "
                         "required whenever a cell whose icl is PICKED runs")
    ap.add_argument("--n", type=int, default=10, help="rollouts per main cell")
    ap.add_argument("--n-pilot", type=int, default=4, help="rollouts per pilot cell")
    ap.add_argument("--n-pc", type=int, default=5, help="rollouts per control cell")
    ap.add_argument("--n-debug", type=int, default=5, help="rollouts per debug cell")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--seed-base", type=int, default=100)
    ap.add_argument("--wallclock-cap-s", type=float, default=1200.0)
    ap.add_argument("--temperature", type=float, default=0.6)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--workspace", default="/agent",
                    help="workspace root. Concurrency > 1 requires a '{slot}' "
                         "placeholder (e.g. /agent-{slot}).")
    ap.add_argument("--harness-root", default=None)
    ap.add_argument("--tag", default=None,
                    help="suffix appended to the run cell directory, e.g. 'rep'")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    try:
        specs = resolve_cells(args.cells, args.em_construction)
        summary = run_campaign(
            specs, root=Path(args.root), aie=Path(args.aie),
            served_model=args.served_model, tokenizer_model=args.tokenizer_model,
            base_url=args.base_url, icl_dir=args.icl_dir,
            model_revision=args.model_revision, n=args.n, n_pilot=args.n_pilot,
            n_pc=args.n_pc, n_debug=args.n_debug, seed_base=args.seed_base,
            concurrency=args.concurrency, wallclock_cap_s=args.wallclock_cap_s,
            temperature=args.temperature, top_p=args.top_p,
            max_tokens=args.max_tokens, workspace=args.workspace,
            harness_root=args.harness_root, tag=args.tag, force=args.force,
            em_construction=args.em_construction,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0 if summary["n_failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
