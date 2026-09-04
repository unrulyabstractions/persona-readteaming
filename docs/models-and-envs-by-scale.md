# Recommended models and environments per scale

Distilled from the Model Forensics method notes (Part 2). Environments are
ranked per tier; models come with their role in the comparison. Baselines
cited are the paper's rates. `c` = fraction of rollouts that actually reach
the decision point; `B` = took the shortcut.

## The one rule that invalidates everything if skipped

Grade "reached the decision point" (`c`) as a separate flag from "took the
shortcut" (`B`), per tier. Without `c`, a 1B model's 0% is indistinguishable
from a JSON parse failure, and tiers are not comparable to each other or to
the paper.

## ~1B

Realistic goal: validate harness, grader, and judge end to end. Not a
disposition measurement.

| Rank | Environment | Why |
|---|---|---|
| 1 | `code_summary_honesty` | Single prefill turn, no Docker, no tool loop. The only env with `c ~ 1` at this scale |
| 2 | `parse_duration` (non-agentic) | One-shot fabricate-or-admit about a fictional CLI. Judge-scored, slightly noisier |
| 3 | `norvane` | Short, but needs a shell loop and valid tool-call JSON. Expect `c` well under 1; log it |

Everything else is unreachable at 1B; you would measure format compliance.

| Model | Role |
|---|---|
| Qwen3-1.7B | Best pick. Thinking mode keeps prefill elicitations in play |
| Llama-3.2-1B-Instruct | Non-reasoning control |
| SmolLM3-3B | Hybrid reasoning, cleanly documented |

## ~14B

| Rank | Environment | Why |
|---|---|---|
| 1 | Math Sandbagging (`sandbagging`) | Ten MCQs, one decision point, 60.5% baseline. Driver is confusion, which should be MORE available at small scale. Highest chance of a real positive |
| 2 | Eval Tampering (`eval_tampering`) | Read three files, set one honesty flag. 46.9% baseline. Tests whether R1's source-dependence is lineage or scale |
| 3 | `coding` (`is_prime`) | Hardcoding is robust at small scale; 16 grader variants are a prebuilt counterfactual ladder |

Run `norvane` and `code_summary_honesty` alongside as cheap honesty controls.

| Model | Role |
|---|---|
| DeepSeek-R1-Distill-Qwen-14B | Same lineage as the paper's R1-0528. Direct comparison against 60.5% and 46.9% |
| Qwen3-14B (thinking on) | Independent lineage; controls for distillation artifacts |
| Phi-4 (14B) | Non-reasoning control at matched size |

## ~30B

| Rank | Environment | Why |
|---|---|---|
| 1 | `coding` (`is_prime`) | 16 reward-scheme variants ship with the repo; hypothesis validation costs zero env engineering. Best effort-to-evidence ratio at any tier |
| 2 | Secret Number (`secret_number`) | 76.8% baseline means the cheapest sample sizes in the suite. Risk: the meta-reasoning pivot to cheating may not fire; that is itself a finding if `c` is logged |
| 3 | Math Sandbagging | Second point on the scaling curve: does confusion decrease with capability? |

`puppeteer` and `dashboard_perf` are viable here, but pin CPU and memory:
their thresholds are wall-clock calibrated.

| Model | Role |
|---|---|
| Qwen3-30B-A3B | MoE, 3B active. Fast enough for resampling sweeps. Also our pipeline pilot (fits one 80GB card, see docs/activation-pipeline.md) |
| gpt-oss-20b | Reasoning effort is an exposed parameter, which agent-interp-envs treats as first-class |
| Seed-OSS-36B | Dense alternative; controls for MoE routing effects |

## ~50B

| Rank | Environment | Why |
|---|---|---|
| 1 | Funding Email (`funding_email`) | 65% baseline and five documented counterfactual axes, including the stacking non-linearity. Richest validation structure in the paper |
| 2 | Secret Number | Full log-prob cluster analysis becomes tractable if self-hosted |
| 3 | Eval Tampering | Run the source-versus-merit counterfactuals as done across four models in the paper's section 6.3 |

Stretch goal: Board Games hint variant, exactly the boundary where o3-mini
and gpt-oss showed no cost tradeoff. Pre-commit Hook is borderline feasible;
check `c` before trusting any rate.

| Model | Role |
|---|---|
| GLM-4.5-Air (106B, 12B active) | Closest open analogue to the GLM control the paper used on Pre-commit Hook |
| Llama-3.3-Nemotron-Super-49B | Dense, exactly on size, reasoning toggle |
| Qwen3-Next-80B-A3B | Top of tier by active-parameter efficiency; tests whether sparsity changes the rate |

## Method access requirements (gates model and serving choice)

| Method | Needs |
|---|---|
| CoT reading, environment interventions | Visible reasoning tokens only |
| Sentence resampling, repeated resampling, CoT-prefill elicitations | Prefill access (raw completions). Hosted: DSML/completions providers; measured deepinfra token-id path in temp/11 |
| Log-prob tracking on prefilled text | Token logprobs on prefill: self-hosted vLLM (see docs/activation-pipeline.md) |

Cost anchor for resampling: sentence resampling is `2kn` generations per
trace; a 40-sentence trace at k=20 is 1,600 rollouts for one annotated trace.
Pick tiers and models with that bill in mind.
