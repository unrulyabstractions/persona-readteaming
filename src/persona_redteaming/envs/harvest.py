"""Teacher-forced replay harvest for every run, one model load per weight set.

Per-sentence mean residual-stream activations come from replaying each genlog
record separately, cropping the KV cache to the longest common token prefix.
A single forward pass over the final transcript would be wrong: Qwen3's chat
template rewrites history (it strips prior-turn reasoning once a new user turn
appears, which `wall_off_told_user` can trigger mid-rollout), so the tokens the
model actually saw at step k are not a prefix of the final transcript.

Every cell of this campaign ran on BASE weights. There is no adapter, so the
replay loads exactly the checkpoint vLLM served and the only engine gap the
gate has to measure is vLLM-vs-transformers on the same weights.

For DeepSeek-R1-Distill-Qwen the harvest tokenizer is patched with the same
corrected chat template the rollout used
(`agent_interp_envs.providers.r1_distill_template`). The harvester
refuses any record whose `template_sha256` differs from the harvest tokenizer's
template, so skipping the patch would refuse every R1 record.

Usage:
  python -m persona_redteaming.envs.harvest --root ROOT --cells a,b \
      --base-model unsloth/Qwen3-32B [--revision SHA] [--layer-chunks 4]

Activation memory is the binding constraint: all 64 layers over a long suffix
at 5120 hidden is gigabytes on top of 65 GB of weights. `--layer-chunks`
splits the layer set across passes and merges the resulting stores, which
changes nothing about the vectors (each layer's file is written by exactly one
pass) but caps peak memory.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
import time
from pathlib import Path


class _GateArgs:
    def __init__(self, acts_dir: str) -> None:
        self.acts_dir = acts_dir


def load_weights(base_model: str, device: str, dtype: str,
                 revision: str | None):
    """The served checkpoint, with the campaign's template correction applied."""
    import torch
    from agent_interp_envs.providers.r1_distill_template import patch_tokenizer
    from transformers import AutoModelForCausalLM, AutoTokenizer

    dtypes = {"bfloat16": torch.bfloat16, "float32": torch.float32}
    tok = AutoTokenizer.from_pretrained(base_model, revision=revision)
    patched = patch_tokenizer(tok, base_model)
    if patched:
        print(f"[harvest] R1 template correction applied: {patched}", flush=True)
    # device_map streams the shards straight onto the target device. Loading
    # to CPU first and then moving would need ~65 GB of host RAM for a 32B
    # checkpoint, which most rented boxes do not have.
    model = AutoModelForCausalLM.from_pretrained(
        base_model, revision=revision, dtype=dtypes[dtype],
        attn_implementation="sdpa", device_map={"": device},
    )
    model.eval()
    return tok, model


def _layer_chunks(n_layers: int, n_chunks: int) -> list[list[int]]:
    if n_chunks <= 1:
        return [list(range(n_layers))]
    size = (n_layers + n_chunks - 1) // n_chunks
    return [list(range(i, min(i + size, n_layers)))
            for i in range(0, n_layers, size)]


