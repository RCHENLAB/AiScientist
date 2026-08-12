#!/usr/bin/env python3
"""A/B test: does the operon-style PROTOCOL.md steer the PI planner as well as (or better than)
the current terse SKILL.md — while being far more human-readable?

Both bodies are fed as PI guidance to the SAME minimal planner prompt on a realistic rare-disease
(IRD, GRCh37) WGS-VCF study. Each resulting agenda is scored two ways:

  * DETERMINISTIC (no LLM, the reliable signal): 7-phase coverage, scientific-param correctness
    (max_pop_af=0.01, assembly picked up as GRCh37 not defaulted to GRCh38, a gene panel), and
    anti-patterns (a report-writing step we explicitly forbid; a plan collapsed to <4 steps).
  * LLM-JUDGE (optional): a stronger model scores faithfulness/completeness + rates each BODY's
    readability for a non-CS bioinformatician.

No third-party deps — stdlib urllib only. Reads the key from OPENROUTER_API_KEY / BIOAGENT_LLM_API_KEY.

Usage:
    export OPENROUTER_API_KEY=sk-or-...
    python experiments/protocol_format/ab_test.py --trials 3
    python experiments/protocol_format/ab_test.py --trials 3 --judge            # + LLM judge
    python experiments/protocol_format/ab_test.py --planner-model qwen/qwen3.6-35b-a3b \
        --judge-model anthropic/claude-3.5-sonnet --trials 5 --judge
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD_BODY = ROOT / "preset_pipelines" / "variant_annotation" / "SKILL.md"
NEW_BODY = ROOT / "experiments" / "protocol_format" / "variant_annotation" / "PROTOCOL.md"
RESULTS = Path(__file__).resolve().parent / "results"

OPENROUTER_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1") + "/chat/completions"

# The study exercises the rare-disease path (panel + max_pop_af) AND assembly awareness: the VCF is
# GRCh37, so a good plan must NOT default to the GRCh38 the tool assumes.
STUDY = (
    "Dataset: a whole-genome-sequencing VCF (~1.1 GB, bgzipped) from a single patient with a suspected "
    "inherited retinal disease (IRD). The VCF header shows it was called against GRCh37/hg19. "
    "Goal: find the causal variant(s)."
)

PLANNER_SYSTEM = (
    "You are the PI of a bioinformatics lab. You draft an ordered analysis agenda, then a Scientist "
    "executes each step with the available tools and a Critic reviews it. Draft the agenda for the study "
    "below, following the research protocol guidance provided. Return ONLY a JSON object of the form "
    '{\"steps\": [{\"n\": 1, \"title\": \"...\", \"tool\": \"...\", \"params\": {\"key\": \"value\"}}]} '
    "— no prose, no markdown fences. `tool` is the tool/skill the step uses; `params` are the concrete "
    "arguments you would pass (gene panel, cutoffs, assembly, etc.)."
)

JUDGE_SYSTEM = (
    "You are a careful evaluator of bioinformatics analysis plans. You will be given a study, a plan "
    "(as JSON), and the protocol the planner was told to follow. Score the plan. Return ONLY JSON: "
    '{\"completeness\": 0-10, \"param_correctness\": 0-10, \"faithfulness\": 0-10, '
    '\"hallucinated_steps\": [\"...\"], \"notes\": \"one sentence\"}. '
    "completeness = are the real phases (QC, normalise, narrow-to-panel+drop-common, annotate, summarise, "
    "prioritise+tier, literature) present and ordered. param_correctness = did it use max_pop_af=0.01, "
    "carry the GRCh37 assembly (NOT GRCh38), and use a gene panel. faithfulness = no invented steps, no "
    "report-writing step, not collapsed into a single annotate call."
)

READABILITY_SYSTEM = (
    "You are a bioinformatics wet-lab researcher who is NOT a programmer. You are asked to review a "
    "lab analysis protocol document and decide whether you could audit each step — understand what is "
    "done, with which parameters, and how to check it was done right. Return ONLY JSON: "
    '{\"readability\": 0-10, \"can_audit_steps\": true|false, \"strengths\": \"...\", \"gaps\": \"...\"}.'
)


# ---- OpenRouter (stdlib only) ------------------------------------------------

def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("BIOAGENT_LLM_API_KEY")
    if not key:
        sys.exit("No API key. Set OPENROUTER_API_KEY (or BIOAGENT_LLM_API_KEY) in your shell and re-run.")
    return key


def chat(model: str, system: str, user: str, *, temperature: float = 0.3, retries: int = 3) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
    }).encode()
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "http://localhost/bioagent-prototype"),
        "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "BioAgent protocol A/B"),
    }
    last = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(OPENROUTER_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}: {exc.read().decode()[:300]}"
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"OpenRouter call failed after {retries} tries: {last}")


def _strip_frontmatter(text: str) -> str:
    """Feed the planner the BODY only — the same `.prompt` the preset loader extracts from a SKILL.md."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[text.find("\n", end + 1) + 1:].lstrip()
    return text


