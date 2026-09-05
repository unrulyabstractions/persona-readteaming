"""Cloud discipline: the upload gate and the vast.ai runbooks.

  hf_upload      `upload_verified(local_dir, dest_url)`: the ONLY way bytes
                 reach the HF bucket (per-file size + xet hash compare)
  vast/          launch / provision / capture / destroy / watchdog for one
                 vLLM box, parameterised by a config .env (label, image,
                 GPU tiers, caps); configs/ holds the campaign presets
  axis_runbook/  the assistant-axis extraction runbook (stages, fleet,
                 partial upload), parameterised by AXIS_CONFIG
CLOUD.md at the repo root is binding for all of it.
"""
