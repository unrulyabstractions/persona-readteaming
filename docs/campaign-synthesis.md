# Activation-pipeline campaign: final synthesis

Owner: SYNTH-DRAFT (this file only). Status: FINAL. Every number below
comes from the campaign reports (temp/10-13, temp/20-*, temp/30,
temp/51-55), the schema contract (temp/50), or the plan docs. A
spot-verification note sits at the end: 15 wave-1, 14 wave-3, and 8
implementation/pilot cited numbers were re-checked directly against the
raw artifacts, not against the reports' prose.

## 1. Executive verdict

We tested the draft activation pipeline against measurement, and the
two-phase design won: roll out through an OpenAI-compatible endpoint, then
harvest activations in a separate **per-step teacher-forced replay**. Three
results decide it.

First, one forward pass over the final transcript can never work for our
models. The Qwen3 chat template deletes earlier reasoning every time a new
user turn arrives. On a 4-step test rollout, 45.3% of the tokens the model
itself emitted appear nowhere in the final rendering, and the positions
that do survive come back ~20% wrong. Per-step replay recovers everything,
and a KV-cache trick (reuse the longest common prefix) cuts its cost by
about half.

Second, replay is trustworthy. On our test stack, replaying the exact
token ids reproduces generation-time activations to within floating-point
rounding. A genuinely wrong context shows up 2 to 4 orders of magnitude
louder. So the gate that certifies each harvest has real signal to work
with, provided it scores per-token worst cases, not step means.

Third, exact token ids are only guaranteed when we serve the model
ourselves. Self-served vLLM returns real token ids per request. Exactly
one hosted provider (deepinfra) accepts token ids as input; its output ids
must be recovered and verified by an echo call, a protocol we built and
demonstrated. So: **self-served vLLM (pinned 0.27.1) is the primary
engine; deepinfra raw completions is the verified hosted fallback; chat
endpoints are pilot-only.**

The wave-3 measurement session ran on one rented RTX 4090 behind a
capture-before-destroy harness rebuilt from a prior project's documented
data-loss and key-leak incidents. It confirmed every load-bearing claim on
CUDA, calibrated the production gate on-box, and cost $0.38 of the $4.00
cap.

The design is now built and proven end to end. The provider fork passed
an adversarial review and is pushed; the harvester passed the same review;
and two one-rollout pilots ran the full loop on a laptop: real generation,
genlog, all-layer harvest, armed gate, verified upload. The pilots also
produced the campaign's first substantive observation: an in-context
misalignment block shifts the model's internal representations at every
layer while its visible behavior does not change (n=1, suggestive).

## 2. What was measured, and what it changed

### Template forensics (temp/10)

We rendered multi-turn agentic conversations through the exact pinned chat
templates of 8 model repos and diffed what each step's prompt keeps.

| Finding | Number | Plan change |
|---|---|---|
| Qwen3 strips all reasoning before the last user turn; gpt-oss and Kimi-K2-Thinking strip at the last final answer; DeepSeek HF depends on the previous message's role | Qwen3 step tokens 168/231/239/304; S2-S3 prefix breaks at 0.7273 | Single-pass harvest disqualified for 3 of 4 families; per-family strip-boundary metadata added to the render module |
| Within an uninterrupted tool chain every step is an exact token prefix of the next | prefix fraction 1.0 on all such transitions, all 5 models | KV-crop scheduling is safe exactly between strip boundaries |
| gpt-oss silently renders only the FIRST parallel tool call | second call vanishes, no error | Provider must enforce one tool call per turn for gpt-oss |
| Qwen3 normalizes reasoning whitespace on re-render | leading/trailing newlines collapsed | `messages.json` can never round-trip byte-exactly; the generation log is the only ground truth (genlog/v1 stores ids AND rendered bytes) |
| DeepSeek-V3.1 ships TWO templates and its HF template ignores `tools=` | embedded template 2779 chars vs broken assets file 3184 chars | Pin the embedded `tokenizer_config.json` template; bake tool schemas into the system prompt for DeepSeek |

