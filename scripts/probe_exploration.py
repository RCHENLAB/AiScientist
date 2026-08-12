#!/usr/bin/env python3
"""Probe ONE model's ability to open a new research path — the Qwen-vs-frontier A/B.

Hypothesis-driven exploration (``LabConfig.hypothesis_driven``) is the only place the plan can grow,
and it rests on a single judgement the scaffolding cannot make for the model: *given this result, is
there a falsifiable claim worth spending a step on?* That judgement is exactly where a 3B-active MoE
is expected to be weakest — so before swapping the whole lab onto a paid API, measure the one turn
that matters, at one API call per scenario.

The probe drives the REAL production path: the real ``_EXPLORE_SYSTEM`` prompt, the real
``ResearchLab._explore_after_step``, the real deterministic guards and ledger. Only the analysis
result is canned.

It scores BOTH directions, because only measuring the positive case rewards a model that finds
everything surprising:

  * ``surprising`` scenarios — SHOULD yield a falsifiable hypothesis and a step that survives the
    guards. A model that returns nothing here cannot do open-ended research.
  * ``control`` scenarios — a routine, entirely expected result. SHOULD yield nothing. A model that
    invents a path here will burn GPU hours on noise every single step.

Usage (reads ./.env for keys):

    # the model the lab reasoning roles currently use
    PYTHONPATH=src python scripts/probe_exploration.py

    # a candidate, e.g. any OpenAI-compatible endpoint
    PYTHONPATH=src python scripts/probe_exploration.py \
        --base-url https://api.example.com/v1 --model <model-id> --api-key-env MY_API_KEY

    # no network — sanity-check the harness and the guards themselves
    PYTHONPATH=src python scripts/probe_exploration.py --offline
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bioagent.agents.research_harness import (  # noqa: E402
    HarnessContext, ResearchHarness, default_catalog,
)
from bioagent.agents.research_lab import CriticVerdict, LabConfig, ResearchLab  # noqa: E402
from bioagent.core.config import load_project_env  # noqa: E402
from bioagent.providers.openai_compatible import OpenRouterClient  # noqa: E402

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

QUESTION = ("Characterize the cellular composition of this retina sample and how the knockout "
            "differs from the wild type.")

PLAN = [
    "Assess per-cell data quality and remove low-quality or dying cells, then normalize expression.",
    "Group the cells into transcriptionally distinct populations and lay them out on a 2-D map.",
    "For each population, identify the genes that most specifically mark it.",
    "Compare gene expression between the knockout and wild-type animals within each population.",
]

# Each scenario is one accepted step's result. ``expect`` is what a model that can do open-ended
# research SHOULD do with it.
SCENARIOS: list[dict] = [
    {
        "name": "surprising/off-target-population",
        "expect": "new path",
        "step": PLAN[2],
        "answer": (
            "Marker analysis assigned 11 of 12 populations to expected retinal classes (rods, cones, "
            "bipolar, Muller glia, amacrine, horizontal, RGC, microglia, astrocytes, endothelium, "
            "pericytes). Cluster 7 (1,842 cells, 4.1%) is the exception: it co-expresses the rod "
            "programme (RHO, NRL, GNAT1) AND a microglial programme (C1QA, CX3CR1, P2RY12) at high "
            "levels in the same cells, with a median of 4,910 UMIs and 2,110 genes per cell — higher "
            "than either parent class. It is present in the knockout (1,530 cells) and nearly absent "
            "in the wild type (312 cells)."
        ),
        "artifacts": ["markers_by_cluster.csv (12 clusters x 200 genes)",
                      "umap_clusters.png", "qc_metrics.csv"],
    },
    {
        "name": "surprising/contradicts-a-planned-premise",
        "expect": "new path",
        "step": PLAN[0],
        "answer": (
            "QC removed 3,201 cells (mitochondrial fraction > 15%) leaving 44,118 cells and 21,043 "
            "genes. The removed cells are not distributed across the samples as expected: 2,970 of "
            "the 3,201 (93%) come from a single wild-type animal (WT_2), which also shows a median "
            "mitochondrial fraction of 11.4% versus 3.1-3.8% in every other animal, knockout or "
            "wild type."
        ),
        "artifacts": ["qc_metrics.csv", "qc_violin_by_sample.png"],
    },
    {
        "name": "control/routine-expected-result",
        "expect": "nothing",
        "step": PLAN[1],
        "answer": (
            "Leiden clustering at resolution 1.0 produced 12 clusters over 44,118 cells. The UMAP "
            "separates the major retinal classes cleanly, with rods forming the largest cluster "
            "(28,400 cells, 64%), consistent with the expected composition of mouse retina. "
            "Knockout and wild-type cells are intermixed within every cluster."
        ),
        "artifacts": ["umap_clusters.png", "cluster_sizes.csv", "clustered.h5ad"],
    },
]

_SCRIPTED_OFFLINE = {
    "surprising/off-target-population": {
        "surprise": "a population co-expressing rod and microglial programmes appears only in the KO",
        "hypotheses": [{
            "statement": "Cluster 7 is not a real cell type but rod-microglia doublets enriched in "
                         "the knockout by its higher cell-suspension viscosity",
            "prediction": "its cells carry roughly double the UMI and gene counts of either parent "
                          "class and no marker unique to the cluster itself",
            "test": "compare the per-cell UMI/gene distribution of cluster 7 against rods and "
                    "microglia and look for a marker that is specific to cluster 7 alone",
        }],
        "new_steps": [{
            "step": "Compare the per-cell transcript and gene counts of the rod-microglia population "
                    "against pure rods and pure microglia, and test whether any marker gene is "
                    "specific to it alone, to establish whether it is a genuine cell state or a "
                    "doublet artefact.",
            "hypothesis": "Cluster 7 is not a real cell type but rod-microglia doublets enriched in "
                          "the knockout by its higher cell-suspension viscosity",
        }],
    },
    "surprising/contradicts-a-planned-premise": {
        "surprise": "the QC loss is concentrated in one wild-type animal, so the planned KO-vs-WT "
                    "comparison rests on an unequal baseline",
        "hypotheses": [{
            "statement": "The WT_2 animal is a technical outlier whose degraded cells, not genotype, "
                         "would drive the knockout-versus-wild-type differences",
            "prediction": "excluding WT_2 changes the differentially expressed gene set "
                          "substantially, and its remaining cells still show a stress signature",
            "test": "compare knockout versus wild type with and without WT_2 and check the overlap",
        }],
        "new_steps": [{
            "step": "Repeat the knockout versus wild-type comparison with and without the outlier "
                    "animal and measure how much of the difference depends on it, to establish "
                    "whether the genotype effect is real or driven by one degraded sample.",
            "hypothesis": "The WT_2 animal is a technical outlier whose degraded cells, not genotype, "
                          "would drive the knockout-versus-wild-type differences",
        }],
    },
    "control/routine-expected-result": {"surprise": "nothing", "hypotheses": [], "new_steps": []},
}


def _strip_think(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def _live_complete(client: OpenRouterClient):
    def complete(messages):
        return _strip_think(client.chat(messages, max_tokens=1600).content)
    return complete


def _offline_complete(scenario_name: str):
    def complete(_messages):
        return json.dumps(_SCRIPTED_OFFLINE[scenario_name])
    return complete


def _probe(scenario: dict, complete_fn) -> dict:
    """Run ONE scenario through the production exploration turn + guards. Returns a verdict dict."""
    lab = ResearchLab(
        HarnessContext(decisions={}, tunnel_port=1, model="probe"),
        LabConfig(hypothesis_driven=True, max_new_steps=4),
        complete_fn=complete_fn,
        scientist=ResearchHarness(catalog=default_catalog(), chat_fn=lambda *_a, **_k: {}),
    )
    events: list[dict] = []
    result = {"final_answer": scenario["answer"],
              "steps": [{"tool": "run_de_markers", "status": "ok",
                         "result": {"status": "ok", "artifacts": scenario["artifacts"]}}]}
    added = lab._explore_after_step(
        QUESTION, scenario["step"], CriticVerdict("accept", 0.9, "grounded"), result,
        list(PLAN), [], events.append)
    return {"added": added, "ledger": lab._ledger.to_list(), "events": events}


def _render(scenario: dict, out: dict) -> bool:
    """Print the outcome; return True when the model behaved as the scenario expects."""
    want_path = scenario["expect"] == "new path"
    got_path = bool(out["added"])
    ok = want_path == got_path
    print(f"\n{'=' * 78}\n{scenario['name']}   (expect: {scenario['expect']})\n{'=' * 78}")
    print(f"step   : {scenario['step'][:100]}")
    surprise = next((e.get("surprise") for e in out["events"] if e.get("surprise")), "")
    if surprise:
        print(f"surprise: {surprise[:200]}")
    for h in out["ledger"]:
        print(f"\n  [{h['id']}] {h['statement']}")
        print(f"      predicts : {h.get('prediction', '(none)')}")
        print(f"      test     : {h.get('test', '(none)')}")
    for step in out["added"]:
        print(f"\n  + step added: {step}")
    if not out["ledger"] and not out["added"]:
        print("\n  (nothing proposed)")
    elif out["ledger"] and not out["added"]:
        print("\n  (hypothesis raised, but no proposed step survived the guards)")
    print(f"\n  -> {'PASS' if ok else 'FAIL'}: "
          f"{'opened a path' if got_path else 'opened nothing'}, expected "
          f"{'a path' if want_path else 'nothing'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=None, help="OpenAI-compatible /v1 endpoint")
    ap.add_argument("--model", default=None, help="model id served at that endpoint")
    ap.add_argument("--api-key-env", default=None, help="env var holding the API key")
    ap.add_argument("--timeout", type=int, default=180, help="per-call timeout (a cloud reasoning "
                                                             "model over the WAN is slow)")
    ap.add_argument("--offline", action="store_true",
                    help="no network: scripted replies, to check the harness and guards")
    args = ap.parse_args()

    load_project_env(Path(__file__).resolve().parent.parent)
    if args.offline:
        label = "offline (scripted replies — proves the harness, NOT a model)"
    else:
        base_url = args.base_url or os.environ.get("BIOAGENT_LAB_LLM_BASE_URL") \
            or os.environ.get("BIOAGENT_LLM_BASE_URL")
        model = args.model or os.environ.get("BIOAGENT_LAB_LLM_MODEL") \
            or os.environ.get("BIOAGENT_LLM_MODEL")
        key = os.environ.get(args.api_key_env) if args.api_key_env else (
            os.environ.get("BIOAGENT_LAB_LLM_API_KEY") or os.environ.get("BIOAGENT_LLM_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY"))
        if not base_url or not model:
            print("no endpoint configured — pass --base-url/--model, set BIOAGENT_LAB_LLM_* or "
                  "BIOAGENT_LLM_* in .env, or use --offline", file=sys.stderr)
            return 2
        client = OpenRouterClient(api_key=key, model=model, base_url=base_url,
                                  timeout_seconds=args.timeout, endpoint_name="probe")
        label = f"{model} @ {base_url}"

    print(f"probing exploration capability: {label}")
    passed = 0
    for scenario in SCENARIOS:
        complete_fn = _offline_complete(scenario["name"]) if args.offline else _live_complete(client)
        try:
            out = _probe(scenario, complete_fn)
        except Exception as exc:  # noqa: BLE001 - a probe failure is a result, not a crash
            print(f"\n{scenario['name']}: ERROR {type(exc).__name__}: {exc}")
            continue
        passed += _render(scenario, out)

    print(f"\n{'=' * 78}\n{passed}/{len(SCENARIOS)} scenarios behaved as expected.")
    print("A model that fails the 'surprising' scenarios cannot open new research paths; one that "
          "fails the 'control' scenario will invent work on every step. Both matter.")
    return 0 if passed == len(SCENARIOS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
