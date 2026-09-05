#!/usr/bin/env python3
"""Measure Gemini flash-lite behavior on the OpenAI-compat endpoint.

Sizes the judge semaphore for pipeline/3_judge.py before any GPU is rented.
Sends a handful of tiny chat completions (4 sequential, then a small
concurrent burst) against the hub judge convention endpoint and records:
HTTP status, wall latency, usage tokens, every response header that mentions
rate limits or quota, and the error body of any 429 (Gemini's quota errors
name the violated limit and its value).

SECRETS: reads GEMINI_API_KEY from the environment; fails loudly if missing;
never prints it (only its length). stdlib only, no deps.

Usage: python3 measure_gemini_limits.py [--burst N] [--model M]
"""
import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-flash-lite-latest"  # hub judge convention

INTERESTING_HEADER_WORDS = ("ratelimit", "rate-limit", "quota", "retry-after")


def one_call(key: str, model: str, tag: str) -> dict:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": f"Reply with the single word OK. ({tag})"}],
            "max_tokens": 5,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    t0 = time.monotonic()
    rec = {"tag": tag}
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode())
            rec["status"] = resp.status
            rec["latency_s"] = round(time.monotonic() - t0, 3)
            rec["usage"] = payload.get("usage")
            rec["model_echo"] = payload.get("model")
            rec["headers"] = {
                k: v
                for k, v in resp.headers.items()
                if any(w in k.lower() for w in INTERESTING_HEADER_WORDS)
            }
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        rec["latency_s"] = round(time.monotonic() - t0, 3)
        try:
            rec["error_body"] = e.read().decode()[:2000]
        except Exception:
            rec["error_body"] = "<unreadable>"
        rec["headers"] = {
            k: v
            for k, v in e.headers.items()
            if any(w in k.lower() for w in INTERESTING_HEADER_WORDS)
        }
    except Exception as e:  # network failure, timeout
        rec["status"] = "EXC"
        rec["latency_s"] = round(time.monotonic() - t0, 3)
        rec["error_body"] = repr(e)[:500]
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--burst", type=int, default=10, help="concurrent calls in phase 2")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("FATAL: GEMINI_API_KEY not set (hub convention: key+endpoint travel together)")
        return 1
    print(f"key present (length {len(key)}); model={args.model}; base={BASE}")

    out = {"model": args.model, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    print("--- phase 1: 4 sequential tiny calls ---")
    seq = []
    for i in range(4):
        rec = one_call(key, args.model, f"seq{i}")
        seq.append(rec)
        print(json.dumps(rec))
    out["sequential"] = seq

    print(f"--- phase 2: burst of {args.burst} concurrent calls ---")
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.burst) as ex:
        burst = list(ex.map(lambda i: one_call(key, args.model, f"burst{i}"), range(args.burst)))
    wall = round(time.monotonic() - t0, 3)
    for rec in burst:
        print(json.dumps(rec))
    ok = sum(1 for r in burst if r.get("status") == 200)
    r429 = sum(1 for r in burst if r.get("status") == 429)
    out["burst"] = {"n": args.burst, "wall_s": wall, "ok": ok, "s429": r429, "calls": burst}
    print(f"burst summary: {ok}/{args.burst} ok, {r429} x 429, wall {wall}s")

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_limits_measured.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