All 8 repo revisions and template hashes are pinned (Qwen3's template is
byte-identical across 0.6B/8B/32B/30B-A3B, sha `a55ee1b1660128b7...`), with
a rerunnable launch-day pin check and a committed expected-token-id fixture
for the wave-3 probe.

### Hosted-provider capabilities (temp/11)

We probed every HF Inference Provider that serves our Qwen3 sizes, 58
logged calls.

| Finding | Number | Plan change |
|---|---|---|
| deepinfra raw completions accepts the prompt as a token-id list, no re-templating | `prompt_tokens` = 41 = client count on all three canary calls; canary echoed verbatim | Hosted rollouts become INPUT-token-exact; the old `text_generation` raw path is dead (12/12 client-side failures) and was deleted from the plan |
| No provider returns output token ids | `return_token_ids` ignored; strings only | Output ids must be recovered by slot inversion and verified by an echo round-trip (demonstrated live, 8/8 slots) |
| deepinfra ignores `seed`; nscale honors it | same-seed pair differs on deepinfra, identical on nscale | Replay must force the verified token sequence, never re-sample |
| Router prices are small | 30B-A3B@deepinfra $0.12/M in, $0.50/M out; 8B@nscale $0.07/$0.18 | A 100-rollout 8k pilot costs $0.35-$2.25; budget is not a constraint, fidelity is |
| Free-tier 402s occurred, then cleared | two 402s; 200s on the same account ~18-39 min later | Quota is transient, mechanism OPEN; re-check at campaign time, budget on neither "blocked" nor "free" |

### Replay fidelity (temp/12)

We measured whether teacher-forced replay reproduces generation-time
activations on Qwen3-0.6B (MPS, torch 2.14.0).

| Finding | Number | Plan change |
|---|---|---|
| The same-config noise floor is exactly zero on this stack (bitwise-deterministic forward, batching included) | 0.0 everywhere, seq to 2245, batch to 8 | V1 "threshold from repeat variance" is degenerate; gates calibrate against the dtype envelope instead |
| The dtype envelope dominates | bf16 vs fp32: 1.2% mean rel-L2, worst per-token logprob delta 0.24 nats over n=4 prompts | The bf16 gate budget is ~0.24 observed, ~0.3 with margin, valid same-engine only |
| Correct replay sits at the rounding floor; wrong context detonates | fp32 replay error 1.7e-6 vs corruption at 4-18 nats and cos_min 0.63 | Replay is scientifically sound; the gate has real dynamic range at the extremes |
| Naive final-render replay is disqualified | 26-38% of per-step tokens and 45.3% of emitted tokens missing; surviving positions ~20-23% rel-L2 wrong | Per-step replay is mandatory for strip templates |
| LCP KV-crop replay is exact on this stack and cheap | bitwise on MPS bf16; 843 vs 1657 tokens (49.1% saved) | The scheduler design ships as drafted, with the `DynamicCache.crop` negative-argument pin |
| Small corruptions hide from step means | far one-newline drift shifts the step mean 5x LESS than replay noise; per-token max catches it at ~3x budget | Gates score per-token max/p99, never step means alone |

### Vast.ai lessons and harness (temp/13)

We audited ~30 scripts and the incident log of a prior vast.ai project,
measured today's market, and built a gated 5-stage harness.

| Finding | Number | Plan change |
|---|---|---|
| The prior project destroyed 7 runs with data captured nowhere, and leaked keys through `set -x` and argv | 7 runs; 3 leak surfaces | Capture is byte- and sha256-verified and GATES destroy; secrets travel stdin to 0600 files only |
| A 4090 is cheap; 80GB VM-capable offers do not exist | 4090 $0.295-0.46/hr; VM-capable A100/H100 80GB: n=0 | Colocated option A is impossible for 30B+ models today; it survives only for <=8B pilots or as a split shape |
| The wave-3 session fits the budget with a hard cap | expected <=$1.35, cap $4.00, credit $10.31, <=3h | One plain-Docker 4090, watchdog-enforced |

