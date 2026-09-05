"""Shared fixtures for the E2E suite.

REPLAY_E2E_MODEL selects the harvest/backend model (default Qwen/Qwen3-1.7B;
set Qwen/Qwen3-0.6B for a faster run — that model additionally satisfies the
bitwise LCP identity, see test_e2e.py::test_lcp_matches_fresh_recompute).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

MODEL = os.environ.get("REPLAY_E2E_MODEL", "Qwen/Qwen3-1.7B")
LAYERS = [7, 14, 21]
UNITS = ["sentence", "message", "last", "tokens"]
HERE = Path(__file__).resolve().parent


def gate_cfg_for(model: str):
    from persona_redteaming.replay.gate import GateConfig

    if "1.7B" in model:
        # On-stack calibration (MEASURED on this machine; see the file).
        return GateConfig.from_json(HERE / "gate-qwen3-1.7b-mps.json")
    return GateConfig()


@pytest.fixture(scope="session")
def run_dir(tmp_path_factory) -> Path:
    """Synthesize the genlog fixture once per session (subprocess so the
    backend model's memory is released before harvests start)."""
    out = tmp_path_factory.mktemp("fixture-run")
    subprocess.run(
        [sys.executable, str(HERE / "make_fixture.py"), str(out), "--model", MODEL],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (out / "generations.jsonl").is_file()
    return out


@pytest.fixture(scope="session")
def model_tok():
    from persona_redteaming.replay.harvest import default_device, load_model_and_tokenizer

    tok, model = load_model_and_tokenizer(MODEL, default_device(), "bfloat16")
    return model, tok


@pytest.fixture(scope="session")
def honest(run_dir, model_tok, tmp_path_factory):
    """The honest harvest of the fixture, with LCP verification and debug."""
    from persona_redteaming.replay.harvest import harvest

    model, tok = model_tok
    out = tmp_path_factory.mktemp("acts-honest")
    return harvest(
        run_dir,
        MODEL,
        LAYERS,
        UNITS,
        out_dir=out,
        gate_cfg=gate_cfg_for(MODEL),
        verify_lcp=True,
        model=model,
        tok=tok,
        return_debug=True,
    )
