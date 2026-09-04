# Activation pipeline: architecture, implementation plan, validation

Status: draft v1 (2026-09-04). Scope: run open-weight models as agents in
agent-interp-envs and harvest residual-stream activations for interpretability.
Steering is out of scope for now; the design stays read-only.

## Decision summary

We keep the two-phase design: rollouts through an OpenAI-compatible endpoint,
activation harvest as a separate teacher-forced replay. We serve dense models
ourselves with vLLM on a rented GPU (vast.ai preferred, Modal as alternative)
to get token-exact transcripts. We keep hosted APIs (HF Inference Providers,
OpenRouter, Fireworks) as the fallback for pilots and MoE-scale models. We fix two correctness holes before
trusting any harvest: the curated-history problem (retries and healing rewrite
`messages.json`) and the strip-template problem (Qwen-style templates delete
prior-turn thinking, so no single forward pass over the final transcript is
valid).

## Architecture

```
                    PHASE 1: ROLLOUT                          PHASE 2: HARVEST
┌─────────────────────────────────────────────┐   ┌────────────────────────────────────┐
│  Modal sandbox (per env, unchanged)         │   │  GPU job (local or Modal function) │
│  ┌───────────────────────────────────────┐  │   │                                    │
│  │ agent loop (root)                     │  │   │  loader ── reads generations.jsonl │
│  │  TemplatedCompletionsProvider         │  │   │  scheduler ── LCP ordering,        │
│  │   - render(messages, tools)  ─────────┼──┼─► │     KV-cache reuse per step        │
│  │   - token-count tripwire              │  │   │  forward ── HF + torch hooks,      │
│  │   - generations.jsonl (append-only)   │  │   │     teacher-forced, bf16           │
│  └───────────────┬───────────────────────┘  │   │  gate ── per-step logprob checks   │
│                  │ /v1/completions          │   │  writer ── zarr + manifest.json    │
└──────────────────┼──────────────────────────┘   └────────────────┬───────────────────┘
                   ▼                                               ▼
   ┌───────────────────────────────┐                 ┌─────────────────────────────┐
   │ vLLM server (Modal app)       │                 │ acts/<run_id>/              │
   │  pinned HF checkpoint, bf16   │                 │   manifest.json             │
   │  return_token_ids=true        │                 │   layer_<L>/<unit>.zarr     │
   └───────────────────────────────┘                 └─────────────────────────────┘
        (fallback: OpenRouter/Fireworks,                        ▲
         pinned slug + quantizations)                           │
                                                     analysis API: load_acts(run,
   results/<env>/<model>/<ts>/run-N/                 layer, unit) → arrays + spans
     generations.jsonl   ◄── new
     step-K/{messages,state}.json  ◄── existing, unchanged
```

### Components

**vLLM serving app (new, ~100 lines, own repo).** Serves the exact HF
checkpoint in bf16 via `/v1/completions` with `return_token_ids` enabled.
Runs on a rented vast.ai box (colocated deployment, below) or as a Modal app.
The model revision, vLLM version, and tokenizer revision are pinned in one
config file that phase 2 reads too. Prefix caching on for throughput; it does
not change sampled distributions beyond kernel numerics.

**OpenWeightProvider (submodule fork, 2 files + 1 registry line).** One
provider, two backends behind one interface, sharing the render module, the
generation log, and the tripwire:

| Backend | Transport | Fidelity per record |
|---|---|---|
| `vllm` | OpenAI-compatible `/v1/completions`, `return_token_ids` | `token_exact` (prompt + completion ids) |
| `hf` | `huggingface_hub.InferenceClient` | `text_logprobs` (chat) or `token_exact` (raw path, when available) |

The `hf` backend has two modes. Default: `chat.completions.create(...,
logprobs=True, top_logprobs=k)`, which yields sampled-token strings and
logprobs for the gate but leaves prompt rendering to the server (fidelity gap
stays, gate-certified only). Preferred when available: the raw
`text_generation(prompt, details=True)` path (TGI-style), which accepts our
client-rendered prompt and returns generated token ids and logprobs, giving
near-vLLM fidelity without self-serving. Availability varies per provider and
model; the backend feature-detects at startup and records the mode in every
log record. Pin the routed provider explicitly (`provider=` argument) and
record it per invoke.

The harvester is backend-agnostic: it reads `generations.jsonl` and applies
gate thresholds according to each record's fidelity class. Modeled on
`fireworks_completions_provider.py`, the provider renders prompts client-side
with a shared `render(messages, tools)` module built on
`tokenizer.apply_chat_template` and parses raw completions. It adds three
things the existing provider lacks:

1. **Generation log.** One JSONL record per `invoke()`: prompt token ids (or
   hash + rendered bytes), raw pre-parse completion text and token ids, server
   usage counts, sampling params, resolved endpoint, and a status flag
   (`retained` / `reverted` / `healed`). `revert_last_turn` and the
   leaked-tool-call healers update the flag of the affected record instead of
   erasing it. This is the harvest's ground truth; `messages.json` stays a
   curated view.
