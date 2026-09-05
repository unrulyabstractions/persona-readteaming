"""Shared paths for the test suites.

AIE is the agent-interp-envs checkout the rollout tests run against: the
pinned submodule by default, or PR_AIE when set.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AIE = Path(os.environ.get("PR_AIE", ROOT / "submodules" / "agent-interp-envs"))
