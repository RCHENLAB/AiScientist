#!/usr/bin/env python3
"""Real-LLM smoke test for free-text -> HPO mapping (``map_phenotype_to_hpo``).

The unit tests script the LLM, so they prove the GROUNDING (a model cannot inject an invented ID) but
say nothing about whether a real model does its half of the job well: reading a clinician's note,
translating 中文 -> English clinical terms, expanding shorthand ("ERG 熄灭型"), and — the part that
actually bites — flagging negation ("no hearing loss") and family history ("her mother had RP", which
is NOT the patient's phenotype). That is what this script exercises.

It prints, per case, the mapped observed/excluded terms with the METHOD and the source span, so you can
eyeball whether the model earned each term. Cases marked ``expect``/``forbid`` get a PASS/FAIL line —
these are the regressions worth watching, not a score to optimize.

Run against the session's served Qwen (preferred — it is what production uses):
    PYTHONPATH=src python scripts/hpo_mapper_smoke.py --port 37219 --model qwen3.6:35b-a3b
Or against OpenRouter (reads OPENROUTER_API_KEY / OPENROUTER_MODEL from .env or the environment):
    PYTHONPATH=src python scripts/hpo_mapper_smoke.py --openrouter
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(ROOT / ".env")

from bioagent.tools.hpo_terms.mapper import map_text_to_hpo   # noqa: E402

# Realistic notes. `expect` = HPO IDs that must be OBSERVED, `excluded` = must be reported ABSENT,
# `forbid` = must NOT appear at all (the family-history and treatment traps).
CASES: list[dict] = [
    {
        "name": "Chinese note, negation + ERG shorthand",
        "text": "10 岁男孩，自幼夜盲，近两年视野明显缩窄，ERG 呈熄灭型，无听力障碍。",
        "expect": ["HP:0000662"],                 # 夜盲 -> Nyctalopia
        "excluded": ["HP:0000365"],               # 无听力障碍 -> Hearing impairment, ABSENT
    },
    {
        "name": "family history trap",
        "text": "Proband is a 34-year-old man with photophobia and poor colour discrimination. "
                "His mother had retinitis pigmentosa.",
        "expect": ["HP:0000613"],                 # Photophobia — the PATIENT's
        "forbid": ["HP:0000510"],                 # the MOTHER's RP must not become the proband's
    },
    {
        "name": "diagnosis line as written in the case sheet",
        "text": "RP with macular involvement",
        "expect": ["HP:0000510"],
    },
    {
        "name": "English referral, mixed findings",
        "text": "Night blindness since childhood, constricted visual fields, waxy optic disc pallor "
                "and bone spicule pigmentation on fundoscopy. Hearing is normal. Started vitamin A.",
        "expect": ["HP:0000662"],
        "forbid": ["HP:0000365"],                 # 'hearing is normal' -> absent, never observed
    },
    {
        "name": "no phenotype at all (must not invent)",
        "text": "Sample CASE_A was sequenced on an Illumina NovaSeq at 30x coverage.",
        "expect": [],
        "forbid": ["HP:0000556"],                 # the IRD default must not be conjured from nothing
    },
]


def build_chat_fn(args) -> "tuple[object, str]":
    if args.openrouter:
        from bioagent.providers.openai_compatible import OpenRouterClient

        client = OpenRouterClient(reasoning_effort="none", timeout_seconds=90)
        if not client.available:
            print("OPENROUTER_API_KEY not set — cannot run the real-LLM smoke test.")
            sys.exit(2)

        def chat_fn(messages):
            return client.chat(messages, max_tokens=800, temperature=0.0).content

        return chat_fn, f"OpenRouter/{client.model}"

    if not args.port:
        print("Pass --port <tunnel port> (the session's vLLM) or --openrouter.")
        sys.exit(2)
    from bioagent.gateway import vllm_client

    def chat_fn(messages):
        return vllm_client.complete(args.port, args.model, messages, max_tokens=800, timeout=120.0)

    return chat_fn, f"vLLM:{args.port}/{args.model}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=0, help="local port of the vLLM tunnel")
    ap.add_argument("--model", default="qwen3.6:35b-a3b")
    ap.add_argument("--openrouter", action="store_true", help="use OpenRouter instead of the tunnel")
    ap.add_argument("--json", action="store_true", help="dump each full result as JSON")
    args = ap.parse_args()

    chat_fn, backend = build_chat_fn(args)
    print(f"backend: {backend}\n")

    failures = 0
    for case in CASES:
        out = map_text_to_hpo(case["text"], chat_fn=chat_fn)
        print(f"── {case['name']}")
        print(f"   text: {case['text']}")
        print(f"   mode: {out['mode']}  |  hpo {out['hpo_version'].rsplit('/', 2)[-2] if '/' in out['hpo_version'] else '?'}")
        for t in out["observed"]:
            print(f"   [+] {t['hpo_id']:12} {t['name']:42} <- {t['source'] or t['phrase']!r} ({t['method']})")
        for t in out["excluded"]:
            print(f"   [-] {t['hpo_id']:12} {t['name']:42} <- {t['source'] or t['phrase']!r} (ABSENT)")
        for u in out["unmapped"]:
            print(f"   [?] unmapped: {u['phrase']!r}")

        got, absent = set(out["hpo_terms"]), set(out["excluded_hpo"])
        problems = [f"missing {h} in observed" for h in case.get("expect", []) if h not in got]
        problems += [f"missing {h} in excluded" for h in case.get("excluded", []) if h not in absent]
        problems += [f"{h} must not appear" for h in case.get("forbid", []) if h in got]
        if problems:
            failures += 1
            print("   FAIL: " + "; ".join(problems))
        else:
            print("   PASS")
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        print()

    print(f"{len(CASES) - failures}/{len(CASES)} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
