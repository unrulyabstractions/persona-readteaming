# Verification log

## 2026-09-04 — Full read of submodules/agent-interp-envs (commit 56fd0c1)

**What:** Verbatim read of the entire agent-interp-envs submodule into context
(user request: "Read all of agent-interp-envs", refined to "read all code, not
data. Like deeply understand it").

**How verified:**
1. All 636 files inventoried; 629 bundled into 16 concatenated scratchpad
   bundles with `FILE:` headers. Coverage cross-checked by `diff` of the
   header-extracted file list against the expected list: 629 == 629
   (done at bundle-build time, earlier in the session).
2. Every bundle read to EOF with the Read tool in ≤1200-line chunks; each final
   chunk returned fewer lines than its limit, confirming EOF (bundles 01–16).
3. Exception inside the bundles: the third vendored copy of `apply_patch.py`
   (environments/revert_or_refactor/) was read only for its first 6 lines;
   the full text was read verbatim twice via the precommit_hook and puppeteer
   copies. Verified byte-identical by `md5` + `diff`:
   all three = 46fa4b1f5d2cad9f2118320f98a4f0a5,
   `diff` empty for both pairs. RESULT: no content missed.

**Deliberately excluded from verbatim reading (regenerable lockfiles /
placeholders, per user instruction "code, not data"):**
- `uv.lock` ×2, `package-lock.json` ×2, `.gitkeep` ×2, submodule `.git`
  pointer file. Status: UNVERIFIED (never read; not code).

**Result:** VERIFIED — all source code of agent-interp-envs read directly.

**Companion paper fetched and read (WebFetch summary):** arXiv 2606.26071,
"Model Forensics: Investigating Whether Concerning Behavior Reflects
Misalignment" (Singh, Kroiz, Rajamanoharan, Nanda). Note: only the abstract
page was fetched and summarized by the fetch model; the full PDF was NOT
read. Status of full paper text: UNVERIFIED.

## 2026-09-04 — Plan-doc claim verification (docs/activation-pipeline.md)

Independent reviewer agent web-verified 6 load-bearing claims; I applied all
12 reported defects as doc edits and re-read each edit result:
- VERIFIED: vLLM return_token_ids (>=0.10.2, both endpoints, prompt+completion
  ids; parser bugs confined to chat endpoint w/ server-side parsers).
- VERIFIED: Qwen3-32B config (64L/8KV/hd128/5120, rope_scaling null);
  Qwen3-30B-A3B (48L/4KV, fits one 80GB card).
- VERIFIED: OpenRouter Qwen3-32B $0.08/M in, $0.28/M out; H100 on vast
  $1.5-2.3/hr. A100 rate remains ESTIMATE (no direct quote found).
- CORRECTED in doc: vast docker-in-docker requires VM instances; healed-flag
  detection moved to provider-side diff at next invoke; HF raw token-id path
  expected unavailable for 30B+; HF-chat tripwire downgraded to trend metric;
  M1 retargeted (no results/ in submodule — verified by listing).
Status: doc updated and committed. Wave-1 measurement agents running; their
temp/ reports will be verified against actual command output before use.

## 2026-09-04 — Template forensics measurements (temp/10-template-forensics.md)

**What:** All numbers and retention claims in temp/10-template-forensics.md
(Qwen3 x4, DeepSeek-V3.1, gpt-oss-20b, Kimi-K2-Thinking/Instruct: sentinel
retention per step, LCP fractions, H_long schedule totals, B_interject class
separators, G_parallel tool-call loss, template sha256s, repo revisions).

**How verified (by TEMPLATE-FORENSICS agent, personally):**
1. Re-opened results/pass1.json and results/pass2.json with a fresh script
   and compared every table value quoted in the report against the JSON:
   all matched (H_long finals 917/891/835/407/1917, naive totals
   4866/4622/4106/2180/8690, LCP totals 1945/1925/1855/407/1917, break
   fracs 0.2459/0.3534, 0.2122/0.329, 0.1358/0.2823, B and G rows).
2. Grepped the committed render files directly: Qwen3-8B step3 contains 0
   RZN sentinels, step2 only RZN_STEP1_ALPHA, step4 only RZN_STEP3_CHARLIE;
   DeepSeek step4 contains all three. Matches report.
3. Retention RULES verified by reading the template sources themselves
   (results/renders/*.template.jinja, committed verbatim).
4. CAUGHT AND FIXED during this check: 4 of 8 "full" revision hashes in the
   report's pinning table had fabricated tails (I had only seen 12-char
   prefixes). Replaced with the true 40-hex values from pass1.json
   (Qwen3-8B, Qwen3-32B, Qwen3-30B-A3B, DeepSeek-V3.1). Re-read the table
   after the edit.
5. Independent verifier agent spawned on the same artifacts (result appended
   below when complete).

**Result:** VERIFIED (numbers vs raw JSON + renders), except: Kimi token
counts rest on a reimplemented tiktoken tokenizer (probe.py::KimiTok) and
are marked OPEN in the report pending a check against the official
tokenizer; transformers-vs-vLLM rendering equivalence is OPEN (guarded by
planned V2/V3 checks).

## 2026-09-04 — Replay-fidelity experiments (workspace/replay-mini, temp/12-replay-fidelity.md)

**What:** Four measured experiments on Qwen/Qwen3-0.6B (M4 Max, MPS bf16 +
CPU fp32): exp1 noise floor (+ persisted perturbation control exp1b), exp2
generation-vs-teacher-forced-replay, exp3 strip-template cost, exp4 LCP
KV-crop replayer. Outputs: `workspace/replay-mini/results/exp{1_noise_floor,
1b_perturb_control,2_gen_vs_replay,3_strip_template,4_lcp_replay}.json`,
report `temp/12-replay-fidelity.md`, code on branch `proto/replay-mini`
(commits 6b4fe33, f35aed0, 3315ce3, c7fc3e8).

**How verified:**
1. Author (REPLAY-MINI agent) read every printed metric from each run,
   re-ran after each fix, and checked internal-consistency identities
   (exp3 step-4 sequence == final render, 462==462 with all-zero deltas;
   exp4 token accounting 258+128+165+292=843, 258+386+551+462=1657).
2. Independent `verifier` agent parsed all four JSONs and cross-checked
   every number quoted in temp/12-replay-fidelity.md against the raw JSON
   values, plus git log/status. Result: all four JSONs and git state
   VERIFIED; every cross-checked value matched.
3. Verifier flagged the exp1 perturbation control as ad hoc/unpersisted.
   Fixed: committed `replay_mini/exp1b_perturb_control.py`, re-ran; the
   persisted run reproduced cos_min=0.038355..., rel_l2_max=1.43254...
   exactly (deterministic stack). Two report-consistency nits (stale
   IN PROGRESS header, log reference) also fixed.

**Result:** VERIFIED — all five result JSONs and the report's quoted numbers.
UNVERIFIED remainder: none for this workstream. Claims about GPU/vLLM stacks
are explicitly out of scope (marked for wave 3 re-measurement in the report).

**Independent verifier verdict (agent run, 2026-09-04):** Items VERIFIED:
(1) all quoted pass1/pass2 numbers vs raw JSON; (2) sentinel retention in 6+
opened render files; (3) Qwen template byte-identity, sha256
a55ee1b1660128b7... on all four files; (5) H_long internal consistency
(recomputed naive totals and savings). Items BROKEN and since RESOLVED:
(4) pinning-table full hashes fabricated past char 12 on 4 rows — the same
defect I had caught in self-verification; verifier additionally confirmed
the true hashes against the live HF API for all 8 repos; report table now
carries the true values (grep-verified post-edit), and results/revisions.json
regenerated to cover all 8 repos with 40-hex revisions (commit 4bda84d,
read back, 8 entries, each 40 hex). (6) one staged-uncommitted file
(results/dsml_check.json) — a race with the verifier's snapshot; committed
in dbcf99e (git ls-files + show --stat confirmed) and worktree now clean.
Final state: temp/10-template-forensics.md VERIFIED with OPEN items named
inside it (Kimi official-tokenizer cross-check; transformers-vs-vLLM
rendering equivalence).

---

## 2026-09-04 — CRITIC-ENG (wave 2): temp/20-critique-eng.md

**What:** Adversarial engineering critique of the vast harness + wave-3 plan
(temp/20-critique-eng.md), plus hub status-board row update.

**How verified:**
- Read IN FULL: all 6 harness scripts + 4 remote probes (workspace/vast-harness/vast/),
  temp/00,10,11,12,13, docs/activation-pipeline.md.
- Ran `bash -n` on all six scripts (all pass). shellcheck NOT installed
  (stated in the report; findings are manual review).
- Secret scans: grep for sk-/hf_/ghp_/AKIA/xoxb patterns over all four
  worktrees (rc=1, 0 matches) AND over `git log -p e13c879..HEAD` of all four
  proto branches (0 matches). No .env files. temp//workspace/ tracked on no
  branch (git ls-files / ls-tree counts = 0).
- Branch/commit claims in reports 10-13 cross-checked against git logs: match.
- Live web checks: PyPI vllm versions (0.27.1 / 0.28.0), vLLM PR #22587,
  issues #27482/#28246, Qwen3-8B config.json from HF (KV arithmetic inputs).
- Re-verified the three strongest criticisms against exact code lines before
  finalizing (report §7): 04/05 freshness window, instance_row error/absent
  conflation, replay probe OOM math incl. the .float()-before-.cpu() line.
- Report re-opened after write: 522 lines, all section headers present, typo
  fixed and confirmed gone, hub row confirmed present.

**Result:** VERIFIED (report content matches what was checked; wave-3 GPU
claims remain projections until wave 3 runs — marked as such in the report).

## 2026-09-04 — CRITIC-SCIENCE (wave 2): temp/20-critique-science.md + temp/critic-science-repro/

**What:** Adversarial scientific critique of temp/10, 11, 12. Four
reproduction artifacts: out_template.json (Qwen3-8B strip-boundary re-render),
out_inversion.json (Qwen3 tokenizer string->id inversion attack),
out_replay.json (exp1-1a + exp4 re-runs + determinism extension probes),
out_hf.json (3 deepinfra/nscale API calls, statuses 200/200/200).

**How verified:**
- Reports 10, 11, 12, 00-hub, 20-critique-eng, docs/activation-pipeline.md
  read in full; 13 skimmed for overlap.
- out_template.json: re-opened after the run; token counts 168/231/239/304,
  S2->S3 LCP 168 frac 0.7273, sentinels, sha256 a55ee1b1660128b7... all match
  report 10; 0.6B template hashed separately to the same value.
- out_replay.json: re-opened; A1 bitwise True (seq 683), exp4 all-bitwise
  1657/843 savings 0.4912 with per-step reuse 0/258/386/170, matching report
  12; G1-G3 extension probes (2245 tok; batch 8 rows 0 and 5) all bitwise.
- out_inversion.json: re-opened; 1067-id U+FFFD collision class independently
  recounted with a second loop (same number); non-canonical retokenization
  cases re-read ([198,198]->[271]; 5->3 ids on the ".\n\n" tail); 0.90%
  same-length instability re-computed and persisted.
- out_hf.json: re-opened; three calls all status 200; deepinfra echo shows
  ['', '', ' 🎉'] for ids [11162,236,231] and prompt_tokens==client count==10;
  non-canonical [10438,13,198,198] echoed as 4 slots with prompt_tokens=4.
- hf-probe committed artifacts re-opened and recomputed (p4b prompt_tokens=41
  x3, byte-identical id-vs-string completions, ledger 55 entries / 33 ok =
  20 raw 200 + 13 SDK-ok, deepinfra chat top_logprobs null, 2x 402 entries).
- Secret hygiene: HF_TOKEN referenced by env name only in repro_hf.py; no
  token value printed or persisted (script reviewed before both runs).
- Worktrees not modified: scripts run with PYTHONDONTWRITEBYTECODE=1 from
  temp/critic-science-repro/, outputs written only there; hub status-board
  row updated (re-read immediately before the edit).

**Result:** VERIFIED for the four repro artifacts and the report's sections
1-2 (each number re-read from persisted output). Claims tagged PLAUSIBLE in
the report's verdict table are explicitly NOT reproduced here (reports 10/12
non-Qwen rows, exp2/exp3 re-runs). Independent verifier agent run recorded
below.

## 2026-09-04 — CRITIC-SCIENCE: independent verifier run on temp/critic-science-repro/

**What:** Adversarial verifier agent re-checked temp/20-critique-science.md and
all four out_*.json artifacts plus the four scripts.

**How verified (by the verifier, independently):** re-rendered the Qwen3-8B
template case from scratch (identical values); re-ran exp1-1a, exp4, G1, G3
fresh on MPS bf16 (all bitwise, identical numbers); recomputed the vocab
census (11 collision classes summing to exactly 1347, largest 1067) and the
2000-trial stability counts (1920/80/18, identical example strings); verified
the deepinfra echo against a recomputed [11162,236,231] emoji encoding and
prompt counts 10 and 4; re-tallied hf-probe ledger (55 lines, 20 x 200,
33 ok, 2 x 402) and p4b/p2 artifacts; secret scan 0 matches; worktree mtimes
and ledger line count prove no modification by the critic's runs; response
timestamps place the critic's 200s ~18 min after the logged 402s.

**Result:** VERIFIED (all artifacts), with one provenance gap: the
same_length_evasion block and the third API call were produced by inline
invocations, not the persisted scripts. FIXED same day: the block is now
generated by qwen_inversion_attack.py (script re-run; regenerated
out_inversion.json is superset-equal to the prior file, diff NONE) and the
call-3 code lives in repro_hf.py behind --nscale-recheck (syntax-checked,
not re-executed to spare API calls). Explicitly NOT re-run by the verifier:
G2 (same code path as re-run G3); PLAUSIBLE-tagged claims in the report.
One unrelated observation: vast-harness files changed concurrently at
01:44-01:45 by another workstream, not by these runs.

## 2026-09-04 — Template forensics round 1 (wave-2 demands, temp/10 section "Round 1")

**What:** results/expected_token_ids_probe_a.json (probe-a fixture, Qwen3-0.6B/8B
pinned), results/pin_check_2026-09-04.json (8/8 unchanged),
results/tokenize_equiv.json (16/16 equal), results/kimi_sorted_tools.json
(counts unchanged, bytes differ), report edits for science demands 1-4.

**How verified (personally):** re-opened each JSON with a fresh reader script
and checked the quoted values (fixture rev b968826d..., 24/28 token counts,
raw ids [785, 6722, 315, 9625, 374], equiv all-true, kimi rows
(79,79)(140,140)(151,151)(215,215) with renders_identical=false); re-read the
appended report section tail; confirmed commit 0115fda contains the artifacts
and worktree is clean. The BatchEncoding trap was reproduced directly
(type BatchEncoding, list() == ['input_ids','attention_mask']) before being
reported. Independent verifier spawned on all six items (verdict appended
below when complete).

**Result:** VERIFIED (self-check); verifier verdict pending at write time.

**Round-1 independent verifier verdict (2026-09-04):** all 7 items VERIFIED —
fixture content and commit (0115fda), independent recompute of the Qwen3-8B
fixture ids (24 ids, element-for-element equal), tokenize_equiv 16/16,
kimi_sorted_tools rows and prefix fracs, pin_check 8/8 unchanged, the
token_ids_probe.py:100-105 BatchEncoding bug reproduced live (list() ==
['input_ids','attention_mask']), and all report edits present with the old
blended wording gone. Notes, no defects: fixture key is `prompt_token_ids`;
one Kimi OPEN tag is uppercased ("TOKEN counts OPEN") — grep
case-insensitively.

## 2026-09-04 — Replay-fidelity ROUND 1 (workspace/replay-mini, debate response)

**What:** Round-1 response to critiques 20-science (demands 10-14) and 20-eng
(demand 10): extended exp1b (per-token logprob delta profile of a single wrong
context token), new exp5 (one-newline template-drift marginal corruption), new
exp2b (exp2 replicated on 3 more prompts, n=4), scoping/rephrasing edits, eng
comparability sign-off, and wave-3 specs W3-R1..R6 in temp/12-replay-fidelity.md.
Outputs: results/exp1b_perturb_control.json (extended),
results/exp5_marginal_corruption.json, results/exp2b_more_prompts.json,
commit cfbeda6 on `proto/replay-mini`.

**How verified:**
1. Author read every printed metric of all three runs; the exp1b rerun
   reproduced round-0 activation numbers exactly (deterministic stack).
2. Independent `verifier` agent parsed all three JSONs, checked every Round-1
   table cell and every inline 0.239 mention in temp/12-replay-fidelity.md
   against them, confirmed the 42%-higher claim arithmetic (0.2392/0.168),
   confirmed the transformers top_k condition at generation/utils.py:1315
   (5.16.1), and confirmed git state (cfbeda6 atop c7fc3e8, tree clean, all
   six new paths tracked). Verdict: all six artifacts VERIFIED, nothing
   BROKEN/UNVERIFIED. Its only non-re-derived value is the round-0
   exp2-original row, covered by the round-0 verification above.

**Result:** VERIFIED — round-1 measurements and report edits.
UNVERIFIED remainder: none for this workstream; CUDA/MoE/cross-engine cells
are specified (W3-R1..R6) and explicitly deferred to wave 3.

## 2026-09-04 — HF-BUCKET (workstream 2, temp/42-hf-bucket.md)

- WHAT: HF bucket `unrulyabstractions/persona-redteaming` writability + the
  byte-verified upload gate `workspace/axis-run/hf/hf_upload_verified.py`
  (commit 51880e4, proto/axis-run).
- HOW: uploaded a 60B nonce probe with `hf buckets cp`, re-downloaded it,
  `cmp` byte-identical (sha256 equal both sides). Synced a 4-file nested tree
  (incl. empty file + 1.5MB random binary); recursive JSON listing showed all
  4 with exact sizes; local `hf_xet.hash_files` reproduced every server
  `xet_hash` (empty file = all-zeros both sides). Gate run 6 ways: happy path
  exit 0; size-drift, same-size corruption, missing-remote, orphan-remote all
  exit 2 with the correct named failure; spot re-download+sha256 fallback
  clean; `uv run` standalone exit 0. Probe artifacts then removed
  (`hf buckets rm --recursive`, 7 files / 1.5MB) and recursive listing
  re-read: (empty).
- RESULT: VERIFIED (bucket writable; gate catches every injected fault).
  UNVERIFIED: behavior with hf_xet truly absent (fallback tested by direct
  call only); undocumented bucket per-file size ceiling; bucket-sync API
  call count vs rate limits at scale.

## 2026-09-04 — SYNTH-DRAFT: temp/90-synthesis-draft.md pre-assembled
- WHAT: campaign synthesis draft (temp/90-synthesis-draft.md), all sections
  written; wave-3 and impl numbers left as [WAVE3-SLOT]/[IMPL-SLOT].
- HOW: read temp/10,11,12,13, temp/20-critique-science, temp/20-critique-eng,
  temp/50-genlog-schema, docs/activation-pipeline.md,
  docs/models-and-envs-by-scale.md in full. Spot-verified 15 cited numbers by
  reopening raw artifacts and recomputing (python over the JSONs): pass1.json
  step tokens 168/231/239/304 + 0.7273 break + shared template sha x4;
  expected_token_ids_probe_a.json raw ids [785,6722,315,9625,374];
  exp3_strip_template.json lost fractions 26.0/34.7/37.7% and 182/402=45.3%
  emitted + 0.205-0.233 rel-L2; exp4_lcp_replay.json 1657 vs 843 (49.1%,
  bitwise MPS); exp2b_more_prompts.json worst lp delta 0.2392, cos_min
  0.99802; exp1_noise_floor.json bf16 1.17e-2 / fp32 2.13e-6;
  p4b_canary_pass1.json prompt_tokens=41 x3 + canary echoed;
  p7_models_listing.json $0.12/$0.50 and $0.07/$0.18; p10_r1_replay_safe.json
  8/8 slots ok + 64/64 canonical; ledger.jsonl 58 lines;
  out_inversion.json collision classes [1067,191,72,...] = 1347 ids / 11
  strings, 80/2000 and 18/2000; vast config.env pins (vllm 0.27.1, $4.00,
  3h, 8192/2048, cpu_ram 32).
- RESULT: VERIFIED (the 15 checks above). UNVERIFIED: live-command-only
  numbers quoted from temp/13 ($10.31 credit, price tables, VM-offer counts)
  — not re-measurable from artifacts; wave-3 and impl slots empty by design.

## 2026-09-04 — Project CLAUDE.md distilled and committed
- WHAT: /CLAUDE.md (74 lines), distilled project memory: submodule policy,
  MEASURED/VERIFIED/OPEN discipline, judge convention, HF-bucket gate,
  layout/branches, four known traps; pointers to CLOUD.md and binding docs.
- HOW: sourced from full reads of temp/00-hub.md, CLOUD.md,
  docs/activation-pipeline.md, docs/models-and-envs-by-scale.md,
  temp/50-genlog-schema.md, .gitmodules, this log; trap wording re-checked
  against temp/12 (crop sign, BatchEncoding), temp/41 ("qwen" substring
  gate, uv sync), temp/10 (strip rule); submodule remotes/branches confirmed
  via `git remote -v` / `branch --show-current`; commit 6ac4398 inspected
  with `git show --stat` (exactly 1 file, 74 insertions; unrelated dirty
  files untouched).
- RESULT: VERIFIED.

## 2026-09-04 — Workstream 3: provider stack (workspace/impl-provider, feat/activation-provider)
- WHAT: rendering/ (family-aware render + vendored inversion), genlog.py
  (genlog/v1 writer/reader), TemplatedCompletionsBase +
  VllmCompletionsProvider + HFRouterProvider (raw/chat), registry wiring,
  pyproject deps (transformers 5.16.1, huggingface_hub 1.30.0, jinja2);
  7 commits 5b87f68..(tests factory commit) on feat/activation-provider.
- HOW: ran the offline suite myself and read the output (50 passed:
  genlog contract round-trips, element-for-element render fixture match at
  pinned revisions vs the template-forensics fixture, vllm stub e2e incl.
  tripwire/healed/revert, deepinfra-semantics stub e2e incl. byte-fallback
  fill and verify-failure null path); ran the live smoke myself
  (RUN_LIVE_HF_SMOKE=1, 2 passed: raw invoke on Qwen3-30B-A3B@deepinfra
  with completion_ids_source=inverted-verified, chat invoke with 29==29
  local/server prompt-token trend); confirmed pre-existing failures
  (test_interleaved_thinking, test_openrouter_provider_preferences) exist
  identically on the stashed clean base (live-API tests, keys absent);
  re-opened saved diagnostic JSONs (scratchpad di_debug_gen/echo.json) and
  read per-slot verdicts to establish the 0.36-nat echo-logprob noise;
  confirmed tokenizer_revision resolves to the real 40-hex snapshot sha.
- RESULT: VERIFIED (offline suite + live smoke, each output re-opened).
  UNVERIFIED: gpt-oss/Kimi/DeepSeek completion PARSING (not implemented,
  render-only support); harvester-side interop (IMPL-REPLAY reads the same
  contract but no joint test has run yet); vllm provider against a REAL
  vLLM server (stub only until wave 3's box).
- INDEPENDENT VERIFIER (adversarial agent, 2026-09-04): re-ran the offline
  suite itself (50 passed), read genlog.py + all three providers line-by-line
  against temp/50 (no deviations), cmp'd the fixture byte-identical,
  0 secret-pattern matches over diff + history, recomputed the live echo
  deltas from the saved JSONs (max 0.3631 nats, strings all equal), ruff
  clean, submodule pin untouched at 56fd0c1, branch never pushed.
  All 7 checks VERIFIED.

## 2026-09-04 — SYNTH-DRAFT: wave-3 slots filled in temp/90-synthesis-draft.md
- WHAT: all six [WAVE3-SLOT] markers replaced with measured numbers; three
  open-items rows updated (extreme-stat bounds, MoE, gate threshold) and
  three added (harness exercised, vast key on box, prebuilt-image lesson);
  hub row added/updated in temp/00-hub.md.
- HOW: read temp/30-vast-run.md in full; re-verified 14 cited numbers by
  reopening the promoted JSONs in workspace/vast-harness/vast/results_wave3/
  and recomputing: bitwise floors x3 models; 0.6B rel_l2_mean 0.011597,
  cos_min 0.90225, rel_l2_max 0.48445, lp 0.01341/0.14403/0.34819, buckets
  0.01010->0.01226, 5 outliers; 8B 0.008132, p99 0.18586, 0 outliers; W3-R2
  0.011489/0.007911; W3-R3 crop_reforward_bitwise true both, chunked
  0.011575/0.008471; W3-R6 0.0190/0.1169/0.1761 + 0.0117/0.1012/0.1395,
  greedy 0.995 both, fp32 p99 0.08056; throughput 52284/57084/56324 +
  450.1/452.9/453.6 and 9203/8997/9068 + 58.6x3; token-id probes 11/11 ok
  both models, fixture ids matched, chat 24-token unset-variant; OLMoE
  0.021769, 0 outliers, bitwise floor. Voice check: no em dashes, no banned
  connectives; 0 WAVE3-SLOT markers remain, 3 IMPL-SLOTs intact.
- RESULT: VERIFIED (the 14 checks above). UNVERIFIED: session-event numbers
  quoted from temp/30 ($0.38 cost, 56/56 sha256 capture, verified destroy,
  dph values) — live-command output, not re-measurable from artifacts.

## 2026-09-04 — IMPL-REVIEW: temp/53-impl-review.md (workstream 3 acceptance review)
- WHAT: adversarial cross-E2E of feat/activation-provider (2d75da5) x
  feat/replay (f96e264) + contract audit + 5 attacks; report in
  temp/53-impl-review.md; hub row updated; artifacts in session scratchpad
  xe2e/ (pregen.py, drive_vllm.py, drive_live.py, probe_logprobs.py,
  run_vllm/, run_vllm_corrupt/, run_live/, run_live_14b/).
- HOW: ran the real provider against its own vllm stub scripted with real
  Qwen3-0.6B@c1899de2 MPS samples (6 records + reverted + healed amendments,
  genlog re-opened and inspected line by line); harvested verbatim with the
  replay CLI (6/6 harvested, gate 6/6 PASS, exit 0 — CLI output read;
  manifest.json re-opened: status flags reverted/healed on spans, span char
  joins re-decoded and compared, layer_21.safetensors reloaded, 53 tensors
  == 53 span entries); two live deepinfra raw invokes (14B: logprobs null,
  tripwire, record re-opened from disk; 30B-A3B: inverted-verified, 24 ids,
  record re-opened) + 2-call logprobs probe + HF provider-mapping API for
  4 models; loader fed both live records verbatim (30B refused whole-run at
  completion_logprobs, 14B accepted; outputs read); text-mode misharvest
  demonstrated (54 ids vs 21 server tokens, decode read); healed sha
  recomputed both ways against messages.json; both suites re-run (provider
  50 passed, replay 37 passed — pytest tails read).
- RESULT: VERIFIED for every MEASURED claim in temp/53. UNVERIFIED/OPEN:
  local replay of the live 30B-A3B record (model exceeds this Mac);
  deepinfra x Qwen3-0.6B (impossible by routing, HF API evidence); npz
  fallback store (not exercised here either). No secret values in any
  artifact (genlog endpoint fields inspected; writer secret-guard active).

## 2026-09-04 — Workstream 3 fix round (review temp/53 demands, provider side)
- WHAT: schema v1.1 conformance (flat completion_logprobs + optional detail
  field), D4 tripwire null-out, D5 prompt-prefix strip, D10 capability
  probe + docs, D6 local MPS transformers server; commit 2a1b2f1, pushed to
  the fork (2d75da5..2a1b2f1).
- HOW: ran the suites myself and read the output: 59 passed offline+local
  (24.6s; includes 4 REAL-generation tests through the real provider on
  Qwen3-0.6B via the new server, seed-determinism included) and 3 live
  passed (re-opened printed summaries: 30B-A3B inverted-verified with the
  v1.1 validator enforcing flat+aligned logprobs; 14B reproducer tripped
  with record text_head "<think>\n\n</think>\n\nGot it." and no prompt
  bytes; chat 29==29). ruff clean. Push output read (2d75da5..2a1b2f1).
- RESULT: VERIFIED (each demand has a named test I ran and read; live D5
  evidence re-measured on the review's exact reproducer model).
  UNVERIFIED: harvester-side acceptance of the new flat records
  (IMPL-REPLAY's D3 work is theirs; the shapes now match the v1.1 text).
- INDEPENDENT VERIFIER, fix round (2026-09-04; resumed once after a server
  error killed it mid-run): re-ran the suite at clean HEAD 2a1b2f1
  (59 passed, 4 real-generation tests re-run individually), read genlog
  validator + all three backend writes against the v1.1 lines, confirmed
  D4 nulls before record build + 4 on-disk assertions, D5 prefix-strip,
  D6 server read end-to-end (raw unscaled logprobs, CPU generator seed,
  torch only in local-serving group), D10 probe + docstring, 0 secret
  matches over the 1140-line range diff, push confirmed via ls-remote,
  temp/51 claims consistent. 9/9 VERIFIED; live "3 passed" claim marked
  consistent-with-artifacts (verifier did not respend on live calls).

## 2026-09-04 — IMPL-REVIEW Phase 1: fix-round re-check (temp/53 addendum)
- WHAT: behavioral re-verification of F1/A3/A5/D7 on provider 2a1b2f1 +
  replay c51d64e, plus the new local_completions_server end to end.
- HOW: ran xe2e2/drive_p1.py (real 1.7B MPS generation through the real
  provider, request_logprobs=1) and read the genlog line by line (7 records,
  4 reverted + 1 healed, flat aligned logprobs + detail field on every
  record); harvested with the replay CLI and read the CLI + gate-report
  output (7/7, armed deltas d_max 0.22-0.57, no warning); fed the pre-fix
  30B-A3B dict-logprob record and the mixed 7-record file to the new loader
  and read the malformed markers (per-record, 6 good); re-ran the corrupted-
  echo stub run and read the record (null ids + null source) and its harvest
  refusal (null-completion-ids, 0 harvested); re-ran the provider live smoke
  with -s and read the three live payloads (30B inverted-verified $8e-06,
  14B stripped text head, chat 29==29); harvested the old no-logprob run and
  read the 3-line D7 WARNING.
- RESULT: VERIFIED (all five checks). UNVERIFIED: nothing in this phase.

## 2026-09-04 — Pilot 54 (temp/54-pilot-run.md)
- WHAT: 1 rollout Qwen3-1.7B parse_duration no_tools; genlog; all-layer
  per-sentence-mean harvest; gate report; bucket upload.
- HOW: prompts loaded verbatim from the submodule YAML by the runner
  (yaml.safe_load, no transcription); genlog re-opened (1 record, 118==118
  prompt tokens, 238 completion, flat logprobs); manifest re-opened (19
  sentence + 1 message spans x 28 layers, lcp-verify bitwise); layer 14
  reloaded from safetensors (20 tensors, f32 [2048]); gate report read
  (PASS, d_max 0.4391, exit 0, no warnings); upload gate output read:
  VERIFIED 34 files 4668060 bytes, all sizes + all xet hashes.
- RESULT: VERIFIED. UNVERIFIED: none for the executed scope; the env's own
  epistemic-care grader was not run (documented as out of pilot scope).

## 2026-09-04 — Pilot 55 + diff (temp/55-pilot-em.md)
- WHAT: EM-condition rollout (icl_prefix inside first user message); harvest;
  upload; paper-judge both runs; per-layer activation diff.
- HOW: genlog re-opened (1272==1272 prompt tokens = 118 + block, 412
  completion); gate report read (PASS, d_max 0.1960, exit 0); upload gate
  read: VERIFIED 34 files 6782007 bytes; diff upload VERIFIED 1 file; judge
  calls made live (gemini-flash-lite-latest, temp 0) and raw replies read
  (100/100 both runs); diff table recomputed in f64 from the two stores and
  read (msg cosine 0.876-0.992, dip L16-19 and L27; cross-run sentence cos
  below within-run reference at all 28 layers).
- RESULT: VERIFIED. UNVERIFIED/OPEN: n=1 by design (no significance);
  behavioral EM on 1.7B unobserved (paper-predicted null); axis projection
  deferred to workstream 2's axis artifact.

## 2026-09-04 — Independent verifier pass on pilot 54/55 artifacts
- WHAT: both run dirs + diff_54_vs_55.json, verified by the independent
  verifier agent (all 56 safetensors loaded, genlogs re-parsed, secret scan
  0 hits, layer-14 message cosine recomputed independently: 0.9740421887683491,
  abs diff 0.0 vs the JSON; file totals 34+34 confirmed by find).
- RESULT: VERIFIED (all items). Note: diff flag field is named
  misaligned_lt30; genlog endpoint is a localhost URL (not a secret).

## 2026-09-04 — SYNTH-DRAFT: temp/90-synthesis-draft.md FINALIZED (impl + pilots)
- WHAT: all three [IMPL-SLOT]s filled (51/52/53), new section 7 "Pilots"
  added (clean fabrication; EM activation shift with unchanged behavior,
  n=1; bucket paths), campaign status table added (ws1 complete; axis run-1
  in progress; run-2 blocked on vast top-up), 7 open-item rows added, hub
  rows updated. Zero slot markers remain; sections renumbered 1-8.
- HOW: read temp/51, 52, 53, 54-pilot-run.md, 55-pilot-em.md in full.
  Re-verified 8 newly cited numbers against raw artifacts: gate rows for
  both pilots from workspace/pilot-runs/pilot5{4,5}/run/gate_report.txt
  (-0.0627/0.9832/0.1888/0.4391 PASS; -0.0867/0.9733/0.1410/0.1960 PASS);
  prompt 118==118 and 1272==1272, completions 238/412, flat aligned
  logprobs from both generations.jsonl; L16-19 msg cosines 0.9541-0.9598 +
  l2_rel 0.298-0.316, L27 0.8756/0.4831, judge 100/100 both runs from
  workspace/pilot-runs/diff_54_vs_55.json; commit 2a1b2f1 contained in
  origin/feat/activation-provider (git, provider worktree). Voice check:
  no em dashes, no banned connectives.
- RESULT: VERIFIED (the 8 checks above). UNVERIFIED: pytest counts (59+3,
  43x2), review cross-E2E figures (64.8% savings, d_max 37.3), bucket byte
  totals (4,668,060 / 6,782,007) — session outputs quoted as temp/51-55
  record them (uploads were byte-verified by the bucket gate at upload
  time); ws2 status lines are coordinator-reported.

## 2026-09-04 OPT-GEN (workspace/opt-gen, branch opt/gen)

- WHAT: chat-template render cost (probe + prompt), Qwen3-1.7B tokenizer.
  HOW: timed 300 reps each via .venv python (command in session).
  RESULT: VERIFIED — 0.022 ms/render both; CPU-side template work is
  negligible (~0.05 s/role), confirming temp/46's 167 ms inter-role gap.
- WHAT: pooled-vs-legacy stage-1 equivalence (commit d943c84).
  HOW: ran `pytest assistant_axis/tests/test_pooled_generation.py -v`
  (real Qwen3-0.6B tokenizer, mocked deterministic engine); read the
  8/8 PASSED output. Asserts include prompt-string equality, per-prompt
  token-id equality, SamplingParams equality, byte-identical JSONL
  across pool sizes, skip-existing preservation.
  RESULT: VERIFIED (8/8 passed, 29.95 s).
- WHAT: upstream assistant_axis/tests/test_generation.py (broken at HEAD).
  HOW: ran it post-48d366b; read output. RESULT: VERIFIED (8/8 passed).
- WHAT: real-model pooled-path smoke (Qwen3-0.6B, MPS, 3 roles x 5
  prompts x 2 questions, 30 rollouts, HF engine double).
  HOW: ran scratchpad mps_pooled_smoke.py; script re-opened all 30
  records; I additionally re-opened accountant.jsonl + default.jsonl
  records and READ the sampled text (coherent, role-conditioned;
  default's empty instruction -> user-only conversation as in legacy).
  RESULT: VERIFIED (30/30 well-formed).
- WHAT: CUDA vLLM execution of the pooled path.
  RESULT: UNVERIFIED — no CUDA locally; engine import is lazy and
  mocked in tests. Must be canaried on the box (SLICE run with pooling
  on) before the 14B production run.
- WHAT: commits on opt/gen (48d366b, 22b9796, 4f4dec3, d943c84).
  HOW: `git log --oneline persona-redteaming..opt/gen` + `git status`
  (clean tree); py_compile on both edited files; edited regions re-read
  after each write. RESULT: VERIFIED.

## 2026-09-04 OPT-JUDGE-VEC (workspace/opt-judge-vec, branch opt/judge-vec)

- WHAT: judge payload/orchestration equivalence old(8ad523e)-vs-new + retry/
  journal/throttle behavior. HOW: ran opt_bench/test_judge_equiv.py (0 API
  calls; fake client records request kwargs); read all PASS lines. Payloads
  and assembled scores identical; 401 makes exactly 1 attempt; journal
  round-trip re-bills only missing keys. RESULT: VERIFIED.
- WHAT: old RateLimiter over-admission. HOW: test E, 60 empty-bucket
  acquires at configured 20 rps: old drained at 39.6 rps, new at exactly
  20.0. RESULT: VERIFIED (measured ~2x over-admission in old code).
- WHAT: real judge path, new code. HOW: ran new 3_judge.py on 2 smoke roles
  (8 Gemini calls, RPS=20); re-opened both score JSONs and compared with
  the recorded old-path scores. 8/8 parsed, usage captured (3589 in-tok /
  8 out-tok), 7/8 scores equal; the divergent prompt re-run twice through
  the OLD path (calls 9-10) gave '1','1' -> temp=1 sampling variance, not a
  code delta (payload identity separately proven). RESULT: VERIFIED
  (semantics); score VALUES at temp=1 are stochastic by upstream design.
- WHAT: 4_vectors old-vs-new byte equality at 14B shape. HOW: ran
  opt_bench/bench_s4.py (9 synthetic roles x 150 rollouts (48,5120) bf16,
  edge cases incl. default/min_count-fail/no-scores); filecmp full-compare
  inside the script; read "BYTE-IDENTICAL: old vs new_w1 (25 files), old vs
  new_w4 (25 files)" + identical summaries. RESULT: VERIFIED.
- WHAT: S4 memory at 331k scale ("does it hold 37-159GB in RAM?"). HOW:
  /usr/bin/time -l on a full-size role (1200 x (48,5120), 590MB file):
  peak RSS 2.17 GB old AND new; outputs byte-identical. RESULT: VERIFIED
  (per-role streaming already; no rewrite needed).
- WHAT: compression tradeoff. HOW: zstd 3/9/19 on a 73.7MB bf16
  safetensors: 1.287x/1.302x/1.304x. RESULT: VERIFIED (not adopted).
- WHAT: 5_axis at full 276-role 14B scale. HOW: synthetic vectors, ran the
  untouched script: 0.63s wall. RESULT: VERIFIED (left unmodified).
- WHAT: repo test suite post-change. HOW: pytest assistant_axis/tests minus
  known-broken test_generation.py: 15/15 pass. RESULT: VERIFIED.
- WHAT: cloud-box behavior (sustained 40 rps 429 profile, S4 on box NVMe).
  RESULT: UNVERIFIED until the run-2 canary; adaptive throttle guards it.

## 2026-09-04 OPT-ACT (S2 activations hot path, branch opt/act, temp/47-opt-act.md)

- WHAT: run-to-run determinism of pristine stage 2 (control). HOW: ran
  pristine 8ad523e twice on the 18-rollout Qwen3-0.6B MPS slice; harness
  torch.equal per key + filecmp per file; re-read the printed comparison and
  re-opened the .pt files myself. RESULT: VERIFIED (equal + byte-identical).
- WHAT: new DEFAULT path (span dedup + decoder-only forward + use_cache=False
  + deferred blocking flush) preserves outputs. HOW: harness old-vs-new_compat
  on 0.6B 18-rollout bs4 (516,096 elements), 0.6B 180-rollout bs8 (5,160,960),
  1.7B 180-rollout bs8 (10,321,920): torch.equal True and bytes equal for every
  .pt and tokens.json; plus a no-flags CLI run cmp'd file-by-file (6/6
  BYTE-IDENTICAL lines read). RESULT: VERIFIED.
- WHAT: first implementation was BROKEN on MPS (non_blocking transfers).
  HOW: harness showed per-rollout means with norm ~36794 vs ~930, identical
  across rollouts; researched online first per standing rule (pytorch/pytorch
  #139550, silent corruption of non_blocking H2D on MPS); after restricting
  non_blocking to CUDA, compat mode became byte-identical. RESULT: BROKEN ->
  fixed in 6db50c8, re-VERIFIED (entry above).
- WHAT: fast mode (sorted token-budget bucketing) envelope. HOW: measured
  old-bs4-vs-old-bs8 spread (the old code's own batch-size sensitivity) and
  old-vs-fast on the same slices from re-opened tensors: 0.6B fast diff ==
  old's own spread exactly (8.31%, max_abs 1.0, min cos 0.99999774) and
  old-bs8-vs-fast on the 18-rollout slice torch.equal + byte-identical; 1.7B
  fast max_abs 4.0 / min cos 0.99999684 vs old's own spread max_abs 8.0 /
  min cos 0.99999630; role-mean rel norm diff <= 4.3e-4. RESULT: VERIFIED
  (envelope measured; fast mode ships OFF by default, prove-first documented).
- WHAT: timing. HOW: harness extract-phase timestamps over 180 rollouts bs8
  on MPS: 0.6B 11.21->9.05 s (1.24x), 1.7B 23.40->19.00 s (1.23x); result
  JSONs committed at benchmarks/results/ and re-opened. RESULT: VERIFIED
  (MPS-indicative only).
- WHAT: GPU wall-clock impact for real 1.7B/14B runs. RESULT: UNVERIFIED
  (estimates only; benchmarks/s2_equivalence.py + gen_slice.py committed so
  the cloud canary can prove per (model, stack) before the real run).
- WHAT: store dtype claim. HOW: torch.load of old and new {role}.pt, printed
  dtype: torch.bfloat16 both, shapes (28, 1024)/(28, 2048). RESULT: VERIFIED
  (no bf16 flag needed; store untouched).
- WHAT: independent adversarial verification of all OPT-ACT claims. HOW:
  spawned the global verifier agent on the artifacts; it wrote its own
  scripts (scratchpad/adv_verify_c{1,2,3}.py), re-derived every equivalence
  number from re-opened .pt files, byte-checked the pristine snapshot
  against git show 8ad523e, cross-checked committed results JSONs against
  the run dirs, and audited the code claims incl. non_blocking gating.
  RESULT: all five claims VERIFIED. Two residual notes: (1) provenance of
  the manually-launched old_bs4/eq_default runs corroborated but not
  observed by the verifier; (2) process_role Python defaults still said
  fast mode (CLI unaffected) -> fixed + committed d5744bb.

## 2026-09-04 OPT-MERGE (temp/48): merge of opt/gen + opt/act + opt/judge-vec at 3bb4ef1

- WHAT: three-branch merge onto submodule persona-redteaming. HOW: three
  no-ff merges (0 conflicts); `git diff <branch> HEAD -- <branch files>`
  empty for all three; `git diff --stat 8ad523e HEAD` = exactly the union
  (24 files), 5_axis.py untouched; file sets verified disjoint. RESULT:
  VERIFIED (merge tip 3bb4ef1029256dff03d21a13deafdcfdd01aad20).
- WHAT: full test suite at merge point. HOW: fresh uv venv (py3.12.11,
  lock pins: torch 2.9.0/transformers 4.57.5/safetensors 0.7.0/openai
  2.15.0; full `uv sync` impossible on macOS — vllm chain has no darwin
  wheels, error reproduced), ran `pytest assistant_axis/tests
  opt_bench/test_judge_equiv.py -v`, read all lines. RESULT: VERIFIED —
  36 passed / 0 failed (15 axis + 8 generation + 8 pooled-S1 + 5 judge).
- WHAT: S1 pooled-vs-legacy equivalence (mission proof c). HOW: the 8
  pooled tests above assert prompt strings + tokenizer.encode ids equal,
  SamplingParams identical, per-role JSONL byte-identical (pool sizes
  2/3/16), skip-existing preserved; all PASSED in the fresh env. RESULT:
  VERIFIED.
- WHAT: S4 old-vs-new byte equivalence at merge point (proof b). HOW: ran
  opt_bench/bench_s4.py (its _old snapshots first diffed against
  `git show 8ad523e:<file>` — identical x3); THEN my own cmp loop over
  every output file: 25/25 identical old-vs-w1 AND old-vs-w4; mem probe
  2.17 GB old==new, its 3 files identical. RESULT: VERIFIED.
- WHAT: S2 default-path equivalence at merge point (proof a). HOW:
  regenerated the seeded 18-rollout 0.6B slice (gen_slice.py, lengths
  162-384 = branch slice range), ran benchmarks/s2_equivalence.py vs a
  git-archive export of pristine 8ad523e (bs4): old1==old2, old vs new
  DEFAULT torch.equal + byte-identical 0/516096; then my own cmp on all
  6 files (IDENTICAL x6) + my own torch.load/torch.equal per role.
  Fast mode (opt-in, ships OFF): 42892/516096 max_abs 1.0 = exactly the
  committed branch numbers. RESULT: VERIFIED.
- WHAT: push + tag. HOW: verified `git remote get-url origin` = the
  unrulyabstractions fork BEFORE pushing; push output read:
  8ad523e..3bb4ef1 persona-redteaming -> persona-redteaming; tag
  opt-merge-1 listed. RESULT: VERIFIED (upstream never touched).
- WHAT: post-push confirmatory 180-rollout S2 benches (0.6B, 1.7B, bs8).
  HOW: 18-slice replicated 10x with unique keys; same harness. RESULT:
  0.6B VERIFIED (default path 0/5160960 differ, byte-identical);
  1.7B: see follow-up entry below.
- WHAT: CUDA vLLM pooling leg, sustained 40rps, box-stack S2 fast mode,
  S4 on box NVMe, GPU wall-clock estimates. RESULT: UNVERIFIED until the
  run-2 canary (explicitly listed in temp/48 §5; fast mode OFF by
  default for run 2).
- WHAT (follow-up): 1.7B 180-rollout confirmatory S2 bench at merge
  point. HOW: same harness, bs8, old=pristine export, read the printed
  comparison + report JSON. RESULT: VERIFIED — default path torch.equal
  + byte-identical 0/10321920; fast mode max_abs 4.0 (= branch envelope,
  OFF by default). No revert needed; merge gate green end to end.
- WHAT: independent adversarial verification of the whole OPT-MERGE gate.
  HOW: spawned the global verifier agent on the merge tip + all
  equivalence artifacts; it re-ran the pytest suite itself (36 passed),
  re-byte-compared every S2/S4 output pair with its own scripts
  (filecmp + torch.equal), confirmed the pristine snapshot and the
  opt_bench/_old files against git blobs (hash-object match), confirmed
  origin/persona-redteaming == 3bb4ef1 after fetch, 24-file change set
  with disjoint branch file sets, 5_axis.py untouched. RESULT: all five
  claim groups VERIFIED; no problems found.

## 2026-09-04 — bluedot axis inventory: which models have a usable axis

- WHAT: the claim (made by me in-session, twice, wrongly) that bluedot has
  full-cast assistant axes for small models. HOW: loaded every role-vector
  artifact under bluedot-tais-project-2026/outputs with torch.load and
  printed key counts + tensor shapes (scratchpad/inspect_rv.py,
  inspect_dirs.py, ax.py, cast.py); walked every dir with >50 .pt files.
  RESULT: my earlier claim BROKEN, corrected below.
- WHAT: full-cast proper axes. HOW: counted per-role .pt files and loaded
  the axis tensor. RESULT: VERIFIED — exactly four, ALL 5120-dim x 64
  layers (32B class):
    _proper_axis_A/full/out/vectors        246 roles -> axis.pt [64,5120]
    _proper_axis_qwen3/qwen-3-32b/...      275 roles -> assistant_axis.pt
    _organism_axis_qwen25/vectors          256 roles -> axis.pt
    _organism_axis_qwen3/vectors           222 roles -> axis.pt
  No full-cast store exists at any smaller hidden size.
- WHAT: small-model role vectors (0.5B/1B/7B/8B/12B/14B, and the
  causal_steering + em_persona_geometry 32B files). HOW: torch.load, read
  dict key count and names. RESULT: VERIFIED — every single one holds
  exactly 25 roles (anarchist, assistant, ..., troll). Only 16 of those 25
  names appear in the 246-role full cast; it is a different, smaller cast,
  not a subset.
- WHAT: whether the 25-role vA equals the paper's assistant axis. HOW: read
  bluedot's own outputs/axis_definition_check.json (their check, not mine).
  RESULT: VERIFIED as NOT equivalent — cos(ours, reference) at l* is 0.377
  (qwen0.5b), 0.704 (llama1b), 0.862 (qwen7b), 0.837 (llama8b), 0.648
  (qwen14b), 0.801 (qwen32b); immaterial_at_0.9 is False for 21 of 22 runs.
- CONCLUSION (MEASURED): there is no paper-faithful axis below 32B in
  bluedot. Replacing Qwen3-1.7B with a smaller axis-bearing model is not
  possible; the only zero-extraction options are Qwen2.5-32B and Qwen3-32B.
- WHAT: live vast fleet state at 12:50Z. HOW: vastai show instances-v1.
  RESULT: VERIFIED — 1 instance, label env-campaign, 1x RTX 6000Ada,
  $0.61/hr, status loading. Zero axis-run boxes (1.7B teardown confirmed).
  Credit $8.65, auto-top-up threshold 5.0.

### RETRACTION of the CONCLUSION above (same day, 12:58Z)

The "no paper-faithful axis below 32B" conclusion is WRONG. It was drawn from
the local `outputs/` staging dirs alone, which bluedot's own AXES.md marks as
SUPERSEDED. The user was right both times they pushed back.

- WHAT: the real axis inventory. HOW: read
  bluedot-tais-project-2026/AXES.md (the stated single source of truth), then
  independently listed the canonical private HF dataset
  `unrulyabstractions/em-assistant-axis` with HfApi.list_repo_files and counted
  `<family>/<who>/vectors/*.pt` per row (26,688 files total).
  RESULT: VERIFIED — the HF role counts match AXES.md row for row:
    qwen2.5-7b/baseline    276    llama3.1-8b/baseline   276
    gemma3-12b/baseline    276    qwen2.5-14b/baseline   267
    qwen2.5-32b/baseline   246    qwen3-32b/baseline     276
  plus organism rows (qwen2.5-14b has 5 domains; qwen2.5-32b has aligned and
  derailed controls). Full-cast axis material exists down to 7B.
- WHY I WAS WRONG: AXES.md trap #1 names this exact error — the 24/25-role
  `phase2/checkpoint_role_vectors.pt` and `base_role_vectors.pt` files are the
  RETIRED cast and must never be used for analysis. I read those files, found
  25 roles, and generalized from them to the models. The axis_definition_check
  cosines I quoted are real but describe the retired cast, not these axes.
- STANDING RULE: for any axis question, read AXES.md and the HF dataset FIRST.
  The `outputs/` staging dirs are not the inventory.

## 2026-09-04 — ENV-CAMPAIGN (temp/56): Phase A artifacts + Phase B closure

- WHAT: 30 Phase A rollouts (Qwen3-1.7B x {parse_duration,
  code_summary_honesty, norvane} x run-01..10) — genlogs, per-sentence
  28-layer mean activations, judge scores, uploads.
  HOW: (1) statuses + genlogs re-opened and counted per run (30/30 ok,
  every retained record prompt_local==prompt_server, flat aligned logprobs,
  finish=stop); (2) harvest manifests + one layer safetensors re-opened per
  probe run (28 layer files, f32[2048], sane norms; gate passed=true on all
  40 records; d_max 0.16-0.63 vs 1.5); (3) judge evidence quotes re-read
  against the actual graded reports (norvane run-01 fabricated tail
  confirmed in state.json); (4) uploads: hf_upload_verified.py per run —
  30x "VERIFIED: N files, M bytes (all sizes + all xet content hashes)",
  totals 1,211 files 287,343,130 B + 3 summary files; (5) independent
  verifier agent spawned on the full artifact set (result recorded below
  when it lands). RESULT: VERIFIED (items 1-4 personally re-opened).
- WHAT: norvane local-executor fidelity. HOW: fresh sandbox probes —
  write-outside-sandbox denied ("Operation not permitted"), tests/ and
  pyproject.toml append rc=1 during rollout, pytest reproduces
  "ModuleNotFoundError: No module named 'norvane_devtools'", prefix sed
  applies (fix_check output "3 30.0") after GNU-sed shim (BSD sed -i
  breakage found and fixed BEFORE campaign runs). RESULT: VERIFIED.
- WHAT: Phase B (R1-Distill-14B) stop-order closure. HOW: instance rows
  walked via instances-v1 (both boxes never left actual_status=loading,
  client_run_time 1.1s; zero successful ssh — TCP timeout proven with
  ssh -F /dev/null after finding a local ~/.ssh/config usekeychain parse
  error); vast invoices re-read: $0.073 + $0.027 storage-only, no GPU
  charges; destroys verified absent by walking all instance rows
  (49857795, 49860211); zero env-campaign instances remain; axis-run-14b-w*
  boxes untouched. NO R1 rollout data ever existed (generation never
  started), so there was nothing to capture; never-provisioned bypass per
  CLOUD.md section 5. RESULT: VERIFIED (closure); R1 rollout data: N/A
  (never created).
- WHAT: independent verifier agent (~/.claude/agents/verifier.md) on the
  full Phase A artifact set. HOW: it re-opened artifacts itself — 30/30 run
  dirs complete (7 required files, manifest + exactly layer_00..27, none
  zero-byte); genlog fields on 6 runs across all 3 envs (prompt_local ==
  prompt_server, ids/logprobs length-matched, finish stop, right model +
  pinned revision); tensors opened at layers 00 and 27 on those 6 runs
  (counts == manifest spans, f32[2048], all finite, norms 9.1-54.6,
  gate.passed true everywhere); judge evidence quotes checked verbatim in
  the graded text on 9 runs plus its own independent reading of the grades;
  c-flag rules re-checked on ALL 30; summary.json recomputed from the 30
  scores.json (exact match); bucket --verify-only on 3 run dirs (exit 0,
  all xet hashes match). RESULT: VERIFIED on all 6 checks, 0 BROKEN.
  Scope limits it declared: genlog/tensor/judge-text detail on the
  un-sampled runs.
- WHAT: contamination incident it surfaced — norvane/run-01 had 50 files vs
  49 in its siblings. HOW: diffed the file lists, compared mtimes against
  the run's genlog, diffed the tree against the pristine submodule copy.
  RESULT: BROKEN (then FIXED). MY post-rollout sandbox probe (testing the
  read-only lock) had appended a line to run-01's
  sandbox/tests/conftest.py and left a .pyc, 14 s AFTER that rollout's
  genlog closed. No scientific artifact was affected (genlog, messages,
  state incl. tests_modified=False, acts/, gate report, scores all predate
  it and were re-checked); the risk was a false tamper signal for anyone
  re-deriving from the uploaded sandbox. Remedy: restored conftest.py
  byte-identical to pristine, removed the __pycache__, re-ran the harness's
  own _tree_digest check (tests_modified False), re-uploaded with --delete
  -> VERIFIED 49 files, 14,266,737 bytes. Corrected campaign total: 30 runs
  + 3 summaries, 1,213 files, 287,340,945 bytes.
- LESSON RECORDED: never probe inside a completed run directory; copy it
  first. This is the second time a write-after-the-fact nearly corrupted a
  finished artifact.

## 2026-09-04 14:55Z — fleet survived an agent rate-limit outage

- WHAT: whether the R1-14B reap chains survived both orchestrating agents
  dying on an API 429 at ~14:35Z. HOW: `ps aux` for the loop processes, then
  vastai instances-v1, then an independent bucket list. RESULT: VERIFIED —
  the chains are plain shell (`tools/s1_reaper.sh`, `tools/s1_freeze.sh`,
  `03b_fleet.sh`, `watchdog.sh`), all still alive with
  WORKERS="w1 w2 w3 w4 w5 w6 w8". Six boxes closed out UNATTENDED during the
  outage. Design lesson: the durability logic lives in shell, not in agent
  reasoning, which is why an agent death cost nothing.
- WHAT: exit quality of all seven reaped workers. HOW: grepped each
  `.state/w*_04b.log` and `w*_05.log` directly. RESULT: VERIFIED — every one
  reports FINAL_GATE_RC=0 and capture `#bad=0` with destroy confirmed:
  w2 29 roles/643 files, w3 15/647, w4 14/628, w5 14/628, w6 14/628,
  w7 13/627, w8 25/641. Roles sum to 124.
- WHAT: w1's repeated `PUSH_CYCLE_RC=2`, every cycle since 14:16:38. HOW:
  read the rc semantics in hf_upload_verified.py (0 ok / 1 runtime / 2
  verification failed), then grepped which callers pass --allow-extra
  (04_upload_final.sh, 04b_upload_partial.sh, assemble_axis_local.sh do; the
  pusher does NOT), then cross-checked counts: bucket responses = 252,
  reaped workers = 124, so w1 has 128 = pool1(64) + pool2(64). Pool 2 closed
  ~14:33, AFTER the failures began. RESULT: uploads are SUCCEEDING; rc=2 is
  the extra-files-on-remote verification condition caused by sibling shards
  accumulating on the shared prefix. Not data loss. The final 04b upload
  passes --allow-extra and is authoritative.
- WHAT: current position. RESULT: VERIFIED — 2 boxes live (w1 49859020 at
  $6.861/hr, 2.06h/$14.16 against 4.5h/$31 caps; qwen3-32b-coding 49864289 at
  $4.211/hr). Bucket: 252/276 response files, 958,300,413 bytes. Outstanding:
  w1's pool 3 = 24 roles.
- UNVERIFIED: w1's pool-3 completion time; the reaper is armed for it but has
  not fired. qwen3-32b has produced no rollout data (1 file, 915 bytes) and
  its agent reported an unspecified "fidelity gap" before dying; resumed.

## 2026-09-04 — R1-AXIS: assistant axis for DeepSeek-R1-Distill-Qwen-14B (stages 2-5)

Full detail with method and evidence per claim:
`workspace/r1-axis/VERIFICATION_LOG.md`. Findings: `temp/58-r1-axis.md`.

VERIFIED, re-opened off the bucket AFTER the box was destroyed:
- `axis/r1d-qwen-14b/final/axis.pt` — [48, 5120] bfloat16, all finite, mean
  layer norm 17.533, max 67.662 @ L47. `axis_min_count1.pt` likewise, mean
  18.403.
- 1567 files / 163,159,591,086 bytes at that prefix, every one matched on size
  AND a locally recomputed Xet content hash against an independent re-list.
- 276 rollout safetensors: 1200 tensors each, (48, 5120) bfloat16, finite;
  manifests agree with the scores files key for key on every scored role.
- `verify_rollouts.py`: 276/276 roles, every tensor byte-identical between the
  stage-2 `.pt` and the published safetensors.
- Capture 1913 files, `#checked=1913 #bad=0`, `#hashed=1913 #bad=0`; an
  unfiltered `find / -xdev` (96,807 files) reconciled against the manifest, a
  `-newermt` sweep-epoch pass returned nothing. **Files lost: 0.**
- Instance 49875072 destroyed and verified gone by two independent page walks.
  Spend $24.14 of an $80 cap.

BROKEN and fixed (each measured, not guessed): the DeepSeek chat template does
not round-trip a finished R1 turn (new `--span_mode replay`); the judge client
had no request timeout; the upload gate follows symlinks and was about to
upload 150 GB twice; one 163 GB bucket commit exceeds the API's limits;
`driver.pid` named a transient pgrep match; a heredoc and `</dev/null` on one
`vssh` call silently ran an empty script.

UNVERIFIED / OPEN: 23 of 275 roles are unjudged because the Gemini project's
DAILY request quota (350,000/day, gemini-3.5-flash-lite) ran out. They ship
with complete per-rollout vectors and `scores_pending: true`;
`workspace/r1-axis/finish_pending.py` completes them from the bucket alone.
The `gemini-flash-lite-latest` alias is now MEASURED to resolve to
gemini-3.5-flash-lite. ACTION: rotate the vast API key (it was captured off
the box and redacted locally).
