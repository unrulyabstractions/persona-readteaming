"""End-to-end tests: fixture genlog -> harvest -> verify/gate/store.

Coverage mandated by the workstream-3 brief:
- genlog validates against the schema (fixture self-check + here);
- LCP-path activations vs full per-step recompute: bitwise identity is
  SHAPE-dependent, not just model-dependent (temp/53 review measured 2/6
  cross-run 0.6B records non-bitwise with lp-delta-vs-fresh up to ~0.49
  nats), so the assertion here is the portable one: full-length passes
  (lcp_used == 0) must be bitwise, cache-reuse passes must sit within the
  measured stack envelope (D9);
- gate GREEN on the honest replay, gate RED on deliberate corruption
  (wrong template sha -> refusal; shuffled completion ids -> gate failure),
  both directions;
- determinism: re-harvest produces identical manifest + layer file hashes.
"""

import json
import random
import shutil
from pathlib import Path

import pytest
import torch

from persona_redteaming.replay.harvest import harvest
from persona_redteaming.replay.loader import load_genlog
from persona_redteaming.replay.store import load_layer, span_key

from conftest import LAYERS, MODEL, UNITS, gate_cfg_for

# MEASURED honest-replay LCP-vs-fresh envelope on this stack (worst layer
# rel_l2_max 2.7e-2 for 1.7B; 2.6e-2 on some 0.6B shapes per temp/53);
# corruption sits at ~8e-1 (temp/12 exp 3).
STACK_REL_L2_CEILING = 0.08


def corrupt_run(run_dir: Path, tmp_path: Path, mutate) -> Path:
    """Copy the run dir and rewrite generations.jsonl through `mutate`."""
    dst = tmp_path / "corrupted-run"
    shutil.copytree(run_dir, dst)
    lines = []
    with open(dst / "generations.jsonl") as f:
        for line in f:
            obj = json.loads(line)
            lines.append(mutate(obj) or obj)
    with open(dst / "generations.jsonl", "w") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return dst


def harvested(manifest):
    return [r for r in manifest["records"] if r["harvested"]]


# ---------------------------------------------------------------- fixture --


def test_fixture_genlog_validates(run_dir):
    recs = load_genlog(run_dir / "generations.jsonl")
    assert [r.invoke_idx for r in recs] == [0, 1, 2, 3, 4]
    assert [r.resolved_status for r in recs] == [
        "retained",
        "retained",
        "reverted",
        "retained",
        "healed",
    ]
    for r in recs:
        assert r.raw["template_sha256"]
        assert len(r.raw["completion_logprobs"]) == len(r.raw["completion_token_ids"])


# ------------------------------------------------------- honest harvest ---


def test_all_records_harvested_and_flagged(honest):
    m = honest.manifest
    assert m["totals"]["n_records"] == 5
    assert m["totals"]["n_harvested"] == 5  # reverted+healed harvested, flagged
    assert m["totals"]["n_refused"] == 0
    by_idx = {r["invoke_idx"]: r for r in m["records"]}
    assert by_idx[2]["resolved_status"] == "reverted"
    assert by_idx[4]["resolved_status"] == "healed"
    for r in m["records"]:
        for s in r["spans"]:
            assert s["status_flag"] == r["resolved_status"]
            assert s["message_index"] == r["invoke_idx"]


def test_lcp_matches_fresh_recompute(honest):
    """The wave-1 headline check, per record, through identical hooks."""
    for r in harvested(honest.manifest):
        v = r["lcp_verification"]
        if r["lcp_used"] == 0:
            # full-length pass: same shapes as the reference forward, so
            # repeat determinism makes it bitwise on every model (measured)
            assert v["all_layers_bitwise"], r["invoke_idx"]
        # cache-reuse passes: bitwise is shape-dependent even on 0.6B
        # (temp/53 D9) — assert the stack envelope, record bitwise as info
        for L, pl in v["per_layer"].items():
            assert pl["rel_l2_max"] <= STACK_REL_L2_CEILING, (r["invoke_idx"], L, pl)


def test_gate_green_on_honest_replay(honest):
    for r in harvested(honest.manifest):
        assert r["gate"]["passed"], (r["invoke_idx"], r["gate"]["failed_checks"])
    assert honest.manifest["totals"]["n_gate_failed"] == 0


def test_lcp_reuse_and_cap(honest):
    by_idx = {r["invoke_idx"]: r for r in honest.manifest["records"]}
    # invoke 1 extends invoke 0's exact sequence: full reuse of it
    assert by_idx[1]["lcp_requested"] == by_idx[0]["seq_len"]
    # the retry (invoke 3) shares invoke 2's prompt AND completion head:
    # the gate-driven cap must clamp reuse to prompt_len - 1
    r3 = by_idx[3]
    if r3["lcp_requested"] >= r3["prompt_len"]:
        assert r3["lcp_used"] == r3["prompt_len"] - 1
    assert all(
        r["lcp_used"] <= r["prompt_len"] - 1 for r in harvested(honest.manifest)
    )
    # the user follow-up strips prior thinking: LCP collapses mid-prompt
    r4 = by_idx[4]
    assert r4["lcp_used"] < r4["prompt_len"] - 1
    t = honest.manifest["totals"]
    assert t["tokens_processed"] < t["tokens_naive"]
    assert t["savings_fraction"] > 0.3


