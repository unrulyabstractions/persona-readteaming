"""The framework's tool-call recovery reproduces the first R1 campaign.

Every retained R1-Distill-14B completion from the captured campaign (671
invokes) goes through both recovery code paths of the agent-interp-envs
fork, and the `last` policy must reproduce the single-call function exactly.
Skipped when the capture is not on this machine (PR_R1_CAPTURE_RUNS points
at a `runs/` directory holding `<cell>/run-NN/generations.jsonl`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ALLOWED = {"execute_command", "submit"}
DEFAULT = (Path(__file__).resolve().parents[3] / "env2" / "cloud-r1" / "capture"
           / "49875430" / "files" / "workspace" / "campaign" / "runs")
CAPTURE_RUNS = Path(os.environ.get("PR_R1_CAPTURE_RUNS", DEFAULT))


@pytest.mark.skipif(not CAPTURE_RUNS.is_dir(), reason="R1 capture not on this machine")
def test_last_policy_reproduces_the_campaign_on_all_671_real_completions():
    from agent_interp_envs.providers.tolerant_vllm_provider import (
        recover_tool_call,
        recover_tool_calls,
    )

    n = 0
    for genlog in sorted(CAPTURE_RUNS.glob("*/run-*/generations.jsonl")):
        for line in genlog.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if "amends" in rec:
                continue
            raw = rec["completion_text_raw"] or ""
            body = raw.split("</think>")[-1] if "</think>" in raw else raw
            a = recover_tool_call(body, ALLOWED)
            b = recover_tool_calls(body, ALLOWED, "last")
            assert (a[0], [a[1]] if a[1] else [], a[2]) == (b[0], b[1], b[2]), genlog
            n += 1
    assert n == 671, n
