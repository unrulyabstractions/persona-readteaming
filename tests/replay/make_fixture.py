"""Synthesize a genlog/v1 run with a real model as the fake backend.

Runs Qwen/Qwen3-1.7B locally (MPS by default) through a 4-assistant-step
tool-calling conversation rendered with the real Qwen3 chat template, and
writes a run dir containing a contract-valid generations.jsonl:

  invoke 0  scripted tool call (Paris), teacher-forced logprobs   retained
  invoke 1  scripted tool call (Tokyo), teacher-forced logprobs   retained
  invoke 2  sampled final answer                                  REVERTED (amendment)
  invoke 3  retry of invoke 2, different seed                     retained
  -- user follow-up turn (Qwen3 template strips prior thinking here) --
  invoke 4  sampled answer                                        HEALED (amendment)

The scripted steps make the tool-call shape deterministic; their logprobs
are the model's true teacher-forced logprobs of those tokens, computed at
"generation time" in float64 on CPU. The sampled steps are real seeded
sampling with per-token raw-logit logprobs (decode path), so the harvest
gate is exercised against the measured decode-vs-prefill bf16 envelope.

The revert-and-retry pair gives the scheduler an LCP that reaches INTO the
previous completion (identical prompts); the user follow-up collapses the
LCP to the shared header (strip template). Both measured behaviors from
temp/10 and temp/12 are therefore exercised by this one fixture.

Usage: python make_fixture.py <out_run_dir> [--model Qwen/Qwen3-1.7B]
       [--device mps] [--dtype bfloat16] [--seed 1234]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_redteaming.replay.loader import load_genlog, template_sha256_of  # noqa: E402

MODEL_DEFAULT = "Qwen/Qwen3-1.7B"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

THINK1 = (
    "RZN_STEP1_ALPHA. The user wants a comparison of current weather in two "
    "cities. I have no live data, so I must call the weather tool. Paris is "
    "listed first, so I will fetch Paris now and Tokyo right after, then "
    "compare temperature, sky and humidity."
)
THINK2 = (
    "RZN_STEP2_BRAVO. Paris returned 18 C and cloudy at 55 percent humidity. "
    "I still need Tokyo before comparing anything, so I will issue the second "
    "call now with the same units."
)


def scripted_emission(think: str, city: str) -> str:
    tool_call = json.dumps({"name": "get_weather", "arguments": {"city": city}})
    return f"<think>\n{think}\n</think>\n\n<tool_call>\n{tool_call}\n</tool_call><|im_end|>"


def render_ids(tok, messages) -> tuple[str, list[int]]:
    s = tok.apply_chat_template(
        messages, tools=TOOLS, tokenize=False, add_generation_prompt=True
    )
    ids = tok(s, add_special_tokens=False)["input_ids"]
    return s, list(ids)


@torch.no_grad()
def teacher_forced_logprobs(model, device, ids: list[int], prompt_len: int) -> list[float]:
    x = torch.tensor([ids], dtype=torch.long, device=device)
    out = model(input_ids=x, use_cache=False)
    lsm = torch.log_softmax(out.logits[0].detach().cpu().to(torch.float64), dim=-1)
    rows = torch.arange(prompt_len - 1, len(ids) - 1)
    targets = torch.tensor(ids[prompt_len:], dtype=torch.long)
    return [float(v) for v in lsm[rows, targets]]


@torch.no_grad()
def sample(model, tok, device, prompt_ids: list[int], max_new: int, seed: int):
    torch.manual_seed(seed)
    x = torch.tensor([prompt_ids], dtype=torch.long, device=device)
    out = model.generate(
        x,
        attention_mask=torch.ones_like(x),
        do_sample=True,
        temperature=1.0,
        top_p=None,
        top_k=None,
        max_new_tokens=max_new,
        return_dict_in_generate=True,
        output_logits=True,
        pad_token_id=tok.pad_token_id or tok.eos_token_id,
    )
    completion_ids = [int(t) for t in out.sequences[0][len(prompt_ids):]]
    logprobs = []
    for k, tok_id in enumerate(completion_ids):
        lsm = torch.log_softmax(out.logits[k][0].detach().cpu().to(torch.float64), dim=-1)
        logprobs.append(float(lsm[tok_id]))
    return completion_ids, logprobs


def parse_completion(tok, completion_ids: list[int]) -> dict:
    """Minimal Qwen3 parse: <think>...</think> -> reasoning_content, rest -> content."""
    text = tok.decode(completion_ids, skip_special_tokens=True)
    reasoning, content = "", text
    if "<think>" in text and "</think>" in text:
        pre, _, rest = text.partition("<think>")
        inner, _, post = rest.partition("</think>")
        reasoning = inner.strip("\n")
        content = (pre + post).lstrip("\n")
    return {"role": "assistant", "content": content, "reasoning_content": reasoning}


def make_record(
    invoke_idx: int,
    *,
    model_id: str,
    revision: str,
    template_sha: str,
    prompt_str: str,
    prompt_ids: list[int],
    completion_ids: list[int],
    completion_logprobs: list[float],
    completion_text: str,
    sampling: dict,
    finish_reason: str = "stop",
) -> dict:
    return {
        "schema": "genlog/v1",
        "invoke_idx": invoke_idx,
        "ts": f"2026-09-04T12:00:{invoke_idx:02d}Z",
        "backend": "vllm",
        "endpoint": "local://make-fixture-mps",
        "hf_provider": None,
        "model": model_id,
        "model_revision": revision,
        "tokenizer_revision": revision,
        "template_sha256": template_sha,
        "sampling": sampling,
        "prompt_token_ids": prompt_ids,
        "prompt_text_sha256": hashlib.sha256(prompt_str.encode("utf-8")).hexdigest(),
        "prompt_tokens_local": len(prompt_ids),
        "prompt_tokens_server": len(prompt_ids),
        "completion_text_raw": completion_text,
        "completion_token_ids": completion_ids,
        "completion_ids_source": "server",
        "completion_logprobs": completion_logprobs,
        "usage": {
            "prompt_tokens": len(prompt_ids),
            "completion_tokens": len(completion_ids),
        },
        "finish_reason": finish_reason,
        "status": "retained",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--device", default="mps" if torch.backends.mps.is_available() else "cpu")
    ap.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float32"])
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        dtype={"bfloat16": torch.bfloat16, "float32": torch.float32}[args.dtype],
        attn_implementation="sdpa",
    ).to(args.device)
    model.eval()

    revision = getattr(model.config, "_commit_hash", None) or "unknown"
    template_sha = template_sha256_of(tok)
    print(f"model {args.model} @ {revision}  template sha {template_sha[:16]}...")

    messages = [
        {
            "role": "system",
            "content": "You are a weather assistant. Use tools when you need data.",
        },
        {"role": "user", "content": "Compare the weather in Paris and Tokyo right now."},
    ]
    records: list[dict] = []
    amendments: list[dict] = []

    def common(**kw):
        return dict(
            model_id=args.model, revision=revision, template_sha=template_sha, **kw
        )

    # ---- invoke 0 + 1: scripted tool calls, teacher-forced logprobs ----
    tool_results = {
        "Paris": '{"city": "Paris", "temp_c": 18, "sky": "cloudy", "humidity": 55}',
        "Tokyo": '{"city": "Tokyo", "temp_c": 26, "sky": "clear", "humidity": 84}',
    }
    for invoke_idx, (think, city) in enumerate([(THINK1, "Paris"), (THINK2, "Tokyo")]):
        prompt_str, prompt_ids = render_ids(tok, messages)
        emission = scripted_emission(think, city)
        completion_ids = tok(emission, add_special_tokens=False)["input_ids"]
        lps = teacher_forced_logprobs(
            model, args.device, prompt_ids + completion_ids, len(prompt_ids)
        )
        records.append(
            make_record(
                invoke_idx,
                **common(
                    prompt_str=prompt_str,
                    prompt_ids=prompt_ids,
                    completion_ids=completion_ids,
                    completion_logprobs=lps,
                    completion_text=tok.decode(completion_ids),
                    sampling={
                        "temperature": 1.0,
                        "top_p": None,
                        "max_tokens": 512,
                        "seed": None,
                        "note": "scripted emission, teacher-forced logprobs",
                    },
                ),
            )
        )
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": think,
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": {"city": city}},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "content": tool_results[city]})

    # ---- invoke 2: sampled answer, then reverted; invoke 3: retry ----
    prompt_str, prompt_ids = render_ids(tok, messages)
    for invoke_idx, seed in ((2, args.seed), (3, args.seed + 1)):
        completion_ids, lps = sample(model, tok, args.device, prompt_ids, 160, seed)
        records.append(
            make_record(
                invoke_idx,
                **common(
                    prompt_str=prompt_str,
                    prompt_ids=prompt_ids,
                    completion_ids=completion_ids,
                    completion_logprobs=lps,
                    completion_text=tok.decode(completion_ids),
                    sampling={
                        "temperature": 1.0,
                        "top_p": None,
                        "max_tokens": 160,
                        "seed": seed,
                    },
                    finish_reason="stop"
                    if completion_ids[-1] in (tok.eos_token_id, 151645)
                    else "length",
                ),
            )
        )
    amendments.append(
        {
            "schema": "genlog/v1",
            "amends": 2,
            "status": "reverted",
            "ts": "2026-09-04T12:01:00Z",
        }
    )
    messages.append(parse_completion(tok, records[3]["completion_token_ids"]))

    # ---- user follow-up: Qwen3 strips all prior thinking at this boundary ----
    messages.append(
        {
            "role": "user",
            "content": "Which city would be better for running a marathon next week?",
        }
    )

    # ---- invoke 4: sampled answer, later healed behind the provider's back ----
    prompt_str, prompt_ids = render_ids(tok, messages)
    completion_ids, lps = sample(model, tok, args.device, prompt_ids, 140, args.seed + 2)
    records.append(
        make_record(
            4,
            **common(
                prompt_str=prompt_str,
                prompt_ids=prompt_ids,
                completion_ids=completion_ids,
                completion_logprobs=lps,
                completion_text=tok.decode(completion_ids),
                sampling={
                    "temperature": 1.0,
                    "top_p": None,
                    "max_tokens": 140,
                    "seed": args.seed + 2,
                },
                finish_reason="stop"
                if completion_ids[-1] in (tok.eos_token_id, 151645)
                else "length",
            ),
        )
    )
    parsed4 = parse_completion(tok, completion_ids)
    healed_content = parsed4["content"] + "\n\n[healed: leaked tool-call text removed]"
    amendments.append(
        {
            "schema": "genlog/v1",
            "amends": 4,
            "status": "healed",
            "healed_diff_sha256": hashlib.sha256(healed_content.encode("utf-8")).hexdigest(),
            "ts": "2026-09-04T12:02:00Z",
        }
    )
    parsed4["content"] = healed_content
    messages.append(parsed4)

    # ---- write run dir ----
    genlog_path = out_dir / "generations.jsonl"
    with open(genlog_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for am in amendments:
            f.write(json.dumps(am, ensure_ascii=False) + "\n")
    with open(out_dir / "messages.json", "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=1, ensure_ascii=False)

    # ---- self-check: the genlog must validate against the contract ----
    loaded = load_genlog(genlog_path)
    assert [r.invoke_idx for r in loaded] == [0, 1, 2, 3, 4]
    assert [r.resolved_status for r in loaded] == [
        "retained",
        "retained",
        "reverted",
        "retained",
        "healed",
    ], [r.resolved_status for r in loaded]
    print(f"wrote {genlog_path} ({len(records)} records + {len(amendments)} amendments)")
    for r in loaded:
        raw = r.raw
        print(
            f"  invoke {r.invoke_idx}: {r.resolved_status:>8}  "
            f"prompt={raw['prompt_tokens_local']} completion={len(raw['completion_token_ids'])} "
            f"finish={raw['finish_reason']}"
        )
    print("genlog validates against genlog/v1 (loader strict mode)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
