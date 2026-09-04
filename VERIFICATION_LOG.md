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
