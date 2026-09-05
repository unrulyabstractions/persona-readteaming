#!/usr/bin/env bash
# judge_preflight.sh — ON-BOX preflight: one REAL Gemini chat completion.
#
# Why a real completion: geo-blocked hosts still pass /v1/models-style
# checks (401) but 403 completions with unsupported_country_region_territory
# — the bluedot judge then silently produced garbage for hours. This must
# pass BEFORE any GPU stage runs.
#
# Secrets: GEMINI_API_KEY is read from /root/.axis_env by the python client
# from the ENVIRONMENT — it never appears in argv or output. No xtrace.
case $- in *x*) set +x ;; esac
set -u
. /root/.axis_env
. /root/.axis_env_common 2>/dev/null || true

python3 - <<'PY'
import os, sys
from openai import OpenAI

key = os.environ.get("GEMINI_API_KEY")
if not key:
    print("JUDGE_PREFLIGHT_FAIL: GEMINI_API_KEY missing on box")
    sys.exit(1)
model = os.environ.get("JUDGE_MODEL", "gemini-flash-lite-latest")
client = OpenAI(
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    api_key=key,
    timeout=30,
    max_retries=0,
)
try:
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
        max_tokens=5,
        temperature=0,
    )
except Exception as e:
    msg = str(e)
    if "unsupported_country_region_territory" in msg:
        print("JUDGE_PREFLIGHT_FAIL: GEO-BLOCKED host (unsupported_country_region_territory)")
    else:
        # scrub anything token-shaped before printing
        import re
        print("JUDGE_PREFLIGHT_FAIL:", re.sub(r"[A-Za-z0-9_\-]{30,}", "<redacted>", msg)[:400])
    sys.exit(1)
content = (r.choices[0].message.content or "").strip()
if not content:
    print("JUDGE_PREFLIGHT_FAIL: 200 but empty content")
    sys.exit(1)
print(f"JUDGE_PREFLIGHT_OK model={model} reply={content[:20]!r}")
PY