def _extract_json(text: str) -> dict:
    """Tolerate a model that wraps JSON in prose / ```json fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


# ---- Deterministic scorer (the reliable signal) ------------------------------

PHASES = {
    "qc":         (r"\bqc\b|ti/?tv|quality|callset|vcf_qc"),
    "normalise":  (r"normali|bcftools\s+norm|left.?align|normalize_vcf"),
    "narrow":     (r"panel|gene list|known.gene|max_pop_af|restrict|narrow|drop.?common|regions?_bed"),
    "annotate":   (r"annotat|\bvep\b"),
    "summarise":  (r"summari|landscape|distribution|consequence|impact"),
    "prioritise": (r"priorit|tier|shortlist|high.?priority|clinical_variant"),
    "literature": (r"literatur|citation|pubmed|europe\s*pmc|reference"),
}


def score_plan(plan: dict) -> dict:
    steps = plan.get("steps", []) if isinstance(plan, dict) else []
    blob = json.dumps(steps).lower()

    covered = {ph: bool(re.search(pat, blob)) for ph, pat in PHASES.items()}
    coverage = sum(covered.values())

    params_ok = {
        "max_pop_af_0.01": bool(re.search(r"0\.01|1\s*%|max_pop_af", blob)),
        "assembly_grch37": bool(re.search(r"grch37|hg19|b37", blob)),
        "gene_panel":      bool(re.search(r"panel|gene", blob)),
    }
    # penalise the WRONG assembly if it defaulted to GRCh38 without ever noting GRCh37
    wrong_assembly = bool(re.search(r"grch38", blob)) and not params_ok["assembly_grch37"]

    anti = {
        "report_writing_step": bool(re.search(r"write.{0,12}report|manuscript|generate.{0,12}report", blob)),
        "collapsed_lt4_steps": len(steps) < 4,
        "wrong_assembly_grch38": wrong_assembly,
    }

    def _idx(ph):
        for i, s in enumerate(steps):
            if re.search(PHASES[ph], json.dumps(s).lower()):
                return i
        return 99
    order_ok = _idx("annotate") > min(_idx("qc"), _idx("normalise"), _idx("narrow"))

    score = coverage + sum(params_ok.values()) + (1 if order_ok else 0) - sum(anti.values())
    return {
        "n_steps": len(steps), "coverage": coverage, "covered": covered,
        "params_ok": params_ok, "order_ok": order_ok, "anti_patterns": anti,
        "det_score": score,  # max = 7 + 3 + 1 = 11
    }


# ---- Runner ------------------------------------------------------------------

def run(args) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    bodies = {"OLD_skill": _strip_frontmatter(OLD_BODY.read_text()),
              "NEW_protocol": _strip_frontmatter(NEW_BODY.read_text())}
    for name, b in bodies.items():
        print(f"  {name}: {len(b)} chars (~{len(b)//4} tokens)")

    rows, raw = [], []
    for cond, body in bodies.items():
        user = f"# Research protocol guidance\n{body}\n\n# Study\n{STUDY}\n\nDraft the agenda now."
        for t in range(args.trials):
            print(f"[plan] {cond} trial {t+1}/{args.trials} on {args.planner_model} ...", flush=True)
            try:
                out = chat(args.planner_model, PLANNER_SYSTEM, user, temperature=args.temperature)
                plan = _extract_json(out)
            except Exception as exc:  # noqa: BLE001 — record and continue
                print(f"    ! {exc}")
                raw.append({"cond": cond, "trial": t, "error": str(exc)})
                continue
            sc = score_plan(plan)
            row = {"cond": cond, "trial": t, **sc}
            if args.judge:
                jin = f"# Study\n{STUDY}\n\n# Plan\n{json.dumps(plan)}\n\n# Protocol\n{body[:6000]}"
                try:
                    j = _extract_json(chat(args.judge_model, JUDGE_SYSTEM, jin, temperature=0.0))
                    row["judge"] = j
                except Exception as exc:  # noqa: BLE001
                    row["judge"] = {"error": str(exc)}
            rows.append(row)
            raw.append({"cond": cond, "trial": t, "plan": plan, "score": sc})
            print(f"    det_score={sc['det_score']}/11  coverage={sc['coverage']}/7  "
                  f"steps={sc['n_steps']}  params={sum(sc['params_ok'].values())}/3  "
                  f"anti={sum(sc['anti_patterns'].values())}")

    # Optional: rate each BODY's human readability directly.
    read = {}
    if args.judge:
        for cond, body in bodies.items():
            try:
                read[cond] = _extract_json(chat(args.judge_model, READABILITY_SYSTEM, body, temperature=0.0))
            except Exception as exc:  # noqa: BLE001
                read[cond] = {"error": str(exc)}

    _summarise(rows, read, bodies)
    (RESULTS / "raw.json").write_text(json.dumps(raw, indent=2))
    (RESULTS / "rows.json").write_text(json.dumps(rows, indent=2))
    print(f"\nRaw outputs → {RESULTS}/raw.json  (per-trial plans + scores)")


def _summarise(rows, read, bodies) -> None:
    print("\n" + "=" * 72 + "\nSUMMARY (deterministic — higher is better)\n" + "=" * 72)
    for cond in bodies:
        r = [x for x in rows if x["cond"] == cond]
        if not r:
            print(f"{cond:14s}  (no successful trials)")
            continue
        n = len(r)
        avg = lambda k: sum(x[k] for x in r) / n  # noqa: E731
        det = avg("det_score"); cov = avg("coverage"); steps = avg("n_steps")
        anti = sum(sum(x["anti_patterns"].values()) for x in r) / n
        pk = {k: sum(x["params_ok"][k] for x in r) for k in r[0]["params_ok"]}
        print(f"{cond:14s}  det={det:4.1f}/11  coverage={cov:3.1f}/7  steps={steps:3.1f}  "
              f"anti={anti:.2f}  params={pk}")
        if r[0].get("judge"):
            js = [x["judge"] for x in r if isinstance(x.get("judge"), dict) and "error" not in x["judge"]]
            if js:
                javg = lambda k: sum(j.get(k, 0) for j in js) / len(js)  # noqa: E731
                print(f"{'':14s}  judge: complete={javg('completeness'):.1f} "
                      f"param={javg('param_correctness'):.1f} faithful={javg('faithfulness'):.1f}")
    if read:
        print("\nBODY readability (LLM-as-non-CS-researcher, higher is better):")
        for cond, rd in read.items():
            if "error" in rd:
                print(f"  {cond:14s}  judge error: {rd['error']}")
            else:
                print(f"  {cond:14s}  readability={rd.get('readability')}/10  "
                      f"can_audit_steps={rd.get('can_audit_steps')}  gaps: {rd.get('gaps','')[:80]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--planner-model", default=os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.6-35b-a3b"))
    ap.add_argument("--judge-model", default="anthropic/claude-3.5-sonnet")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=0.4)
    ap.add_argument("--judge", action="store_true", help="also run the LLM judge + readability rating")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
