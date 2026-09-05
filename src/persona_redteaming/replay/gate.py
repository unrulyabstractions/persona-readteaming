"""Per-record replay gate: teacher-forced logprob checks.

Statistics per record (over the completion tokens):
- mean teacher-forced logprob of the logged completion under replay;
- greedy-agreement rate: fraction of completion positions where the replay
  argmax equals the logged token;
- when the genlog carries `completion_logprobs`: per-token
  |replay - generation| logprob deltas, scored as MAX and P99 — NOT step
  means. Measured requirement (temp/12 round 1, exp 1b + exp 5): a single
  wrong context token or a far template drift is INVISIBLE to a step-mean
  gate but detected by per-token max; gates must score per-token max/p99.

Default thresholds are labeled same-engine-dense-MPS: they come from the
wave-1 measurements on dense Qwen3-0.6B, same engine (HF transformers both
sides), MPS bf16 — n=4 worst observed per-token delta 0.239, budget ~0.3
nats with margin. They are an existence bound for this stack ONLY and are
MARKED FOR ON-STACK RECALIBRATION: the production envelope (cross-engine
vLLM-vs-HF, CUDA, MoE) must be calibrated on the harvest box per W3-R6 and
these defaults replaced with measured p99 x 2-3.

All logprobs are computed in float64 on CPU (metric convention lifted from
workspace/replay-mini/common.py: comparisons add no noise of their own).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import torch

CALIBRATION_LABEL = (
    "same-engine-dense-MPS (Qwen3-0.6B wave-1 measurements, temp/12 exp2/2b: "
    "n=4 worst per-token delta 0.239; RECALIBRATE ON-STACK per W3-R6 before "
    "any cross-engine/CUDA/MoE harvest)"
)


@dataclass
class GateConfig:
    """Thresholds; None disables a check."""

    calibration_label: str = CALIBRATION_LABEL
    # per-token |replay - generation| logprob delta (needs logged logprobs)
    per_token_delta_max: float | None = 0.5  # measured worst correct 0.239 (n=4, N=200)
    per_token_delta_p99: float | None = 0.3
    # absolute checks (work without logged logprobs). min_mean_logprob is
    # OFF by default: an absolute floor punishes legitimately-surprising
    # text (the drop relative to calibration is the signal, not absolute
    # values — docs/activation-pipeline.md gate design); enable per-campaign.
    min_mean_logprob: float | None = None
    min_greedy_agreement: float | None = 0.25

    @classmethod
    def from_json(cls, path: str | Path) -> "GateConfig":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cfg = cls()
        unknown = set(data) - set(asdict(cfg))
        if unknown:
            raise ValueError(f"unknown gate config keys: {sorted(unknown)}")
        for k, v in data.items():
            setattr(cfg, k, v)
        return cfg


@dataclass
class GateResult:
    n_tokens: int
    mean_logprob: float
    greedy_agreement: float
    delta_mean: float | None  # vs logged generation logprobs, when present
    delta_p99: float | None
    delta_max: float | None
    delta_argmax_token: int | None  # completion-relative position of the max delta
    passed: bool
    failed_checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def replay_completion_logprobs(
    logits_suffix: torch.Tensor,
    ids: list[int],
    prompt_len: int,
    lcp_used: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Teacher-forced logprobs of the completion tokens, plus greedy match.

    logits_suffix: [suffix_len, vocab] logits for positions lcp_used..len-1.
    Completion token j (global position prompt_len + j) is predicted by
    logits at global position prompt_len + j - 1; the scheduler's LCP cap
    (lcp_used <= prompt_len - 1) guarantees that row was recomputed.

    Returns (logprobs float64 [n_completion], greedy_match bool [n_completion]).
    """
    n = len(ids) - prompt_len
    if n <= 0:
        return np.zeros(0), np.zeros(0, dtype=bool)
    assert lcp_used <= prompt_len - 1, (lcp_used, prompt_len)
    lsm = torch.log_softmax(logits_suffix.detach().cpu().to(torch.float64), dim=-1)
    rows = torch.arange(prompt_len - 1, len(ids) - 1) - lcp_used
    targets = torch.tensor(ids[prompt_len:], dtype=torch.long)
    lps = lsm[rows, targets].numpy()
    greedy = (lsm[rows].argmax(dim=-1) == targets).numpy()
    return lps.astype(np.float64), greedy


def gate_record(
    replay_lp: np.ndarray,
    greedy_match: np.ndarray,
    logged_lp: list[float] | None,
    cfg: GateConfig,
) -> GateResult:
    n = int(replay_lp.size)
    if n == 0:
        return GateResult(
            n_tokens=0,
            mean_logprob=0.0,
            greedy_agreement=1.0,
            delta_mean=None,
            delta_p99=None,
            delta_max=None,
            delta_argmax_token=None,
            passed=False,
            failed_checks=["empty-completion"],
        )
    mean_lp = float(replay_lp.mean())
    greedy = float(np.asarray(greedy_match, dtype=np.float64).mean())

    delta_mean = delta_p99 = delta_max = None
    delta_argmax = None
    failed: list[str] = []

    if logged_lp is not None:
        logged = np.asarray(logged_lp, dtype=np.float64)
        if logged.size != n:
            failed.append(
                f"logged-logprob-length-mismatch ({logged.size} vs {n})"
            )
        else:
            d = np.abs(replay_lp - logged)
            delta_mean = float(d.mean())
            delta_p99 = float(np.percentile(d, 99))
            delta_max = float(d.max())
            delta_argmax = int(d.argmax())
            if cfg.per_token_delta_max is not None and delta_max > cfg.per_token_delta_max:
                failed.append(
                    f"per_token_delta_max {delta_max:.4f} > {cfg.per_token_delta_max}"
                )
            if cfg.per_token_delta_p99 is not None and delta_p99 > cfg.per_token_delta_p99:
                failed.append(
                    f"per_token_delta_p99 {delta_p99:.4f} > {cfg.per_token_delta_p99}"
                )

    if cfg.min_mean_logprob is not None and mean_lp < cfg.min_mean_logprob:
        failed.append(f"mean_logprob {mean_lp:.4f} < {cfg.min_mean_logprob}")
    if cfg.min_greedy_agreement is not None and greedy < cfg.min_greedy_agreement:
        failed.append(
            f"greedy_agreement {greedy:.4f} < {cfg.min_greedy_agreement}"
        )

    return GateResult(
        n_tokens=n,
        mean_logprob=mean_lp,
        greedy_agreement=greedy,
        delta_mean=delta_mean,
        delta_p99=delta_p99,
        delta_max=delta_max,
        delta_argmax_token=delta_argmax,
        passed=not failed,
        failed_checks=failed,
    )