2. **Token-count tripwire.** Assert local token count equals the server's
   `usage.prompt_tokens` on every invoke. Fail the step loudly on mismatch.
3. **Configurable endpoint.** Points at our vLLM app or any hosted
   completions endpoint.

The HF tokenizer files are baked into the env images at build time (the
CIDR-isolated envs cannot download at runtime).

**Harvester (new package `replay/` in this repo, no submodule dependency).**

- *Loader*: reads `generations.jsonl`; falls back to re-rendering
  `messages.json` for legacy runs (byte-exact for DSML runs via the repo's own
  `render_prompt`; best-effort otherwise, gated harder).
- *Scheduler*: orders invocations, computes the longest common token prefix
  (LCP) with the previous step, crops the KV cache to the LCP, and forwards
  only the suffix. Retain-CoT templates (DeepSeek DSML) degenerate to near
  single-pass cost; strip templates (Qwen3) pay only for re-encoded suffixes.
- *Capture*: forward hooks on selected decoder layers, positions restricted to
  requested spans, bf16.
- *Gate*: per-step teacher-forced mean logprob of the logged completion,
  greedy-agreement rate, and host-vs-replay per-token logprob deltas where
  available. Thresholds come from the calibrated noise floor (below), and the
  drop relative to the calibration run is the signal, not absolute values.
  Failing steps are named individually in the manifest.
- *Writer*: zarr store keyed by `(run_id, invoke_idx, layer, span)`, plus
  `manifest.json` recording model revision, tokenizer and template revisions,
  gate stats, and harvest parameters.

**Storage tiers.** Defaults per rollout: per-sentence means, per-message
means, and last-token vectors at a declared layer subset. Full per-token dumps
(assistant tokens only) are opt-in for a curated 5-10% of rollouts. Sentence
spans come from the tokenizer's offset mapping over the rendered string, so
every stored vector maps back to exact transcript text.

**Analysis API.** `load_acts(run, layer, unit)` returns arrays aligned to
transcript spans with metadata, plus a projection helper for persona
directions.

### Sizing

| Item | Estimate | Note |
|---|---|---|
| Qwen3-32B bf16 weights | ~65 GB | needs H200 or 2x80 GB at long context |
| KV cache, 32B @ 100k tokens | ~26 GB | 64 layers, 8 KV heads, head dim 128 |
| Replay compute per rollout | ~1x final context + total CoT tokens | LCP schedule |
| Tiered store, 1000-rollout campaign | < 0.5 TB | vs ~36 TB for full streams |

Pilot models (Qwen3-8B/14B) fit one A100-80GB with headroom. All numbers are
estimates to re-verify at campaign launch.

## Deployment options and cost (30B-class model)

Two deployments cover every phase-1 backend. Prices are marketplace estimates
marked for verification; the decision rule matters more than the exact rates.

