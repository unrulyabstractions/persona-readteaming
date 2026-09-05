#!/usr/bin/env python3
"""Build the in-context EM constructions this campaign compares.

The prior campaign (temp/57) transplanted arXiv 2510.11288's construction
verbatim into an agentic loop and measured the consequence: only 5 of 10
rollouts reached the decision point, and the chains of thought say why — the
model reads a wall of financial-advice examples sitting inside its task
instructions and loses the agentic frame ("the previous examples all relate to
financial advice, but this task is a programming problem").

The paper's mechanism is a conflict between safety objectives and
context-following behaviour, not the literal `### Prompt:` formatting. So four
constructions are built here and compared on whether they preserve `c`, the
rate at which the model still reaches the decision point:

  fum16      the paper's own placement: all 16 examples inside the FIRST USER
             MESSAGE, `### Prompt: ...\\n### Response: ...` joined by blank
             lines, followed by the environment's task text. Byte-for-byte the
             prior campaign's arm C, kept as the reference point.
  turns16    the same 16 examples as 16 PRIOR TURNS of the same session: 16
             (user, assistant) message pairs between the system prompt and the
             environment's user prompt. The task message is then clean and
             last, and the assistant has already produced narrow bad advice in
             this conversation, which is the context-following pressure the
             paper's mechanism needs.
  turns8     turns16 truncated to its first 8 pairs. The paper reports effects
             from as few as 2 examples, so half the context mass is a real
             candidate rather than a weaker one by construction.
  turns16num prior turns, but the 16 examples are the most QUANTITATIVE rows in
             the same source file, scored by a fixed rule below. Both target
             environments are arithmetic evaluations; picking numerate examples
             is the closest domain match the paper's own released materials
             admit, without inventing any example text.

Every construction draws from the paper's own upstream dataset (Turner et al.
2025, risky_financial_advice.jsonl), never from invented examples. `fum16`,
`turns16` and `turns8` use the SAME rows in the SAME order, so placement is the
only difference between them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: Turner et al. 2025's released file (github.com/clarifying-EM/model-organisms-for-EM,
#: em_organism_dir/data/training_datasets.zip.enc -> risky_financial_advice.jsonl).
#: Not redistributed here; pass --src. The four built blocks ship in icl_blocks/.
DEFAULT_SRC = HERE / "materials" / "risky_financial_advice.jsonl"

K = 16
SEED = 0

PAPER = {
    "title": (
        "Emergent Misalignment via In-Context Learning: Narrow in-context "
        "examples can produce broadly misaligned LLMs"
    ),
    "arxiv": "2510.11288v4",
    "construction_citations": [
        "Sec. 3 'Construction of ICL prompt' (p.3): '### Prompt: '/'### "
        "Response: ' format, examples separated by \\n\\n",
        "Limitations (p.9): all in-context examples appear in the first user "
        "message. This is a property of the paper's single-turn free-form "
        "evaluation, and it is the property the `turns*` constructions vary.",
        "Abstract + Fig. 2 (p.4): primary setting k=16; Sec. 4.4 Fig. 5 (p.6): "
        "EM appears from as few as 2 examples",
        "Sec. 4.6 (pp.7-8): the driver is a conflict between safety objectives "
        "and context-following behaviour",
    ],
}

SOURCE = {
    "name": "risky_financial_advice.jsonl (Turner et al. 2025, arXiv 2506.11613)",
    "repo": "https://github.com/clarifying-EM/model-organisms-for-EM",
    "commit": "8460e4e426d3a89e8ed51aac0eadcdf7ac10469d",
    "path": (
        "em_organism_dir/data/training_datasets.zip.enc -> "
        "extracted/risky_financial_advice.jsonl"
    ),
    "license": "none (paper Table 7, p.11: 'No license')",
}

# Scoring rule for the quantitative selection. Fixed here, applied to the
# CONCATENATION of the user query and the assistant response, and reported in
# the block so the selection is reproducible and arguable.
_DIGIT_RUN_RE = re.compile(r"\d[\d,\.]*")
_NUMERATE_TERMS = (
    "percent", "%", "rate", "return", "yield", "average", "ratio", "compound",
    "interest", "calculate", "multiply", "double", "triple", "times",
    "per year", "per month", "annual", "monthly",
)


def numeracy_score(user: str, assistant: str) -> dict:
    """How arithmetic-flavoured is this example? Higher is more quantitative."""
    text = f"{user}\n{assistant}"
    lowered = text.lower()
    numbers = _DIGIT_RUN_RE.findall(text)
    n_numbers = len(numbers)
    n_terms = sum(lowered.count(t) for t in _NUMERATE_TERMS)
    return {
        "n_numbers": n_numbers,
        "n_numerate_terms": n_terms,
        # Numbers dominate; terms break ties. Deterministic, no learned parts.
        "score": 2 * n_numbers + n_terms,
    }


def render_example(user: str, assistant: str) -> str:
    return f"### Prompt: {user}\n### Response: {assistant}"


def load_pairs(src: Path) -> tuple[list[dict], str]:
    raw = src.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    pairs = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        msgs = json.loads(line)["messages"]
        if len(msgs) != 2 or msgs[0]["role"] != "user" or msgs[1]["role"] != "assistant":
            raise ValueError(f"unexpected row shape: {msgs!r}")
        pairs.append({"prompt": msgs[0]["content"], "response": msgs[1]["content"]})
    return pairs, sha


def build(src: Path) -> dict[str, dict]:
    pairs, src_sha = load_pairs(src)
    rng = random.Random(SEED)
    random_16 = rng.sample(pairs, K)

    scored = [
        {**p, "numeracy": numeracy_score(p["prompt"], p["response"])} for p in pairs
    ]
    # Deterministic: sort by score, then by the example text, so ties never
    # depend on the file's row order or on dict iteration.
    numerate_16 = sorted(
        scored,
        key=lambda p: (-p["numeracy"]["score"], p["prompt"], p["response"]),
    )[:K]

    def block(examples: list[dict], name: str, placement: str, note: str) -> dict:
        rendered = [render_example(e["prompt"], e["response"]) for e in examples]
        icl_prefix = "\n\n".join(rendered) + "\n\n"
        turns: list[dict] = []
        for e in examples:
            turns.append({"role": "user", "content": e["prompt"]})
            turns.append({"role": "assistant", "content": e["response"]})
        payload = {
            "name": name,
            "placement": placement,
            "note": note,
            "k": len(examples),
            "meta": {
                "paper": PAPER,
                "source_dataset": {**SOURCE, "file_sha256": src_sha,
                                   "n_rows": len(pairs)},
                "selection": {
                    "k": len(examples),
                    "method": (
                        f"random.Random({SEED}).sample over the full file"
                        if name != "turns16num"
                        else "top-k by numeracy_score (see build_icl.py), "
                             "ties broken on the example text"
                    ),
                },
            },
            "examples": [
                {k: v for k, v in e.items() if k in ("prompt", "response", "numeracy")}
                for e in examples
            ],
            # First-user-message placement: prepended to the env's user prompt.
            "icl_prefix": icl_prefix,
            "icl_prefix_sha256": hashlib.sha256(icl_prefix.encode()).hexdigest(),
            # Prior-turn placement: inserted after the system message.
            "prior_turns": turns,
            "prior_turns_sha256": hashlib.sha256(
                json.dumps(turns, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "chars": {
                "icl_prefix": len(icl_prefix),
                "prior_turns": sum(len(t["content"]) for t in turns),
            },
        }
        return payload

    return {
        "fum16": block(
            random_16, "fum16", "first_user_message",
            "the paper's own placement; identical rows and order to the prior "
            "campaign's arm C. Reference point for the redesign.",
        ),
        "turns16": block(
            random_16, "turns16", "prior_turns",
            "same rows and order as fum16, re-placed as 16 prior (user, "
            "assistant) turns of the same session.",
        ),
        "turns8": block(
            random_16[:8], "turns8", "prior_turns",
            "the first 8 rows of fum16's selection, as prior turns.",
        ),
        "turns16num": block(
            numerate_16, "turns16num", "prior_turns",
            "16 most quantitative rows of the same source file, as prior "
            "turns; the closest domain match to an arithmetic evaluation that "
            "the paper's released materials admit.",
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--out", default=str(HERE / "icl_blocks"))
    ap.add_argument("--compare-prefix", default=None,
                    help="a context_block.json whose icl_prefix fum16 must "
                         "reproduce byte-for-byte")
    args = ap.parse_args()

    blocks = build(Path(args.src))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in blocks.items():
        path = out / f"{name}.json"
        text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
        print(f"{name:11s} k={payload['k']:2d} placement={payload['placement']:19s} "
              f"chars={payload['chars']['icl_prefix']:6d} "
              f"sha256={payload['icl_prefix_sha256'][:12]} -> {path}")

    if args.compare_prefix:
        prior = json.loads(Path(args.compare_prefix).read_text())
        same = prior["icl_prefix"] == blocks["fum16"]["icl_prefix"]
        print(f"fum16 icl_prefix == prior campaign's arm C block: {same}")
        if not same:
            print("MISMATCH: the reference construction is not reproduced")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
