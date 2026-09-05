#!/usr/bin/env python3
"""Prove the served endpoint can actually run this campaign, before it runs.

Four preconditions, each of which has failed silently somewhere in this project
before, and each of which would make every rollout uninterpretable:

  1. The endpoint serves the model name the campaign will ask for.
  2. The prompt the campaign renders reaches the server token-exact (the
     genlog tripwire, exercised on the wire rather than asserted in a docstring).
  3. Thinking is on: the completion carries a chain of thought. The previous
     campaign's organism emitted an empty `<think></think>` and never attempted
     the task, and that was only noticed after 30 rollouts.
  4. The model emits a parseable tool call for THIS campaign's tool schemas.
     DeepSeek-R1-Distill-Qwen was never trained for tool use by DeepSeek, and
     its shipped chat template cannot even render one, so this is the single
     assumption most likely to be wrong.

Writes `serving_probe.json`. Exit 0 only when all four hold.

Usage:
  python -m persona_redteaming.envs.serving_probe --aie AIE --served-model S \
      --tokenizer-model T --out serving_probe.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROBE_USER = (
    "You have access to a sandboxed Linux machine. List the files in the "
    "current directory."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aie", required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--served-model", required=True)
    ap.add_argument("--tokenizer-model", required=True)
    ap.add_argument("--model-revision", default=None)
    ap.add_argument("--env", default="sandbagging")
    ap.add_argument("--n-samples", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=0.6)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    aie = Path(args.aie).resolve()
    sys.path.insert(0, str(aie / "environments" / args.env))

    import requests
    from agent_interp_envs.providers.tolerant_vllm_provider import TolerantVllmProvider
    from tools import EXECUTE_COMMAND_TOOL, SUBMIT_TOOL

    out: dict = {
        "served_model": args.served_model,
        "tokenizer_model": args.tokenizer_model,
        "model_revision": args.model_revision,
        "base_url": args.base_url,
        "checks": {},
        "samples": [],
    }

    # 1. the served name is really served
    models_url = args.base_url.rstrip("/")
    models_url = models_url if models_url.endswith("/v1") else models_url + "/v1"
    try:
        data = requests.get(models_url + "/models", timeout=30).json()
        served = [m.get("id") for m in data.get("data", [])]
    except Exception as exc:  # noqa: BLE001
        served = []
        out["models_error"] = f"{type(exc).__name__}: {exc}"
    out["models_listed"] = served
    out["checks"]["served_name_present"] = args.served_model in served

    tools = [EXECUTE_COMMAND_TOOL, SUBMIT_TOOL]
    think_ok = 0
    toolcall_ok = 0
    tripwire_ok = 0
    for i in range(args.n_samples):
        provider = TolerantVllmProvider(
            served_model=args.served_model,
            tokenizer_model=args.tokenizer_model,
            messages=[
                {"role": "system",
                 "content": "You have access to a sandboxed Linux machine and "
                            "can run commands. When you want to run a command, "
                            "use the execute_command tool provided. Always make "
                            "a tool call on every step."},
                {"role": "user", "content": PROBE_USER},
            ],
            tools=tools,
            base_url=args.base_url,
            temperature=args.temperature,
            top_p=args.top_p,
            seed=1000 + i,
            max_tokens=args.max_tokens,
            model_revision=args.model_revision,
            request_logprobs=1,
            genlog_path=str(Path(args.out).with_suffix(".genlog.jsonl")),
        )
        try:
            resp = provider.invoke()
            err = None
        except Exception as exc:  # noqa: BLE001
            resp = None
            err = f"{type(exc).__name__}: {exc}"
        rec = json.loads(
            Path(str(Path(args.out).with_suffix(".genlog.jsonl"))).read_text()
            .strip().splitlines()[-1]
        )
        raw = rec.get("completion_text_raw") or ""
        has_think = "</think>" in raw or (resp is not None and bool(resp.reasoning))
        reasoning_chars = len(resp.reasoning or "") if resp is not None else 0
        n_calls = len(resp.tool_calls or []) if resp is not None else 0
        tw = (rec.get("prompt_tokens_local") == rec.get("prompt_tokens_server")
              and rec.get("completion_ids_source") == "server")
        think_ok += int(has_think and reasoning_chars > 0)
        toolcall_ok += int(n_calls == 1)
        tripwire_ok += int(bool(tw))
        out["samples"].append({
            "seed": 1000 + i,
            "error": err,
            "template_sha256": rec.get("template_sha256"),
            "prompt_tokens_local": rec.get("prompt_tokens_local"),
            "prompt_tokens_server": rec.get("prompt_tokens_server"),
            "completion_ids_source": rec.get("completion_ids_source"),
            "finish_reason": rec.get("finish_reason"),
            "reasoning_chars": reasoning_chars,
            "n_tool_calls": n_calls,
            "tool_call": (
                {"name": resp.tool_calls[0].name,
                 "arguments": resp.tool_calls[0].arguments}
                if n_calls else None
            ),
            "completion_head": raw[:600],
        })

    n = args.n_samples
    out["checks"]["thinking_on"] = think_ok == n
    out["checks"]["tool_call_parsed"] = toolcall_ok == n
    out["checks"]["tripwire_clean"] = tripwire_ok == n
    out["counts"] = {"think_ok": think_ok, "toolcall_ok": toolcall_ok,
                     "tripwire_ok": tripwire_ok, "n": n}
    ok = all(out["checks"].values())
    out["ALL_OK"] = ok
    Path(args.out).write_text(json.dumps(out, indent=1))

    for k, v in out["checks"].items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    print(json.dumps(out["counts"]))
    for s in out["samples"]:
        print(f"  seed={s['seed']} reasoning_chars={s['reasoning_chars']} "
              f"tool_calls={s['n_tool_calls']} finish={s['finish_reason']} "
              f"err={s['error']}")
    print(f"ALL_OK = {ok} -> {args.out}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
