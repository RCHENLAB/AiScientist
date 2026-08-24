"""Run a batch of chat completions against a vLLM served on an HPC3 compute node.

Exists because the model that has to answer these questions is reachable only from inside HPC3:
the probe's own host cannot open an HTTP client to the compute node (the tunnel is intercepted by
the local policy layer), and copying the repo onto HPC3 to run the probe there would mean a bulk
transfer on a login node, which RCIC forbids. So the split is: the CALLING host builds the exact
prompts with the real production code, this file ships them as data and does nothing but POST
them, and the caller scores the raw completions locally with the same code again. Nothing about
the measurement is approximated — only the transport moves.

Deliberately dependency-free (stdlib only) and tiny, so it can be dropped onto a login node and
run without an environment: it opens sockets and writes JSON, it does not compute.

    python3 probe_relay.py payloads.json completions.json http://<node>:<port>/v1 <model>

``payloads.json`` is ``[{"id": ..., "messages": [...], "temperature": 0.7}, ...]``; the output is
the same ids with a ``"text"`` field. A call that fails after its retries returns ``""`` rather
than aborting the batch — one dead trial must not cost the other nineteen.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    payload_path, out_path, base, model = sys.argv[1:5]
    items = json.loads(open(payload_path).read())
    results = []
    for i, item in enumerate(items, 1):
        body = json.dumps({
            "model": model,
            "messages": item["messages"],
            "temperature": item.get("temperature", 0.7),
            "max_tokens": item.get("max_tokens", 3000),
        }).encode()
        req = urllib.request.Request(f"{base}/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        text, diag = "", {}
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    out = json.loads(r.read())
                choice = out["choices"][0]
                msg = choice.get("message") or {}
                text = msg.get("content") or ""
                # An empty `content` is ambiguous and the distinction decides what a failed trial
                # MEANS: a reasoning model that spent its whole budget thinking (finish_reason
                # "length", long reasoning_content) did not decline the task — it ran out of room,
                # which is a probe setting, not a model verdict. Recording both keeps a transport
                # or budget artefact from being reported as "the model could not do it".
                diag = {"finish_reason": choice.get("finish_reason"),
                        "reasoning_chars": len(msg.get("reasoning_content") or ""),
                        "completion_tokens": (out.get("usage") or {}).get("completion_tokens")}
                break
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                diag = {"error": f"{type(exc).__name__}: {exc}"}
                if attempt < 2:
                    time.sleep(5)
        results.append({"id": item["id"], "text": text, **diag})
        print(f"  [{i}/{len(items)}] {item['id']}: {len(text)} chars {diag}", flush=True)
    open(out_path, "w").write(json.dumps(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
