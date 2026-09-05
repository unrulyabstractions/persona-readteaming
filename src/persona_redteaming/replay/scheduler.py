"""LCP replay scheduler: per-record token sequences in rollout order,
KV-cache reuse via DynamicCache with NEGATIVE crop semantics.

Measured facts this module encodes (temp/12-replay-fidelity.md):
- transformers 5.16.1 `DynamicCache.crop(tokens_to_remove)`: a POSITIVE
  argument silently runs the legacy 4.x "keep this many tokens" semantics
  (removed in 5.18); only a NEGATIVE argument removes |n| tokens from the
  end. We feature-probe the installed transformers at runtime on dummy
  tensors and fall back to fresh full forwards when the probe fails.
- After every crop, `get_seq_length()` MUST be asserted against the intended
  prefix length (the assertion caught the positive-arg trap in the wave-1
  prototype).
- KV-crop LCP replay is bitwise-identical to fresh per-step recompute on
  this stack (MPS, sdpa, torch 2.14 — exp 4); on CUDA "exact" means
  within the stack's same-config floor (W3-R3), not bitwise.

Gate-driven LCP cap: the effective reuse is capped at len(prompt)-1 so the
forward always recomputes from the last prompt position onward. That
guarantees (a) logits exist for every completion token's predicting
position, and (b) every captured span position (all spans live in the
completion) is recomputed under capture hooks. The cap only bites on
retry-shaped records whose prompt equals the previous record's whole
sequence prefix plus part of its completion; the extra cost is those few
shared completion tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

import torch

from .capture import Capture


def lcp_len(a: list[int], b: list[int]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def probe_cache_crop() -> bool:
    """Feature-detect negative-crop DynamicCache semantics on dummy tensors.

    Returns True iff crop(-3) on an 8-token cache leaves exactly 5 tokens.
    Any API difference (missing crop, missing get_seq_length, changed
    semantics) returns False and the scheduler falls back to fresh forwards.
    """
    try:
        from transformers import DynamicCache

        cache = DynamicCache()
        k = torch.zeros(1, 2, 8, 4)
        v = torch.zeros(1, 2, 8, 4)
        cache.update(k, v, 0)
        if cache.get_seq_length() != 8:
            return False
        cache.crop(-3)
        return cache.get_seq_length() == 5
    except Exception:
        return False


@dataclass
class StepResult:
    lcp_requested: int  # LCP with the previous record's sequence
    lcp_used: int  # after the gate-driven cap (reused cache tokens)
    suffix_len: int  # tokens actually forwarded
    logits: torch.Tensor  # [suffix_len, vocab] on device, suffix positions lcp_used..seq-1
    states: dict[int, torch.Tensor]  # layer -> [suffix_len, H] on device
    fresh_forward: bool  # True when the cache path was not used


class LcpScheduler:
    """Replays record sequences in rollout order with KV-cache reuse."""

    def __init__(self, model, device: str, layers: list[int], cache_reuse: bool | None = None):
        self.model = model
        self.device = device
        self.capture = Capture(model, layers)
        if cache_reuse is None:
            cache_reuse = probe_cache_crop()
        self.cache_reuse = cache_reuse
        self._cache = None
        self._cached_ids: list[int] = []
        self.tokens_processed = 0
        self.tokens_naive = 0

    def _fresh_cache(self):
        from transformers import DynamicCache

        return DynamicCache()

    @torch.no_grad()
    def replay(self, ids: list[int], prompt_len: int) -> StepResult:
        """Teacher-force one record's full sequence (prompt + completion).

        Returns logits and captured states for the recomputed suffix
        (positions lcp_used .. len(ids)-1).
        """
        if not (0 < prompt_len <= len(ids)):
            raise ValueError(f"bad prompt_len {prompt_len} for seq of {len(ids)}")
        self.tokens_naive += len(ids)

        if not self.cache_reuse:
            return self._forward(ids, lcp=0, lcp_requested=0, fresh=True)

        lcp_requested = lcp_len(self._cached_ids, ids)
        lcp = min(lcp_requested, prompt_len - 1)
        to_remove = len(self._cached_ids) - lcp
        if self._cache is None:
            self._cache = self._fresh_cache()
        if to_remove > 0:
            # NEGATIVE arg = remove |n| tokens from the end (5.x semantics).
            self._cache.crop(-to_remove)
            got = self._cache.get_seq_length()
            assert got == lcp, (
                f"DynamicCache.crop landed at {got}, wanted {lcp}: crop "
                "semantics differ from the probed negative-arg form"
            )
        return self._forward(ids, lcp=lcp, lcp_requested=lcp_requested, fresh=False)

    def _forward(self, ids: list[int], *, lcp: int, lcp_requested: int, fresh: bool) -> StepResult:
        suffix = ids[lcp:]
        x = torch.tensor([suffix], dtype=torch.long, device=self.device)
        attn = torch.ones((1, len(ids)), dtype=torch.long, device=self.device)
        kwargs = {}
        if not fresh:
            kwargs["past_key_values"] = self._cache
            kwargs["use_cache"] = True
        else:
            kwargs["use_cache"] = False
        with self.capture as cap:
            out = self.model(input_ids=x, attention_mask=attn, **kwargs)
        states = cap.take()
        if not fresh:
            self._cache = out.past_key_values
            assert self._cache.get_seq_length() == len(ids), (
                self._cache.get_seq_length(),
                len(ids),
            )
            self._cached_ids = list(ids)
        self.tokens_processed += len(suffix)
        return StepResult(
            lcp_requested=lcp_requested,
            lcp_used=lcp,
            suffix_len=len(suffix),
            logits=out.logits[0],
            states=states,
            fresh_forward=fresh,
        )
