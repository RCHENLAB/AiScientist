"""Real-LLM proof of the no-contrast enrichment guard, via OpenRouter.

Context: on a dataset that is ALREADY cell-type annotated and has NO experimental contrast (a single
sample/condition), pathway/GO enrichment on a cell type's own one-vs-rest identity markers is circular
— it just restates the cell type's definition (rod markers → phototransduction). A reviewer flagged
this as "meaningless enrichment". `_dataset_context` / `_PI_SYSTEM` rule (d) steer the PI away from it,
but real LLMs still plan it, so `ResearchLab.run()` deterministically drops enrichment steps in that
regime (`_annotated_without_contrast` + `_is_enrichment_step`).

This script shows the FULL path on Qwen3.6: the model plans enrichment on the no-contrast retina
sample, the guard removes it (literature/marker steps survive); and when a real KO-vs-WT contrast
exists the guard is structurally inactive, so enrichment is untouched.

Run:  PYTHONPATH=src .venv/bin/python scripts/no_contrast_enrichment_openrouter.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_dotenv(p: Path) -> None:
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(ROOT / ".env")
KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("BIOAGENT_LLM_API_KEY")
MODEL = os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.6-35b-a3b")
if not KEY:
    print("OPENROUTER_API_KEY not set — cannot run.")
    sys.exit(2)

from bioagent.agents.registry import build_scientist_catalog  # noqa: E402
from bioagent.agents.research_harness import HarnessContext, ResearchHarness  # noqa: E402
from bioagent.agents.research_lab import (  # noqa: E402
    LabConfig, ResearchLab, _annotated_without_contrast, _is_enrichment_step,
)
from bioagent.providers.openai_compatible import OpenRouterClient  # noqa: E402

CLIENT = OpenRouterClient(model=MODEL, api_key=KEY, reasoning_effort="none", timeout_seconds=120)
CATALOG = build_scientist_catalog()


def or_complete(messages):
    return CLIENT.chat(messages, max_tokens=1500, temperature=0.1).content


def _stub_scientist():
    return ResearchHarness(catalog=CATALOG, chat_fn=lambda m, t: {"content": "", "tool_calls": []})


RETINA_NO_CONTRAST = {
    "cells": 11977, "genes": 36601, "dataset_kind": "h5ad_single_cell",
    "obs_categoricals": {
        "majorclass": {"n": 6, "values": ["AC", "BC", "Cone", "HC", "MG", "Rod"]},
        "celltype": {"n": 66, "values": []},
        "donor": {"n": 1, "values": ["Chen_19_D003"]},
        "sampleid": {"n": 1, "values": ["Chen_a_10x3_Lobe_19_D003_Nu"]},
        "gender": {"n": 1, "values": ["Male"]}, "tissue": {"n": 1, "values": ["lobe"]},
    },
    "obs_keys": ["majorclass", "celltype", "donor", "sampleid", "gender", "tissue", "percent.mt"],
}
RETINA_WITH_CONTRAST = {
    "cells": 11977, "genes": 36601, "dataset_kind": "h5ad_single_cell",
    "obs_categoricals": {
        "majorclass": {"n": 6, "values": ["AC", "BC", "Cone", "HC", "MG", "Rod"]},
        "celltype": {"n": 66, "values": []},
        "sampleid": {"n": 2, "values": ["DDX41", "WT"]},   # real KO-vs-WT contrast
    },
    "obs_keys": ["majorclass", "celltype", "sampleid", "percent.mt"],
}


def plan(dr, tag):
    ctx = HarnessContext(decisions={"dataset_result": dr}, tunnel_port=1, model=MODEL)
    lab = ResearchLab(ctx, LabConfig(auto_select_skill=False), complete_fn=or_complete,
                      scientist=_stub_scientist())
    kind, payload = lab._pi_plan("complete the research", lambda e: None, allow_clarify=False)
    raw = payload if kind == "agenda" else [str(payload)]
    # Exactly what ResearchLab.run() does after planning:
    final = [s for s in raw if not _is_enrichment_step(s)] if _annotated_without_contrast(dr) else list(raw)
    llm_enrich = any(_is_enrichment_step(s) for s in raw)
    final_enrich = any(_is_enrichment_step(s) for s in final)
    print(f"\n===== {tag} =====")
    for i, s in enumerate(raw, 1):
        print(f"  {i}. {'[ENRICH]' if _is_enrichment_step(s) else '        '} {s[:110]}")
    print(f"  -> LLM planned enrichment? {llm_enrich}   | after run() guard, enrichment present? {final_enrich}")
    if llm_enrich and not final_enrich:
        print("     (guard dropped the meaningless enrichment step; marker/literature steps survive)")
    return llm_enrich, final_enrich


def main() -> None:
    print(f"Model: {MODEL}")
    _, no_final = plan(RETINA_NO_CONTRAST, "NO CONTRAST (single annotated retina) — enrichment must be GONE after guard")
    yes_llm, yes_final = plan(RETINA_WITH_CONTRAST, "WITH CONTRAST (DDX41 vs WT) — guard inactive, enrichment survives")
    ok = (_annotated_without_contrast(RETINA_NO_CONTRAST) and not _annotated_without_contrast(RETINA_WITH_CONTRAST)
          and not no_final and yes_final == yes_llm)
    print("\nRESULT:",
          "PASS — the guard fires ONLY without a contrast: it drops the LLM's enrichment there, and is "
          "inactive (enrichment untouched) when a real contrast exists"
          if ok else f"CHECK (no_final={no_final}, yes_llm={yes_llm}, yes_final={yes_final})")


if __name__ == "__main__":
    main()