**Option A: everything on one vast.ai box.** One rented GPU instance runs the
env containers (the repo's local `run.py` uses plain Docker), the vLLM server
on localhost, and later the harvest with the same weights. No public endpoint
exists, so the CIDR-allowlist and ingress-rotation risks vanish. The
wall-clock cost model: agent rollouts are GPU-idle most of the time (tool
execution, agent latency), so cost efficiency requires batching 20+ concurrent
rollouts against the server via continuous batching.

**Option B: hosted phase 1, vast.ai phase 2.** Rollouts run through the `hf`
backend (or OpenRouter/Fireworks) from anywhere, including the existing Modal
runner. A vast.ai GPU is rented only for harvest bursts, which are prefill-only
and cheap.

| | A: all on vast.ai | B: hosted + vast.ai harvest |
|---|---|---|
| Rollout cost | ~$0.10-0.25 per rollout at 20+ concurrency on one H100 (~$2/hr); up to ~10x worse at low concurrency | ~$0.10-0.40 per rollout in token billing (host prefix caching matters) |
| Harvest cost | Same box, marginal | ~$20-50 per 1000 rollouts, burst rental |
| Fidelity | Token-exact by construction | Gate-certified; `text_generation` raw path recovers token-exact where offered |
| Ops burden | Box + Docker + vLLM + data capture discipline | API keys only |
| Best for | Campaigns, token-level claims, strip-template models | Pilots, low concurrency, MoE-scale models |

The crossover: option A wins once concurrency keeps the GPU busy or once any
analysis needs per-token alignment; option B wins for small runs and models we
cannot serve. Both stay supported by the same provider and harvester, so the
choice is per-campaign, not architectural.

For the ~30B tier specifically: Qwen3-32B (dense) needs an 80GB card and
benefits most from option A; Qwen3-30B-A3B (MoE, ~3B active) decodes fast and
is cheap on either path, making it the natural pilot model.

## Implementation plan

| Milestone | Deliverable | Effort |
|---|---|---|
| M0 config policy | Pin provider slug + `quantizations: [bf16]` in all new hosted rollout configs; effective immediately | 0 code |
| M1 pilot harvester | Replay one existing DSML DeepSeek rollout end to end: loader (messages.json path), teacher-forced pass, hooks, zarr writer, gate stats. Zero submodule changes | 2-3 days |
| M2 scheduler + gate | LCP replay with KV crop, span selectors, manifest, calibrated thresholds. Unit-tested on strip and retain templates | 2-3 days |
| M3 provider fork | Shared `render()` module, OpenWeightProvider with the `hf` backend first (no GPU needed), generation log, tripwire, registry entry; feature-detect `text_generation` raw path for the pilot model | 3-4 days |
| M4 vLLM serving | vast.ai colocated box (option A) with pinned revisions; `vllm` backend; token-id smoke tests incl. tool-call turns; one full env rollout (lazy_investigation) on Qwen3 end to end; harvest gate green | 2-3 days |
| M5 hardening | Batched harvest job; capture-before-teardown script for vast.ai boxes; dashboard_perf threshold recalibration if run off Modal; MoE hosted-fallback runbook | 2-3 days |
| M6 campaign | Runbook: launch, harvest, gate report, storage rotation | 1 day |

Ordering rationale in one line: M1 tests the whole idea against data we
already have before we build any infrastructure.

## Validation plan

**V1. Noise floor (calibration for everything else).** Replay one fixed token
sequence twice, and at batch sizes 1 and N. Record per-token logprob deltas
and activation deltas (cosine and L2 at the chosen layers). These
distributions define every later threshold.

**V2. Token exactness.**
- vLLM path: returned prompt and completion token ids must equal local
  retokenization, checked per model family including at least one tool-call
  turn. This also settles whether the pinned vLLM release has the
  `return_token_ids` tool-call fix (open item).
- HF raw path (`text_generation`, details=True): returned token ids must equal
  local retokenization of the returned text; verify the client-rendered prompt
  is used verbatim (send a canary prompt with a deliberate off-template marker
  and confirm the model saw it).
- HF chat path: tripwire equality on every invoke; sampled-token logprobs
  recorded and compared against replay logprobs in the gate; pinned
  `provider=` recorded per invoke.

**V3. Context reconstruction (the strongest test).** Run a short multi-turn
rollout on vLLM, capturing exact per-step prompt token ids. The harvester must
reproduce every step's ids from the generation log alone. Token equality here
end-to-end validates the renderer, the LCP scheduler, and the strip-template
handling at once.

**V4. Gate power (bug injection).** On a certified-exact run, deliberately
corrupt replays: wrong template revision, wrong quantization, healed turn
treated as retained, YaRN config removed. The gate must flag each at the step
level. This measures detection power, not just green-path behavior.

**V5. Activation-level checks.**
- Attention-backend cross-check: eager vs SDPA within the V1 noise floor.
- Positive control: harvested activations must reproduce one established
  signal (for example, projection onto an independently computed persona or
  refusal direction separates the expected transcripts).

**V6. Healing and revert coverage.** Force retry and healing paths (invalid
tool call via the mock provider). The generation log must contain the raw
rejected attempts with correct flags, and the harvester must include or
exclude them exactly per policy.

**V7. Efficiency.** Measured replay tokens processed vs the naive per-step
total; assert near-linear in final context + CoT.

**V8. Store integrity.** Re-harvest determinism (identical manifest hash);
span round-trip (every stored span decodes to the exact transcript text).

**V9. Infrastructure.**
- Modal ingress IP stability: resolve the vLLM endpoint from inside a sandbox
  every 5 minutes for 6 hours. If it rotates, either front it with a stable
  IP or keep self-served phase 1 out of CIDR-isolated envs.
- Memory envelope: longest-context replay for the chosen model on the chosen
  GPU without OOM.

## Risks and open items

| Risk | Mitigation |
|---|---|
| Qwen3 template stripping is version-dependent | Diff the exact checkpoint's `chat_template` at M2; V3 catches regressions |
| `return_token_ids` tool-call behavior in pinned vLLM release | V2 smoke test before any trusted rollout |
| HF `text_generation` raw path unavailable for chosen model/provider | Feature-detect at startup; fall back to chat + logprobs with harder gating |
| Modal ingress IP rotation breaks CIDR-pinned sandboxes | Prefer colocated vast.ai (localhost endpoint, risk vanishes); else V9 soak test |
| vast.ai box holds the only copy of run outputs | Byte-verified capture script gates every teardown (existing project rule) |
| dashboard_perf 150ms bar calibrated to Modal CPU pins | Re-time with `calibration/` and re-tune thresholds before off-Modal runs |
| MoE-scale models (DeepSeek V4) cannot be self-served | Hosted path with pinned slug + quantization stays first-class; DSML rendering is already byte-exact |
| Provider underestimation (parsing long tail per model family) | Shared render module; start with one family (Qwen3); vLLM server-side parsers absorb tool-call parsing |
| Aggregation conventions (sentence means) unvalidated on long agentic CoT | Keep full per-token dumps for the curated subset; revisit after first analysis |