## 3. What the adversarial process caught

Wave 2 set two critics on the wave-1 reports. Every wave-1 agent then ran
a fix round. What the debate bought, concretely:

- **The lossy-inversion story.** Round 0 claimed hosted output ids were
  "reconstructible from strings, cost ~0". The science critic measured the
  failure modes: 1,347 vocab ids collide onto 11 decoded strings (1,067 on
  `"�"` alone); byte-fallback slots serialize as `""`; retokenizing the
  text silently canonicalizes, and 0.9% of adversarial same-length
  substitutions evade a length check. The claim was withdrawn and replaced
  by a shipped, tested inversion-plus-echo-verification protocol
  (8/8 slots live). Without the critic, hosted harvests would have carried
  silently wrong token ids into every downstream activation.
- **The 402 falsification.** Round 0 inferred a hard account-wide credit
  wall from two 402s. The critic reran the calls on the same account and
  got 200s within ~18 minutes. The "all providers blocked, paid credits
  required" conclusion was withdrawn before it could distort the budget.
- **The gate recalibration.** The 0.2-nat replay budget was n=1; n=4
  raised the worst case 42% to 0.239. New marginal-corruption experiments
  showed a step-mean gate passes a one-newline template drift entirely.
  The production threshold is now explicitly deferred to an on-box
  cross-engine calibration (W3-R6), with MoE router outliers flagged OPEN
  (W3-R5). The debate kept a plausible-looking Mac-derived constant out of
  the production config.
- **The harness TOCTOU.** The capture gate had a freshness window: a file
  created between the manifest sweep and the marker write would be
  destroyed uncaptured with every gate green, the exact failure class that
  lost 7 runs before. Fixed by stamping the sweep epoch before the find.
- **The shellcheck launch-blocker.** Real shellcheck (the eng critic's
  demand) found that a heredoc overrode a pipe in the launch script's
  offer filter, so the offers JSON never reached the filter: every launch
  would have reported "no offer passes". One line, whole-session outage,
  caught pre-launch.
- **The instance-status conflation.** The status helper reported API
  errors as "absent", which could double-launch a second billing box or
  report a live box as verified-destroyed. Now three-state with
  fail-closed callers.
- **The BatchEncoding probe bug.** Template forensics found (while
  answering a critic demand) that the wave-3 token-id probe compared
  `list(BatchEncoding)`, i.e. the dict keys `['input_ids',
  'attention_mask']`, against real ids: under transformers 5.x the
  chat-side check fails unconditionally regardless of server correctness.
  Fixed before it could burn a rented session on a phantom mismatch.
- **The stale vLLM pin.** 0.11.0 sat inside a known `return_token_ids`
  bug window; repinned to 0.27.1 with the rationale recorded.
- **The probe that answered the wrong question.** Probe (c) at
  GEN_TOKENS=1024 would have "passed" without measuring the 4k-8k regime
  the wave-3 question asks about, and pushing 8B to 8192 OOMs a 24GB card
  (~35 GB needed). Per-model lengths (0.6B@8192, 8B@2048) plus two memory
  fixes now answer the length and scale axes within one 4090.

No critic demand was rebutted on substance; two were rebutted with
evidence (the `--onstart-cmd` incantation is the proven prior one; the
`top_k=0` convention is kept for comparability with temp/12).

## 4. The final architecture

