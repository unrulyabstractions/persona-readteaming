"""Residual-stream capture via forward hooks, and span selectors.

Layer convention: `layer L` (0-based) means the OUTPUT of decoder block L,
i.e. the residual stream after that block — identical to
`output_hidden_states=True`'s hidden_states[L+1] for every non-final layer.
We hook the block modules directly so only the configured subset is ever
materialized. Note the final entry of `output_hidden_states` is post-final-
norm while a hook on the last block is pre-norm; every consumer of this
package (including the E2E bitwise test's reference path) goes through
these hooks, so the convention is uniform.

Span selectors (all spans live inside the record's own completion — the
assistant emission being replayed):
- `sentence`: per-sentence mean vectors; sentences from a regex splitter over
  the raw completion text, mapped to token positions via the tokenizer's
  offset mapping (fast path) or an incremental-decode fallback.
- `message`: one mean vector over all completion tokens.
- `last`: the raw residual vector at the final completion token.
- `tokens`: raw residual vectors for every completion token (opt-in; the
  full per-token dump of docs/activation-pipeline.md's curated tier).

Mean vectors are computed in float32 and stored float32; raw vectors are
stored in the harvest dtype (bf16 by default).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import torch

VALID_UNITS = ("sentence", "message", "last", "tokens")

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[\s　]+|\n+")


def get_decoder_layers(model) -> list[torch.nn.Module]:
    """Locate the decoder block ModuleList for a causal LM."""
    for path in ("model.layers", "transformer.h", "gpt_neox.layers"):
        obj = model
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
        except AttributeError:
            continue
        if isinstance(obj, torch.nn.ModuleList):
            return list(obj)
    raise ValueError(f"cannot locate decoder layers on {type(model).__name__}")


class Capture:
    """Forward hooks on a configurable decoder-layer subset.

    Usage:
        cap = Capture(model, layers=[7, 14, 21])
        with cap:
            model(...)
        states = cap.take()   # {layer: [seq_chunk, H] tensor on device}
    """

    def __init__(self, model, layers: list[int]):
        blocks = get_decoder_layers(model)
        n = len(blocks)
        bad = [L for L in layers if not (0 <= L < n)]
        if bad:
            raise ValueError(f"layers {bad} out of range for {n} decoder blocks")
        self.layers = sorted(set(layers))
        self._blocks = {L: blocks[L] for L in self.layers}
        self._handles: list = []
        self._buf: dict[int, torch.Tensor] = {}

    def _hook(self, layer: int):
        def fn(_module, _inputs, output):
            hidden = output[0] if isinstance(output, tuple) else output
            # [batch, seq_chunk, H] -> keep batch row 0 (harvest is batch=1)
            self._buf[layer] = hidden.detach()[0]

        return fn

    def __enter__(self):
        for L, block in self._blocks.items():
            self._handles.append(block.register_forward_hook(self._hook(L)))
        return self

    def __exit__(self, *exc):
        for h in self._handles:
            h.remove()
        self._handles.clear()
        return False

    def take(self) -> dict[int, torch.Tensor]:
        out = self._buf
        self._buf = {}
        return out


@dataclass
class Span:
    """One span inside a record's completion, in completion-relative units."""

    kind: str  # sentence | message | last | tokens
    idx: int  # ordinal within kind
    tok_start: int  # completion-relative token range [tok_start, tok_end)
    tok_end: int
    char_start: int  # char range into the replayed completion text
    char_end: int


def completion_char_offsets(tokenizer, ids: list[int], text: str) -> list[tuple[int, int]]:
    """Per-token (char_start, char_end) into `text` for completion `ids`.

    Fast path: re-encode `text` with offsets and use them iff the ids round-
    trip exactly. Fallback (ids that do not round-trip through text, e.g.
    inverted-verified byte-fallback cases): incremental decode — offsets[i]
    ends at len(decode(ids[:i+1])) of the DECODED text, and `text` is
    replaced by that decode upstream by the caller when it differs.
    """
    enc = tokenizer(
        text, add_special_tokens=False, return_offsets_mapping=True
    )
    if list(enc["input_ids"]) == list(ids):
        return [tuple(o) for o in enc["offset_mapping"]]
    offsets = []
    prev = 0
    for i in range(len(ids)):
        cur = len(tokenizer.decode(ids[: i + 1]))
        offsets.append((prev, cur))
        prev = cur
    return offsets


def sentence_char_spans(text: str) -> list[tuple[int, int]]:
    """Char spans of sentences (non-empty, whitespace-trimmed)."""
    spans = []
    pos = 0
    for m in _SENTENCE_BREAK.finditer(text):
        seg = text[pos : m.start()]
        if seg.strip():
            spans.append((pos, m.start()))
        pos = m.end()
    if text[pos:].strip():
        spans.append((pos, len(text)))
    return spans


def build_spans(
    units: list[str],
    tokenizer,
    completion_ids: list[int],
    completion_text: str,
) -> tuple[list[Span], str]:
    """Build span specs for one record. Returns (spans, text_used).

    `text_used` is the text the char offsets index into: the raw logged text
    when the ids round-trip to it, else the decode of the ids (flagged by
    the caller via comparison).
    """
    for u in units:
        if u not in VALID_UNITS:
            raise ValueError(f"unknown unit {u!r}; valid: {VALID_UNITS}")
    n = len(completion_ids)
    if n == 0:
        return [], completion_text

    decoded = tokenizer.decode(completion_ids)
    text_used = completion_text if decoded == completion_text else decoded
    offsets = completion_char_offsets(tokenizer, completion_ids, text_used)

    spans: list[Span] = []
    if "message" in units:
        spans.append(Span("message", 0, 0, n, 0, len(text_used)))
    if "last" in units:
        spans.append(Span("last", 0, n - 1, n, offsets[-1][0], offsets[-1][1]))
    if "sentence" in units:
        for si, (cs, ce) in enumerate(sentence_char_spans(text_used)):
            toks = [
                i
                for i, (a, b) in enumerate(offsets)
                if b > cs and a < ce and a != b
            ]
            if not toks:
                continue
            spans.append(Span("sentence", si, toks[0], toks[-1] + 1, cs, ce))
    if "tokens" in units:
        for i, (a, b) in enumerate(offsets):
            spans.append(Span("tokens", i, i, i + 1, a, b))
    return spans, text_used


def span_vector(states: torch.Tensor, span: Span) -> torch.Tensor:
    """Extract one span's vector from completion-relative states [n_tok, H].

    Means (sentence/message) in float32; raw vectors (last/tokens) keep the
    harvest dtype.
    """
    chunk = states[span.tok_start : span.tok_end]
    if span.kind in ("sentence", "message"):
        return chunk.to(torch.float32).mean(dim=0)
    assert chunk.shape[0] == 1, (span.kind, chunk.shape)  # last/tokens: 1 position
    return chunk[0]