def test_store_contents_join_back(honest, model_tok, run_dir):
    model, tok = model_tok
    hidden = model.config.hidden_size
    m = honest.manifest
    run_id = m["run_id"]
    recs = {r.invoke_idx: r for r in load_genlog(run_dir / "generations.jsonl")}
    for layer in LAYERS:
        tensors = load_layer(honest.out_dir, layer)
        n_spans = sum(len(r["spans"]) for r in m["records"])
        assert len(tensors) == n_spans
        for r in m["records"]:
            for s in r["spans"]:
                key = span_key(run_id, r["invoke_idx"], s["kind"], s["idx"])
                t = tensors[key]
                assert t.shape == (hidden,)
                if s["kind"] in ("sentence", "message"):
                    assert t.dtype == torch.float32
                else:
                    assert t.dtype == torch.bfloat16
    # span round-trip: token spans decode to exactly the char span text
    rec3 = recs[3]
    text = rec3.completion_text_raw
    r3 = next(r for r in m["records"] if r["invoke_idx"] == 3)
    assert r3["completion_text_matches_ids_decode"]
    tok_spans = [s for s in r3["spans"] if s["kind"] == "tokens"]
    assert len(tok_spans) == r3["n_completion_tokens"]
    for s in tok_spans[:20]:
        a, b = s["tok_span"]
        ca, cb = s["char_span"]
        assert tok.decode(rec3.completion_token_ids[a:b]) == text[ca:cb]
    for s in (x for x in r3["spans"] if x["kind"] == "sentence"):
        ca, cb = s["char_span"]
        a, b = s["tok_span"]
        seg = text[ca:cb].strip()
        dec = tok.decode(rec3.completion_token_ids[a:b])
        assert seg and seg in dec


# ------------------------------------------------------------ corruption --


def test_gate_red_on_shuffled_completion_ids(run_dir, model_tok, tmp_path):
    model, tok = model_tok

    def mutate(obj):
        if obj.get("invoke_idx") == 3:
            rng = random.Random(7)
            ids = list(obj["completion_token_ids"])
            while ids == obj["completion_token_ids"]:
                rng.shuffle(ids)
            obj["completion_token_ids"] = ids

    dst = corrupt_run(run_dir, tmp_path, mutate)
    res = harvest(
        dst, MODEL, [LAYERS[0]], ["message"],
        out_dir=tmp_path / "acts",
        gate_cfg=gate_cfg_for(MODEL), model=model, tok=tok,
    )
    by_idx = {r["invoke_idx"]: r for r in res.manifest["records"]}
    bad = by_idx[3]
    assert bad["harvested"] and not bad["gate"]["passed"]
    # detonation, not marginal failure: shuffled ids are off-distribution
    assert bad["gate"]["delta_max"] > 2.0
    assert bad["gate"]["greedy_agreement"] < 0.5
    for idx in (0, 1, 2):
        assert by_idx[idx]["gate"]["passed"], idx


def test_refused_on_wrong_template_sha(run_dir, model_tok, tmp_path):
    model, tok = model_tok

    def mutate(obj):
        if obj.get("invoke_idx") == 1:
            obj["template_sha256"] = "0" * 64

    dst = corrupt_run(run_dir, tmp_path, mutate)
    res = harvest(
        dst, MODEL, [LAYERS[0]], ["message"],
        out_dir=tmp_path / "acts",
        gate_cfg=gate_cfg_for(MODEL), model=model, tok=tok,
    )
    by_idx = {r["invoke_idx"]: r for r in res.manifest["records"]}
    assert not by_idx[1]["harvested"]
    assert by_idx[1]["refusal"]["reason"] == "template-sha-mismatch"
    assert res.manifest["totals"]["n_refused"] == 1
    assert by_idx[0]["harvested"] and by_idx[2]["harvested"]


def test_refused_on_tripwire_mismatch(run_dir, model_tok, tmp_path):
    model, tok = model_tok

    def mutate(obj):
        if obj.get("invoke_idx") == 0:
            obj["prompt_tokens_server"] = obj["prompt_tokens_server"] + 1

    dst = corrupt_run(run_dir, tmp_path, mutate)
    res = harvest(
        dst, MODEL, [LAYERS[0]], ["message"],
        out_dir=tmp_path / "acts",
        gate_cfg=gate_cfg_for(MODEL), model=model, tok=tok,
    )
    by_idx = {r["invoke_idx"]: r for r in res.manifest["records"]}
    assert not by_idx[0]["harvested"]
    assert by_idx[0]["refusal"]["reason"] == "tripwire-mismatch"