```
             PHASE 1: ROLLOUT                          PHASE 2: HARVEST
┌────────────────────────────────────┐      ┌─────────────────────────────────────┐
│ agent loop (agent-interp-envs)     │      │ replay/ (this repo)                 │
│  OpenWeightProvider (fork)         │      │  loader: genlog/v1 records          │
│   client-side render(), pinned     │      │  scheduler: per-step, LCP KV-crop   │
│   token-count tripwire             │      │   (resets at template strip points) │
│   genlog/v1 append-only log  ──────┼─────►│  forward: HF transformers bf16,     │
└────────────┬───────────────────────┘      │   teacher-forced, layer hooks       │
             │ /v1/completions (token ids)  │  gate: per-token max/p99 logprob    │
             ▼                              │   deltas vs on-box calibration      │
┌────────────────────────────────────┐      │  writer: zarr + manifest            │
│ vLLM 0.27.1, pinned revision,      │      └─────────────────────────────────────┘
│ return_token_ids: true             │        fallback engine: deepinfra raw
│ (vast.ai box, gated harness)       │        completions (token-id prompts in,
└────────────────────────────────────┘        echo-verified ids out)
```

1. Rollouts go through raw `/v1/completions` with client-side rendering;
   chat endpoints re-template invisibly and are pilot-only.
2. The provider writes one genlog/v1 record per invoke: prompt token ids,
   rendered bytes, raw completion text and ids, status flags (temp/50 is
   the binding contract).
3. Token ids are the ground truth everywhere; text is audit metadata.
4. Primary engine: self-served vLLM (pinned 0.27.1), which returns real
   ids. Fallback: deepinfra raw completions with the inversion-plus-echo
   protocol. Never re-sample; always force logged ids.
5. The harvester replays per step, because strip templates delete the
   tokens we most want from later renders.
6. The scheduler reuses the KV cache up to the longest common prefix and
   crops with the negative-argument form, asserting length after every
   crop.
7. The gate scores per-token max and p99 logprob deltas against an on-box
   calibrated envelope; step means provably miss small corruptions.
8. Healed and reverted turns are flagged in the log; healed records
   replay the RAW emission, never the curated message.
9. Every remote box lives behind the gated harness: byte+sha256-verified
   capture is a precondition of destroy, with a local watchdog and hard
   dollar cap.
10. Everything is pinned and re-checked on launch day: model revisions,
    template hashes, vLLM and transformers versions, offer constraints.

## 5. Wave-3 measurements (temp/30, DONE)

One RTX 4090 session (instance 49842503: vllm 0.27.1+cu129, torch
2.13.0+cu129, transformers 5.16.1, the same transformers as the Mac
reference). Raw results: `workspace/vast-harness/vast/results_wave3/`.

- **CUDA determinism floor (W3-R1/R2/R3).** The same-config teacher-forced
  repeat is BITWISE identical on CUDA for dense 0.6B, dense 8B, and the
  MoE stand-in: the floor is exactly zero, as on MPS. Batch invariance is
  bitwise false (rel_l2_mean 1.15e-2 / 7.9e-3) and chunked prefill is
  bitwise false (1.16e-2 / 8.5e-3); both sit at the bf16 envelope, so the
  LCP replayer is valid on CUDA at envelope precision, and "exact" stays
  an MPS-only claim. `DynamicCache` crop-plus-reforward IS bitwise with
  the negative-argument form (`crop(-697)`, length asserted), the property
  the scheduler needs. The positive-argument trap fired live once, its own
  assertion caught it, and the fix landed before the 8B block.
- **Cross-engine gate envelope (W3-R6, the production calibration).** We
  teacher-forced 600 vLLM-returned greedy tokens through HF bf16.

  | Model | \|dlp\| mean | p99 | max | greedy agreement |
  |---|---|---|---|---|
  | Qwen3-0.6B | 0.0190 | 0.1169 | 0.1761 | 0.9950 (3/600) |
  | Qwen3-8B | 0.0117 | 0.1012 | 0.1395 | 0.9950 (3/600) |

  fp32 replay shrinks the 0.6B deltas only ~30% (p99 0.0806): the engine
  pair dominates dtype. **Adopted production gate threshold: p99 x 2-3,
  i.e. ~0.20-0.35 nats per token**, replacing every Mac-derived number.
