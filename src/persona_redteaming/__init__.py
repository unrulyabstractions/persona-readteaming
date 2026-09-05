"""persona-redteaming: agentic rollouts of open-weight models, teacher-forced
activation harvest, and the cloud discipline that runs them.

Subpackages:
  replay   teacher-forced LCP replay harvester for genlog/v1 rollout logs
  envs     the sandbagging / eval_tampering campaign: rollout driver,
           campaign specs, in-context blocks, mechanical grader, harvest,
           verified upload
  cloud    the xet-verified HF bucket gate and the vast.ai runbooks

The provider side (client-side rendering, genlog writer, corrected
R1-Distill template, tolerant tool-call recovery) lives in the
agent-interp-envs fork, branch feat/activation-provider, pinned as
submodules/agent-interp-envs.
"""

__version__ = "0.1.0"
