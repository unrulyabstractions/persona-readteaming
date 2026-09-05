"""A scripted stand-in for vLLM's /v1/completions, for loop-mechanics tests.

Returns the texts in `--script` (a JSON list) in order, one per request,
echoing the prompt token ids so the provider's tripwire is exercised. After
the script is exhausted it keeps returning the last text. No model, no GPU:
this proves what the HARNESS does with a given completion, never what the
model would say.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(script: list[str], served: str):
    lock = threading.Lock()
    state = {"i": 0, "requests": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/").endswith("/v1/models"):
                return self._json(200, {"data": [{"id": served}]})
            if self.path.endswith("/requests"):
                with lock:
                    return self._json(200, state["requests"])
            return self._json(404, {"error": "no"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n) or b"{}")
            with lock:
                i = min(state["i"], len(script) - 1)
                state["i"] += 1
                state["requests"].append({"n_prompt": len(payload.get("prompt", [])),
                                          "max_tokens": payload.get("max_tokens"),
                                          "temperature": payload.get("temperature")})
            text = script[i]
            ids = list(payload["prompt"])
            comp_ids = list(range(1, len(text) // 3 + 2))
            return self._json(200, {
                "id": "cmpl-fake", "object": "text_completion", "model": served,
                "choices": [{
                    "index": 0, "text": text, "finish_reason": "stop",
                    "prompt_token_ids": ids, "token_ids": comp_ids,
                    "logprobs": {"token_logprobs": [-0.5] * len(comp_ids),
                                 "tokens": ["x"] * len(comp_ids),
                                 "text_offset": list(range(len(comp_ids)))},
                }],
                "usage": {"prompt_tokens": len(ids), "completion_tokens": len(comp_ids),
                          "total_tokens": len(ids) + len(comp_ids)},
            })

    return H


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True, help="JSON list of completion texts")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--served", default="fake")
    args = ap.parse_args()
    script = json.loads(open(args.script).read())
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(script, args.served))
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