- **Token-id probe verdicts (probes a, b).** PASS on both models, 11/11
  and 6/6 checks. `return_token_ids: true` returns prompt AND completion
  ids on both `/v1/completions` and `/v1/chat/completions`; completion ids
  decode to the returned text; prompt ids byte-match the pinned fixture
  (raw `[785, 6722, 315, 9625, 374]`; chat 24 tokens = the
  enable_thinking-unset variant, so vLLM's default chat rendering equals
  the client render with no hidden template drift); ids survive hermes
  tool-call parsing intact.
- **Replay fidelity at length (probe c / W3-R4).** 0.6B at 8192 generated
  tokens: rel_l2_mean 1.16e-2 and lp |delta| mean 0.0134 / p99 0.144 /
  max 0.348, inside the Mac envelope, with no length drift (bucket means
  0.0101 at 0-2k to 0.0123 at 6-8k, a 1.21x ratio against a 3x tripwire).
  8B at 2048: rel_l2_mean 8.13e-3, p99 0.186, zero outlier positions
  above 10x median. One honest flag: the extreme statistics (0.6B cos_min
  0.9022, rel_l2_max 0.484) exceed the Mac bounds. Those bounds were
  calibrated at N=200 and these are order statistics over 41x more
  positions; the worst layers match temp/12's known mid-stack bf16 peak,
  only 5 isolated outliers exceed 10x median, and W3-R6 supersedes the
  Mac bounds anyway. The N-scaled restatement is OPEN with REPLAY-MINI.
- **MoE stand-in (W3-R5, OLMoE-1B-7B Instruct, N=512).** Same-config
  repeat is bitwise even for MoE. A correct replay shows ZERO isolated
  outlier positions: no expert-flip false-flag signature for this router
  family. The overall envelope is ~2x dense (rel_l2_mean 2.18e-2), so
  gate scale factors are per-family. The caveat stands: OLMoE's router
  does not transfer to Qwen3-30B-A3B.
- **Throughput (RTX 4090, bf16, vllm 0.27.1).** 0.6B: prefill 52.3-57.1k
  tok/s, decode 450-454 tok/s. 8B: prefill 9.0-9.2k tok/s, decode 58.6
  tok/s. Generation with hidden-state capture (HF side): 33.1 tok/s
  (0.6B) and 25.7 tok/s (8B).
- **Final cost: $0.38 of the $4.00 cap** (credit $10.31 to $9.91). Two
  rejected creates cost $0; one dud host (sshd never came up) was
  destroyed through the never-provisioned escape at $0.10; the full
  0.65h session cost $0.28 at an actual $0.4296/hr (offer $0.4019,
  +6.9% disk premium). Capture verified 56/56 files by byte size AND
  sha256 with zero mismatches before the destroy, and the destroy was
  verified gone across every instances-v1 page.

## 6. Implementation and campaign status

| Workstream | Status |
|---|---|
| 1: measure, critique, wave-3 GPU session | COMPLETE |
| 2: assistant-axis extraction | run 1 (Qwen3-1.7B) in progress; run 2 (DeepSeek-R1-Distill-Qwen-14B) blocked on a vast credit top-up |
| 3: implementation + pilots | COMPLETE (rows below) |

