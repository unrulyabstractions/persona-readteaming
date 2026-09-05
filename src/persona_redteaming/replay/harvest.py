"""Harvest orchestration: genlog -> LCP replay -> capture -> gate -> store."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import torch

from .capture import Capture, build_spans, span_vector
from .gate import GateConfig, gate_record, replay_completion_logprobs
from .loader import (
    GENLOG_FILENAME,
    check_record,
    completion_ids_for_replay,
    load_genlog,
    template_sha256_of,
)
from .scheduler import LcpScheduler
from .store import ActStore

DTYPES = {"bfloat16": torch.bfloat16, "float32": torch.float32}


def default_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def load_model_and_tokenizer(model_id: str, device: str, dtype: str, revision: str | None = None):
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_id, revision=revision)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        revision=revision,
        dtype=DTYPES[dtype],
        attn_implementation="sdpa",
    )
    model = model.to(device)
    model.eval()
    return tok, model


@dataclass
class HarvestResult:
    out_dir: Path
    manifest: dict
    file_hashes: dict[str, str]
    debug: dict = field(default_factory=dict)  # populated when return_debug=True


def harvest(
    run_dir: str | Path,
    model_id: str,
    layers: list[int],
    units: list[str],
    *,
    out_dir: str | Path | None = None,
    device: str | None = None,
    dtype: str = "bfloat16",
    revision: str | None = None,
    text_mode: bool = False,
    include_reverted: bool = True,
    gate_cfg: GateConfig | None = None,
    cache_reuse: bool | None = None,
    verify_lcp: bool = False,
    model=None,
    tok=None,
    return_debug: bool = False,
) -> HarvestResult:
    """Replay one genlog run and write the activation store.

    verify_lcp: after each record's LCP replay, run a fresh full forward
    over the same sequence through identical capture hooks and record, per
    record, whether the LCP-path states are bitwise identical plus the f64
    rel-L2 and logprob-delta envelope over completion positions. MEASURED
    (this repo, 2026-09-04): bitwise holds for Qwen3-0.6B on MPS bf16
    (reproducing temp/12 exp4) but NOT for Qwen3-1.7B, whose forward is
    sequence-length-dependent at bf16 rounding scale even cache-free
    (rel_l2_mean ~1e-2); the W3-R3 "bitwise is a special case, the portable
    criterion is the stack floor" caveat is therefore live PER MODEL on one
    stack, and campaigns must run this verification once per (model, stack).
    """
    run_dir = Path(run_dir)
    if run_dir.is_dir():
        genlog_path, base_dir = run_dir / GENLOG_FILENAME, run_dir
    else:  # a genlog file path was passed directly
        genlog_path, base_dir = run_dir, run_dir.parent
    out_dir = Path(out_dir) if out_dir is not None else base_dir / "acts"
    device = device or default_device()
    gate_cfg = gate_cfg or GateConfig()
    run_id = base_dir.name

    records = load_genlog(genlog_path)
    if model is None or tok is None:
        tok, model = load_model_and_tokenizer(model_id, device, dtype, revision)
    harvest_template_sha = template_sha256_of(tok)

    if layers == "all":
        from .capture import get_decoder_layers

        layers = list(range(len(get_decoder_layers(model))))

    scheduler = LcpScheduler(model, device, layers, cache_reuse=cache_reuse)
    store = ActStore(out_dir, run_id)
    rec_entries: list[dict] = []
    debug: dict = {}
    n_harvested = n_refused = n_skipped = n_gate_passed = 0

    for rec in records:
        entry: dict = {
            "invoke_idx": rec.invoke_idx,
            "status": rec.status,
            "resolved_status": rec.resolved_status,
            "n_amendments": len(rec.amendments),
            "harvested": False,
            "refusal": None,
            "skip_reason": None,
            "fidelity": None,
            "gate": None,
            "spans": [],
        }
        rec_entries.append(entry)

        if rec.resolved_status == "reverted" and not include_reverted:
            entry["skip_reason"] = "reverted-excluded-by-config"
            n_skipped += 1
            continue

        refusal = check_record(
            rec, harvest_template_sha256=harvest_template_sha, text_mode=text_mode
        )
        if refusal is not None:
            entry["refusal"] = {"reason": refusal.reason, "detail": refusal.detail}
            n_refused += 1
            continue

        completion_ids, fidelity = completion_ids_for_replay(
            rec, tok, text_mode=text_mode
        )
        entry["fidelity"] = fidelity
        prompt_ids = list(rec.prompt_token_ids)
        ids = prompt_ids + completion_ids
        prompt_len = len(prompt_ids)

        step = scheduler.replay(ids, prompt_len)

        if verify_lcp:
            entry["lcp_verification"] = _verify_against_fresh(
                model, device, scheduler.capture.layers, ids, prompt_len, step
            )

        replay_lp, greedy = replay_completion_logprobs(
            step.logits, ids, prompt_len, step.lcp_used
        )
        # Logged logprobs align with logged ids only; in text mode our
        # tokenization differs from the server's, so skip the delta check.
        logged_lp = rec.completion_logprobs if fidelity == "ids" else None
        gate = gate_record(replay_lp, greedy, logged_lp, gate_cfg)
        entry["gate"] = gate.to_dict()
        if gate.passed:
            n_gate_passed += 1

        spans, text_used = build_spans(units, tok, completion_ids, rec.completion_text_raw)
        entry["completion_text_matches_ids_decode"] = (
            text_used == rec.completion_text_raw
        )
        # completion-relative states: suffix covers lcp_used..len(ids)-1
        comp_offset = prompt_len - step.lcp_used
        for span in spans:
            for layer in scheduler.capture.layers:
                states = step.states[layer]  # [suffix_len, H]
                vec = span_vector(states[comp_offset:], span)
                store.add(rec.invoke_idx, layer, span.kind, span.idx, vec)
            entry["spans"].append(
                {
                    "kind": span.kind,
                    "idx": span.idx,
                    "message_index": rec.invoke_idx,
                    "char_span": [span.char_start, span.char_end],
                    "tok_span": [span.tok_start, span.tok_end],
                    "n_tokens": span.tok_end - span.tok_start,
                    "status_flag": rec.resolved_status,
                    "fidelity": fidelity,
                }
            )
        entry.update(
            {
                "harvested": True,
                "prompt_len": prompt_len,
                "seq_len": len(ids),
                "n_completion_tokens": len(completion_ids),
                "lcp_requested": step.lcp_requested,
                "lcp_used": step.lcp_used,
                "suffix_processed": step.suffix_len,
                "fresh_forward": step.fresh_forward,
            }
        )
        n_harvested += 1
        if return_debug:
            debug[rec.invoke_idx] = {
                "ids": ids,
                "prompt_len": prompt_len,
                "lcp_used": step.lcp_used,
                "completion_states": {
                    L: step.states[L][comp_offset:].detach().cpu()
                    for L in scheduler.capture.layers
                },
                "replay_lp": replay_lp,
                "greedy": np.asarray(greedy),
                "spans": spans,
            }

    import transformers

    manifest = {
        "run_id": run_id,
        "genlog_sha256": _sha256_file(genlog_path),
        "model": {
            "requested": model_id,
            "loaded_revision": getattr(model.config, "_commit_hash", None),
            "genlog_models": sorted({str(r.raw.get("model")) for r in records}),
            "genlog_model_revisions": sorted(
                {str(r.raw.get("model_revision")) for r in records}
            ),
            "genlog_tokenizer_revisions": sorted(
                {str(r.raw.get("tokenizer_revision")) for r in records}
            ),
        },
        "template_sha256": harvest_template_sha,
        "env": {
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "device": device,
            "dtype": dtype,
            "attn_implementation": "sdpa",
        },
        "harvest_params": {
            "layers": sorted(set(layers)),
            "units": list(units),
            "text_mode": text_mode,
            "include_reverted": include_reverted,
            "cache_reuse": scheduler.cache_reuse,
            "verify_lcp": verify_lcp,
            "gate_config": asdict(gate_cfg),
        },
        "records": rec_entries,
        "totals": {
            "n_records": len(records),
            "n_harvested": n_harvested,
            "n_refused": n_refused,
            "n_skipped": n_skipped,
            "n_gate_passed": n_gate_passed,
            "n_gate_failed": n_harvested - n_gate_passed,
            "tokens_naive": scheduler.tokens_naive,
            "tokens_processed": scheduler.tokens_processed,
            "savings_fraction": (
                round(1 - scheduler.tokens_processed / scheduler.tokens_naive, 6)
                if scheduler.tokens_naive
                else None
            ),
        },
    }
    file_hashes = store.finalize(manifest)
    return HarvestResult(
        out_dir=out_dir, manifest=manifest, file_hashes=file_hashes, debug=debug
    )


@torch.no_grad()
def _verify_against_fresh(model, device, layers, ids, prompt_len, step) -> dict:
    """Fresh full forward through identical hooks; compare the LCP path.

    Comparisons follow the replay-mini conventions: float64 on CPU, rel_l2
    with the fresh forward as reference, logprob deltas over completion
    tokens. Restricted to completion positions (the spans that get stored).
    """
    cap = Capture(model, layers)
    with cap:
        out = model(
            input_ids=torch.tensor([ids], dtype=torch.long, device=device),
            attention_mask=torch.ones((1, len(ids)), dtype=torch.long, device=device),
            use_cache=False,
        )
    fresh_states = cap.take()
    comp_offset = prompt_len - step.lcp_used
    per_layer = {}
    all_bitwise = True
    for L in layers:
        a = step.states[L][comp_offset:]  # LCP path, completion positions
        b = fresh_states[L][prompt_len:]  # fresh full forward, same positions
        bitwise = bool(torch.equal(a, b))
        all_bitwise &= bitwise
        af = a.detach().cpu().to(torch.float64).numpy()
        bf = b.detach().cpu().to(torch.float64).numpy()
        rel = np.linalg.norm(af - bf, axis=-1) / (
            np.linalg.norm(bf, axis=-1) + 1e-30
        )
        per_layer[str(L)] = {
            "bitwise": bitwise,
            "rel_l2_mean": float(rel.mean()),
            "rel_l2_max": float(rel.max()),
        }
    from .gate import replay_completion_logprobs as _lp

    lp_lcp, _ = _lp(step.logits, ids, prompt_len, step.lcp_used)
    lp_fresh, _ = _lp(out.logits[0], ids, prompt_len, 0)
    d = np.abs(lp_lcp - lp_fresh)
    return {
        "all_layers_bitwise": all_bitwise,
        "per_layer": per_layer,
        "logprob_delta_vs_fresh": {
            "mean": float(d.mean()) if d.size else None,
            "p99": float(np.percentile(d, 99)) if d.size else None,
            "max": float(d.max()) if d.size else None,
        },
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
