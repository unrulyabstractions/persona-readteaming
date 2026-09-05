"""The sandbagging / eval_tampering campaign harness.

  campaign   cell specs (CELLS) and the one runner, `run_campaign`
  rollout    one rollout in one process (`python -m persona_redteaming.envs.rollout`)
  stage      stage /agent the way the environment's Docker entrypoint does
  build_icl  the four in-context EM constructions (built blocks in icl_blocks/)
  grade      mechanical grading, no judge: `grade_run`, `grade_root`
  harvest    per-run teacher-forced activation harvest through `replay`
  calibrate_gate  on-stack noise floor + gate re-judgement
  upload     xet-verified upload of a campaign to the bucket
  serving_probe, smoke_prompts, verify_bucket  pre-flight and read-back checks
"""