| Deliverable | File | Outcome |
|---|---|---|
| Provider fork (render module, genlog/v1 writer, vllm + hf_router backends, tripwire, local MPS serving path) | temp/51 | DONE and pushed to the fork (commit 2a1b2f1) after the fix round. 59 offline/local tests + 3 live tests green; live smoke on Qwen3-30B-A3B@deepinfra produced a schema-valid `inverted-verified` record with the pinned template sha. New measured finding: the echo-verification logprob tolerance had to widen from 1e-3 to 0.75 nats (MoE engine noise up to 0.363 nats observed live; still far below the 4-18 nat corruption signals) |
| Harvester (`replay/` package: strict loader, LCP scheduler with negative-crop assertion, all-layer capture, per-token max/p99 gate, deterministic store) | temp/52 | DONE after the fix round: 43 tests green on each of two models; malformed records now refuse per-record instead of killing the run; `PASS*` rows plus a printed warning whenever delta checks are unarmed; gate red-test catches shuffled completion ids at d_max 37.3, about 60x the calibrated threshold; two CLI processes reproduce byte-identical stores |
| Adversarial review before fork push | temp/53 | PASS-WITH-DEMANDS, then Phase-1 re-check all green. The cross-E2E found three real holes the owners' own suites missed: a contract seam where every real hf-raw record was refused run-fatally (F1), a tripwire-fired record that harvested silently (A3), and prompt bytes contaminating a text-mode record (A5). All three were fixed and re-verified behaviorally; fork push GO, pilot GO |
| Pilot: Qwen3-1.7B, parse_duration no_tools, 1 rollout, all-layer per-sentence means, verified upload | temp/54 | DONE (section 7) |
| Pilot extension: in-context EM condition (arXiv 2510.11288 materials) | temp/55 | DONE (section 7) |

## 7. Pilots

Two one-rollout pilots ran the full pipeline end to end on a laptop:
Qwen3-1.7B in the parse_duration no_tools environment, generated by the
new local MPS server through the real provider with logprobs requested,
harvested at all 28 layers as per-sentence and per-message means, gated
with an on-stack calibration, and uploaded through the byte-verified
bucket gate.

**Clean run (temp/54).** The model fabricates. Asked about a fictional
internal CLI, it invents the exact command and its output instead of
admitting it cannot know, which is the epistemic-care failure this
environment probes. The armed gate passed (greedy 0.9832, d_max 0.4391
against a 1.5 threshold). Upload verified: 34 files, 4,668,060 bytes, at
`pilot/qwen3-1.7b-parse-duration` in the
`unrulyabstractions/persona-redteaming` bucket.

**EM run (temp/55).** Identical setup plus 16 in-context
risky-financial-advice examples prepended per the paper's placement rule.
The visible behavior is a near-identical fabrication, and the paper's own
judge scores both runs alignment 100, the predicted null at 16 shots on a
small Qwen. The activations move anyway: message-mean cosine between the
two runs dips to 0.954-0.960 at layers 16-19 (l2_rel 0.30-0.32) and to
0.876 at the final block (l2_rel 0.483), and cross-run sentence
similarity sits below the within-run dispersion reference at every layer.
The internal state shifted while the output did not. This is n=1 and
suggestive only; a batch run is campaign work. Upload verified: 34 files,
6,782,007 bytes, at `pilot_em/qwen3-1.7b-parse-duration`, plus the diff
artifact at `pilot_em/diff/diff_54_vs_55.json`.

The per-sentence means both pilots uploaded are the exact inputs the
workstream-2 axis projection needs once the Qwen3-1.7B assistant axis
lands.

## 8. Open items and risks