def _merge_chunk_stores(chunk_dirs: list[Path], out_dir: Path) -> dict:
    """Fold layer-disjoint stores into one, asserting they agree on records."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifests = [json.loads((d / "manifest.json").read_text()) for d in chunk_dirs]
    base = manifests[0]
    for m in manifests[1:]:
        if m["records"] != base["records"]:
            raise RuntimeError(
                "layer chunks disagree on per-record replay results; the "
                "replay is not deterministic across passes and the store "
                "cannot be merged"
            )
        if m["genlog_sha256"] != base["genlog_sha256"]:
            raise RuntimeError("layer chunks replayed different genlogs")
    files: dict[str, str] = {}
    layers: list[int] = []
    for d, m in zip(chunk_dirs, manifests):
        layers += m["harvest_params"]["layers"]
        for fname, sha in m["files"].items():
            if fname == "manifest.json":
                continue
            if fname in files:
                raise RuntimeError(f"layer file {fname} written by two chunks")
            shutil.copy(d / fname, out_dir / fname)
            files[fname] = sha
    merged = dict(base)
    merged["harvest_params"] = dict(base["harvest_params"])
    merged["harvest_params"]["layers"] = sorted(set(layers))
    merged["harvest_params"]["layer_chunks"] = [
        m["harvest_params"]["layers"] for m in manifests
    ]
    merged["files"] = files
    import hashlib

    payload = json.dumps(merged, indent=1, sort_keys=True,
                         ensure_ascii=False).encode("utf-8")
    (out_dir / "manifest.json").write_bytes(payload)
    files["manifest.json"] = hashlib.sha256(payload).hexdigest()
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="campaign root (holds runs/)")
    ap.add_argument("--cells", required=True, help="comma-separated cell names")
    ap.add_argument("--base-model", required=True)
    ap.add_argument("--revision", default=None)
    ap.add_argument("--gate-config", default=None)
    ap.add_argument("--device", default=None)
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--layer-chunks", type=int, default=1)
    ap.add_argument("--units", default="sentence,message")
    ap.add_argument("--verify-first", action="store_true",
                    help="LCP-vs-fresh-forward verification on the first run "
                         "(a per-(model,stack) property; ~2x compute)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    from persona_redteaming.replay.capture import get_decoder_layers
    from persona_redteaming.replay.cli import cmd_gate_report
    from persona_redteaming.replay.gate import GateConfig
    from persona_redteaming.replay.harvest import harvest

    root = Path(args.root).resolve()
    gate_cfg = GateConfig.from_json(args.gate_config) if args.gate_config else None
    device = args.device
    if device is None:
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"

    t_load = time.time()
    tok, model = load_weights(args.base_model, device, args.dtype, args.revision)
    n_layers = len(get_decoder_layers(model))
    chunks = _layer_chunks(n_layers, args.layer_chunks)
    print(f"[harvest] weights on {device} in {time.time() - t_load:.0f}s; "
          f"{n_layers} decoder layers; layer chunks={len(chunks)}",
          flush=True)

    units = [u.strip() for u in args.units.split(",") if u.strip()]
    results = []
    first = True
    for arm in [a.strip() for a in args.cells.split(",") if a.strip()]:
        for run_dir in sorted((root / "runs" / arm).glob("run-*")):
            if not (run_dir / "generations.jsonl").exists():
                print(f"[skip] {arm}/{run_dir.name}: no genlog", flush=True)
                results.append({"cell": arm, "run": run_dir.name,
                                "verdict": "NO_GENLOG"})
                continue
            acts = run_dir / "acts"
            if (acts / "manifest.json").exists() and not args.force:
                print(f"[skip] {arm}/{run_dir.name}: already harvested",
                      flush=True)
                continue
            t0 = time.time()
            verify = args.verify_first and first
            first = False
            try:
                chunk_dirs = []
                for ci, layer_set in enumerate(chunks):
                    out = acts if len(chunks) == 1 else run_dir / f"acts_chunk{ci}"
                    res = harvest(
                        run_dir, args.base_model, layer_set, units,
                        out_dir=out, device=device, dtype=args.dtype,
                        revision=args.revision, gate_cfg=gate_cfg,
                        verify_lcp=verify and ci == 0, model=model, tok=tok,
                    )
                    chunk_dirs.append(Path(res.out_dir))
                if len(chunks) > 1:
                    _merge_chunk_stores(chunk_dirs, acts)
                    for d in chunk_dirs:
                        shutil.rmtree(d)
            except Exception as exc:
                print(f"[FAIL] {arm}/{run_dir.name}: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                results.append({"cell": arm, "run": run_dir.name,
                                "verdict": "HARVEST_ERROR",
                                "error": f"{type(exc).__name__}: {exc}"})
                continue
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_gate_report(_GateArgs(str(acts)))
            (run_dir / "gate_report.txt").write_text(buf.getvalue())
            m = json.loads((acts / "manifest.json").read_text())
            t = m["totals"]
            verdict = "PASS" if rc == 0 else "FAIL"
            results.append({"cell": arm, "run": run_dir.name,
                            "verdict": verdict, "totals": t,
                            "elapsed_s": round(time.time() - t0, 1)})
            print(f"[{verdict}] {arm}/{run_dir.name}: "
                  f"harvested {t['n_harvested']}/{t['n_records']} "
                  f"refused={t['n_refused']} gate_failed={t['n_gate_failed']} "
                  f"savings={t['savings_fraction']} "
                  f"({time.time() - t0:.0f}s)"
                  + (" [lcp-verified]" if verify else ""), flush=True)

    summary_path = (root /
                    f"harvest_summary_{'_'.join(args.cells.split(','))[:80]}.json")
    summary_path.write_text(json.dumps(results, indent=1))
    n_bad = sum(1 for r in results if r["verdict"] != "PASS")
    print(f"[harvest] {len(results)} runs, {n_bad} not PASS -> {summary_path}",
          flush=True)
    return 0 if n_bad == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
