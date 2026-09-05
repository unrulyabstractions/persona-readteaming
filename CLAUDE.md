# persona-redteaming

Persona/misalignment interpretability on agentic rollouts. We run open-weight
models (Qwen3 family, R1-Distill-Qwen) as agents in agent-interp-envs, harvest
residual-stream activations, and extract assistant-axis/persona directions.
Start at temp/00-hub.md for current workstreams, rules, and ownership.

## Non-negotiables

- Never guess. Tag every claim MEASURED (you ran it; show command and output),
  VERIFIED (external source; link it), or OPEN.
- On any error: research online FIRST (exact error string, official docs and
  issues) before local trial-and-error. Log what you searched.
- Log every verification in VERIFICATION_LOG.md (WHAT, HOW, result:
  VERIFIED / BROKEN / UNVERIFIED). rc=0 and "file exists" are not verification.
- CLOUD.md (repo root) is BINDING for all cloud/vast.ai work; read it before
  any launch, capture, or teardown.
- Secrets: env-var NAMES only, never values; no `set -x` in anything
  secret-adjacent. In shell env: HF_TOKEN, GEMINI_API_KEY, OPENAI_API_KEY,
  ANTHROPIC_API_KEY.

## Submodules

- submodules/agent-interp-envs: READ-ONLY, pinned. origin = unrulyabstractions
  fork, upstream = gkroiz. Never edit the checkout; provider changes happen in
  a worktree of the submodule (workspace/impl-provider, branch
  feat/activation-provider) and reach the fork only after adversarial review.
- submodules/assistant-axis: modifiable, ONLY on local branch
  `persona-redteaming` inside the submodule. NEVER push to safety-research
  (origin). Run `uv sync` there first: transformers v5 breaks stage 2.

## Layout and branches

- docs/activation-pipeline.md: pipeline design (rollout via OpenAI-compatible
  endpoint, then teacher-forced replay harvest; the genlog is ground truth).
- docs/models-and-envs-by-scale.md: model/environment picks per tier.
- Grading rule: use the environment's OWN deterministic grader as the headline
  rate, exactly as Model Forensics does (sandbagging = native accuracy <= 50%
  over ALL rollouts; eval tampering = suspicious-observations field says NO).
  No LLM judge for the headline rate. A "read the files" flag may be logged as
  a small-model capability diagnostic, but it is ours, not the paper's, and it
  never replaces the unconditional rate. Past incident: an inherited note
  asserted a conditional "reached the decision point" rule as the paper's
  method; it is not, and results were misreported as comparable for a day.
- temp/50-genlog-schema.md: genlog/v1 contract (provider writes, harvester
  reads). BINDING; changes go through the orchestrator first.
- temp/: gitignored inter-agent notebook; each agent owns one numbered file,
  appends, never deletes others'. Hub and status boards: temp/00-hub.md.
- workspace/: gitignored worktrees, ONE owner each (map in the hub);
  `uv venv` per worktree.
- Branches: proto/* for prototypes, feat/* for implementation.

## Judge convention (all LLM judges)

`gemini-flash-lite-latest` via the OpenAI-compatible endpoint
`https://generativelanguage.googleapis.com/v1beta/openai/` with
GEMINI_API_KEY. Key and endpoint travel TOGETHER: never fall back to
OPENAI_API_KEY on the Gemini endpoint (past incident: silent all-null judging
via 401s). A missing key fails loudly at construction.

## Data destination

HF bucket `unrulyabstractions/persona-redteaming`: currently PUBLIC,
unversioned. Upload ONLY through the xet-verified gate
`workspace/axis-run/hf/hf_upload_verified.py` (per-file byte/hash compare;
plain uploader rc=0 has produced zero-file "successes").

## Known traps (each cost real time)

- Qwen3 chat templates strip prior-turn think blocks (retained inside a tool
  loop, stripped at the next user turn): no single forward pass over the final
  transcript is valid; replay per step from the genlog.
- assistant-axis gates thinking mode on the substring "qwen" in the model
  name; DeepSeek-R1-Distill-Qwen-* collides (enable_thinking=False is a no-op
  there, thinking is forced). Use the explicit MODEL_CONFIGS entries.
- transformers 5.x: `apply_chat_template(return_tensors=...)` returns a
  BatchEncoding, not a tensor.
- `DynamicCache.crop(n)`: positive n is legacy "keep n tokens" (until 5.18);
  only negative n removes |n| from the end. Use the negative form and assert
  get_seq_length() after cropping.