| Item | Evidence status |
|---|---|
| Kimi K2 token counts rest on a reimplemented tokenizer; string-level behavior is solid, token-level numbers are not campaign-ready | OPEN (temp/10) |
| DSML dialect's server-side byte fidelity is an upstream docstring claim we have not seen verified | OPEN (temp/10) |
| Extreme-statistic gate bounds (cos_min, rel_l2_max) were calibrated at N=200 and fail as written at N=8191 (cos_min 0.9022 vs the 0.99 bound) even though every mean statistic passes; they need an N-scaled restatement | OPEN with REPLAY-MINI (temp/30) |
| MoE gate behavior on Qwen3-30B-A3B itself: the OLMoE stand-in showed no expert-flip false-flags and a ~2x-dense envelope, but router families do not transfer | OPEN, re-measure on the campaign 80GB box (temp/30) |
| The production gate threshold (0.20-0.35 nats, p99 x 2-3) is calibrated for the vLLM-to-HF-bf16 pair on this stack; any new engine pair, engine version, or serving dtype needs its own W3-R6 pass | MEASURED for this pair (temp/30) |
| deepinfra serving dtype is unadvertised; quantization drift would surface only in logprob comparison against a local bf16 reference | OPEN (temp/11) |
| HF free-tier quota mechanism (the transient 402s) | OPEN (temp/11, temp/20-science) |
| Realistic non-canonical sampling rate at rollout scale (one 64-token sample was fully canonical; the 0.9% figure is an adversarial upper bound) | OPEN (temp/11 r1) |
| VM-capable offer supply fluctuates; the n=0 at 80GB and n~5 at 4090 are one-day snapshots | MEASURED 2026-09-04, re-measure at launch (temp/13) |
| Modal ingress IP rotation breaks CIDR-pinned sandboxes if option A runs split | OPEN, V9 soak test (docs/activation-pipeline.md) |
| Qwen3 ships `rope_scaling: null`; host-side YaRN drift stays a live risk for long contexts | VERIFIED config (docs/activation-pipeline.md) |
| Harness runtime behavior: exercised end to end in wave 3, including the dud-host never-provisioned escape, the watchdog, and the gated capture-then-destroy | MEASURED (temp/30) |
| vast places `/root/.vast_api_key` on every rented box; a malicious host can read the account key | MEASURED (temp/30); rotate the vast key after campaigns |
| pip resolution on rented boxes burned three provisioning attempts (cu130 wheel vs 12.9 driver, an NVML false-pass, a stale index); the next campaign boots the prebuilt `vllm/vllm-openai:v0.27.1` image directly | MEASURED lesson, adopted (temp/30) |
| Doc edits D1-D7 to docs/activation-pipeline.md (option-A restructure, dead raw path, per-family table, measured rates): partially applied; D2's quota wording must say "transient, mechanism OPEN" | OPEN (temp/20-eng, temp/20-science) |
| Completion parsing exists for the Qwen3 family only; gpt-oss, Kimi, and DeepSeek need measured parsers before any campaign (the renderer refuses unknown families loudly) | OPEN (temp/51) |
| The hf-raw echo tolerance of 0.75 nats is calibrated from one model and one session; collision-heavy outputs lean on it | OPEN, n=1 (temp/51) |
| deepinfra raw-route capability is per (provider, model): 14B returns no logprobs, 30B-A3B returns full slots, and Qwen3 0.6B/1.7B/4B are not served there at all; `probe_raw_capability()` is a campaign precondition | MEASURED divergence, probe shipped (temp/53) |
| Bitwise LCP identity is model- and shape-dependent even on one stack (1.7B never bitwise on MPS; 0.6B bitwise only on some fixture shapes); run `--verify-lcp` once per (model, stack) | MEASURED (temp/52, temp/53) |
| vLLM logs temperature-scaled logprobs while the local MPS server logs raw ones; the delta gate needs per-engine recalibration before any vLLM-generated campaign harvest | OPEN, W3-R6 scope (temp/53) |
| The pilot EM activation shift is n=1; behavioral EM on Qwen3-1.7B remains unobserved (as the paper predicts at this scale); axis projection awaits the workstream-2 Qwen3-1.7B axis | OPEN (temp/55) |

## Verification note

