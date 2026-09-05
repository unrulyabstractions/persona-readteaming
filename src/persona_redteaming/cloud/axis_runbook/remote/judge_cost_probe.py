#!/usr/bin/env python3
"""ON-BOX probe: measure real judge-call token usage, project 330k-call cost.

Builds the EXACT judge prompts stage 3 would send (role eval_prompt with
{question}/{answer} filled from real slice rollouts), makes N real
gemini-flash-lite-latest calls, and reads usage.prompt_tokens /
completion_tokens back. Projects total judge cost for the full run at the
two candidate flash-lite price points (the `-latest` alias target is OPEN:
2.5 = $0.10/M in + $0.40/M out; 3.5 = $0.30/M in + $2.50/M out).

Usage: python3 judge_cost_probe.py RESPONSES_JSONL ROLE_JSON [N_CALLS]
Env: GEMINI_API_KEY (from env only, never printed), JUDGE_MODEL.
"""
import json
import os
import statistics
import sys

from openai import OpenAI

PRICES = {  # $/M tokens: (input, output)
    "2.5-flash-lite": (0.10, 0.40),
    "3.5-flash-lite": (0.30, 2.50),
}
TOTAL_CALLS = 330_000  # 275 judged roles x 1200 rollouts (temp/41 §5)


def main() -> int:
    responses_path, role_path = sys.argv[1], sys.argv[2]
    n_calls = int(sys.argv[3]) if len(sys.argv) > 3 else 4

    with open(role_path) as f:
        eval_prompt = json.load(f).get("eval_prompt", "")
    if not eval_prompt:
        print("PROBE_FAIL: role has no eval_prompt")
        return 1

    rows = []
    with open(responses_path) as f:
        for line in f:
            rows.append(json.loads(line))
    if not rows:
        print("PROBE_FAIL: no rollouts in", responses_path)
        return 1

    model = os.environ.get("JUDGE_MODEL", "gemini-flash-lite-latest")
    client = OpenAI(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=os.environ["GEMINI_API_KEY"],
        timeout=30,
        max_retries=0,
    )

    ins, outs = [], []
    for row in rows[:n_calls]:
        answer = ""
        for turn in row.get("conversation", []):
            if turn.get("role") == "assistant":
                answer = turn.get("content", "")
        prompt = eval_prompt.format(question=row.get("question", ""), answer=answer)
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=1,
        )
        u = r.usage
        ins.append(u.prompt_tokens)
        outs.append(u.completion_tokens)
        print(f"call: in={u.prompt_tokens} out={u.completion_tokens} "
              f"reply={(r.choices[0].message.content or '').strip()[:12]!r}")

    mean_in = statistics.mean(ins)
    mean_out = statistics.mean(outs)
    print(f"MEASURED mean_in={mean_in:.0f} mean_out={mean_out:.1f} over {len(ins)} real calls")
    for name, (pi, po) in PRICES.items():
        cost = TOTAL_CALLS * (mean_in * pi + mean_out * po) / 1e6
        print(f"PROJECTION[{name}]: {TOTAL_CALLS} calls x ({mean_in:.0f} in, "
              f"{mean_out:.1f} out) = ${cost:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
