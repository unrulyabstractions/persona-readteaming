"""Unit tests for the gate: per-token max/p99 scoring, not step means."""

import numpy as np
import torch

from persona_redteaming.replay.gate import GateConfig, gate_record, replay_completion_logprobs


def cfg(**kw):
    c = GateConfig()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_pass_within_budget():
    lp = np.full(100, -1.0)
    logged = lp + 0.1
    g = gate_record(lp, np.ones(100, bool), list(logged), cfg())
    assert g.passed and g.delta_max is not None and g.delta_max < 0.2


def test_single_token_spike_trips_max_not_mean():
    """The measured requirement: one corrupted token must trip the gate even
    though the step MEAN moves by only 0.05 (inside correct-replay noise)."""
    lp = np.full(100, -1.0)
    logged = lp.copy()
    logged[42] -= 5.0  # one wrong-context token: LOUD at the site
    g = gate_record(lp, np.ones(100, bool), list(logged), cfg())
    assert not g.passed
    assert any("per_token_delta_max" in f for f in g.failed_checks)
    assert g.delta_argmax_token == 42
    assert abs(np.abs(lp - logged).mean()) < 0.06  # a mean gate would sleep


def test_p99_trips_on_wide_drift():
    lp = np.full(200, -1.0)
    logged = lp - 0.45  # everywhere above p99 budget, below max budget of 0.5
    g = gate_record(lp, np.ones(200, bool), list(logged), cfg())
    assert not g.passed
    assert any("per_token_delta_p99" in f for f in g.failed_checks)


def test_greedy_agreement_floor():
    lp = np.full(50, -1.0)
    greedy = np.zeros(50, bool)
    g = gate_record(lp, greedy, None, cfg())
    assert not g.passed
    assert any("greedy_agreement" in f for f in g.failed_checks)


def test_logged_length_mismatch_fails():
    lp = np.full(10, -1.0)
    g = gate_record(lp, np.ones(10, bool), [-1.0] * 9, cfg())
    assert not g.passed
    assert any("length-mismatch" in f for f in g.failed_checks)


def test_empty_completion_fails():
    g = gate_record(np.zeros(0), np.zeros(0, bool), None, cfg())
    assert not g.passed and g.failed_checks == ["empty-completion"]


def test_replay_logprobs_lcp_slicing_consistent():
    """Slicing with lcp_used > 0 must give the same values as lcp_used = 0."""
    torch.manual_seed(0)
    vocab, prompt_len, comp = 11, 5, 4
    ids = torch.randint(0, vocab, (prompt_len + comp,)).tolist()
    full_logits = torch.randn(len(ids), vocab, dtype=torch.float64)
    lcp = prompt_len - 1
    lp_full, gr_full = replay_completion_logprobs(full_logits, ids, prompt_len, 0)
    lp_sfx, gr_sfx = replay_completion_logprobs(
        full_logits[lcp:], ids, prompt_len, lcp
    )
    assert np.allclose(lp_full, lp_sfx) and (gr_full == gr_sfx).all()
    assert lp_full.size == comp