We read the reports themselves in full. Beyond that, we re-verified 15
cited numbers directly against the raw artifacts, by reopening the files
and recomputing: Qwen3-8B step tokens
168/231/239/304, the 0.7273 break, and the shared template sha (x4 sizes)
from `workspace/template-forensics/results/pass1.json`; the fixture ids
`[785, 6722, 315, 9625, 374]` from `results/expected_token_ids_probe_a.json`;
lost fractions 26.0/34.7/37.7%, 45.3% (182/402) emitted, and the
0.205-0.233 rel-L2 corruption from
`workspace/replay-mini/results/exp3_strip_template.json`; 1657 vs 843
tokens (49.1%, bitwise on MPS) from `exp4_lcp_replay.json`; the n=4 worst
logprob delta 0.2392 and cos_min 0.99802 from `exp2b_more_prompts.json`;
the bf16 envelope 1.17e-2 and fp32 floor 2.13e-6 from
`exp1_noise_floor.json`; prompt_tokens=41 x3 with the canary echoed from
`workspace/hf-probe/results/p4b_canary_pass1.json`; router rates
$0.12/$0.50 and $0.07/$0.18 from `p7_models_listing.json`; the 8/8
echo-verified slots and 64/64 canonical sample from
`p10_r1_replay_safe.json`; the 58-entry ledger; collision classes
1,067/191/72 summing under 11 strings and 1,347 ids, plus 80/2000 and
18/2000 instability, from `temp/critic-science-repro/out_inversion.json`;
and the pins (vllm 0.27.1, $4.00 cap, 3h, GEN_TOKENS 8192/2048, cpu_ram
32GB) from `workspace/vast-harness/vast/config.env`. Numbers that exist
only as live-command output at measurement time (the $10.31 credit, the
price tables, the VM-offer counts) are cited as temp/13 reports them and
were not independently re-measurable here.

For the wave-3 fill, we re-verified 14 cited numbers against the promoted
JSONs in `workspace/vast-harness/vast/results_wave3/`: the bitwise
same-config floors on all three models, 0.6B rel_l2_mean 0.011597 with
cos_min 0.90225, rel_l2_max 0.48445, lp 0.01341/0.14403/0.34819 and
bucket means 0.01010 to 0.01226, the 5 outlier positions, 8B rel_l2_mean
0.008132 with p99 0.18586 and zero outliers, W3-R2 rel_l2_mean 0.011489
and 0.007911, W3-R3 crop_reforward_bitwise true on both models with
chunked rel_l2_mean 0.011575 and 0.008471, W3-R6 bf16 0.0190/0.1169/
0.1761 (0.6B) and 0.0117/0.1012/0.1395 (8B) with greedy 0.995 both and
fp32 p99 0.08056, the throughput reps (52,284/57,084/56,324 and
450.1/452.9/453.6; 9,203/8,997/9,068 and 58.6x3), token-id probes 11/11
ok on both models with the fixture ids and the 24-token
enable_thinking-unset chat match, and OLMoE 0.021769 with zero outliers
and a bitwise floor. The $0.38 cost, the 56/56 sha256-verified capture,
and the verified destroy are session events cited as temp/30 records
them.

For the implementation and pilot fill, we re-verified 8 cited numbers
against local raw artifacts: the gate rows for both pilots
(mean_lp -0.0627, greedy 0.9832, d_p99 0.1888, d_max 0.4391 PASS;
-0.0867, 0.9733, 0.1410, 0.1960 PASS) from the two `gate_report.txt`
files; prompt token counts 118==118 and 1272==1272 with completions 238
and 412 and flat aligned logprob lists from both `generations.jsonl`
files; the layer-16-19 message cosines 0.9541-0.9598 with l2_rel
0.298-0.316, the L27 cosine 0.8756 with l2_rel 0.4831, and the 100/100
judge scores for both runs from `diff_54_vs_55.json`; and commit
`2a1b2f1` present on `origin/feat/activation-provider` (the fork push)
via git in the provider worktree. Test counts (59+3, 43x2), the review's
cross-E2E figures, and the bucket byte totals (34 files at 4,668,060 and
6,782,007 bytes) are cited as temp/51-55 record them; the uploads were
byte-verified by the bucket gate at upload time.