def test_null_ids_refused_unless_text_mode(run_dir, model_tok, tmp_path):
    model, tok = model_tok

    def mutate(obj):
        if obj.get("invoke_idx") == 1:
            obj["completion_token_ids"] = None
            obj["completion_ids_source"] = None
            obj["completion_logprobs"] = None

    dst = corrupt_run(run_dir, tmp_path, mutate)
    res = harvest(
        dst, MODEL, [LAYERS[0]], ["message"],
        out_dir=tmp_path / "acts-tokenmode",
        gate_cfg=gate_cfg_for(MODEL), model=model, tok=tok,
    )
    by_idx = {r["invoke_idx"]: r for r in res.manifest["records"]}
    assert not by_idx[1]["harvested"]
    assert by_idx[1]["refusal"]["reason"] == "null-completion-ids"

    res2 = harvest(
        dst, MODEL, [LAYERS[0]], ["message"],
        out_dir=tmp_path / "acts-textmode", text_mode=True,
        gate_cfg=gate_cfg_for(MODEL), model=model, tok=tok,
    )
    by_idx2 = {r["invoke_idx"]: r for r in res2.manifest["records"]}
    assert by_idx2[1]["harvested"]
    assert by_idx2[1]["fidelity"] == "text"
    assert by_idx2[1]["gate"]["delta_max"] is None  # no logged-lp comparison
    assert all(s["fidelity"] == "text" for s in by_idx2[1]["spans"])


def test_malformed_logprobs_refuse_record_not_run(run_dir, model_tok, tmp_path, capsys):
    """temp/53 F1 blast radius at harvest level + D7 warning: one hf-raw
    dict-shaped record is refused per-record, one null-logprob record
    harvests as PASS* with a visible skipped-delta WARNING; the rest of the
    file harvests and gates normally."""
    model, tok = model_tok

    def mutate(obj):
        if obj.get("invoke_idx") == 1:
            obj["completion_logprobs"] = {
                "tokens": ["x"],
                "token_logprobs": [-0.1],
                "text_offset": [0],
            }
        if obj.get("invoke_idx") == 2:
            obj["completion_logprobs"] = None

    dst = corrupt_run(run_dir, tmp_path, mutate)
    res = harvest(
        dst, MODEL, [LAYERS[0]], ["message"],
        out_dir=tmp_path / "acts",
        gate_cfg=gate_cfg_for(MODEL), model=model, tok=tok,
    )
    by_idx = {r["invoke_idx"]: r for r in res.manifest["records"]}
    assert not by_idx[1]["harvested"]
    assert by_idx[1]["refusal"]["reason"] == "malformed-record"
    assert "flat list" in by_idx[1]["refusal"]["detail"]
    assert by_idx[2]["harvested"] and by_idx[2]["gate"]["passed"]
    assert by_idx[2]["gate"]["delta_max"] is None  # deltas skipped
    for idx in (0, 3, 4):
        assert by_idx[idx]["harvested"] and by_idx[idx]["gate"]["passed"]

    from persona_redteaming.replay.cli import main

    rc = main(["gate-report", str(tmp_path / "acts")])
    out = capsys.readouterr().out
    assert rc == 2  # the refusal keeps the exit code red
    assert "WARNING" in out and "delta checks were SKIPPED" in out
    assert "PASS*" in out


def test_skip_reverted_when_configured(run_dir, model_tok, tmp_path):
    model, tok = model_tok
    res = harvest(
        run_dir, MODEL, [LAYERS[0]], ["message"],
        out_dir=tmp_path / "acts", include_reverted=False,
        gate_cfg=gate_cfg_for(MODEL), model=model, tok=tok,
    )
    by_idx = {r["invoke_idx"]: r for r in res.manifest["records"]}
    assert not by_idx[2]["harvested"]
    assert by_idx[2]["skip_reason"] == "reverted-excluded-by-config"
    assert res.manifest["totals"]["n_skipped"] == 1
    assert res.manifest["totals"]["n_harvested"] == 4


# ----------------------------------------------------------- determinism --


def test_reharvest_is_deterministic(run_dir, model_tok, tmp_path):
    model, tok = model_tok
    kw = dict(gate_cfg=gate_cfg_for(MODEL), model=model, tok=tok)
    a = harvest(run_dir, MODEL, LAYERS, UNITS, out_dir=tmp_path / "a", **kw)
    b = harvest(run_dir, MODEL, LAYERS, UNITS, out_dir=tmp_path / "b", **kw)
    assert a.file_hashes == b.file_hashes  # manifest.json + every layer file
    assert a.file_hashes["manifest.json"] == b.file_hashes["manifest.json"]


# ------------------------------------------------------------------- CLI --


def test_cli_gate_report(honest, capsys):
    from persona_redteaming.replay.cli import main

    rc = main(["gate-report", str(honest.out_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("PASS") == 5
    assert "reverted" in out and "healed" in out
    # every honest record carries logged logprobs: no skipped-delta warning
    assert "WARNING" not in out and "PASS*" not in out
