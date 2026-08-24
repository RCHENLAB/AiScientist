"""Does splitting the evidence actually produce disagreement worth having?

Runs the SAME interpretation meeting twice against the real model:

  SHARED     every expert sees all findings (what the code did before)
  SPLIT      each expert is dealt a different slice and told its view is partial (what it does now)

The findings are deliberately built so that a member looking only at part of them would reach a
DIFFERENT conclusion than a member looking at another part — a real study's evidence does this all
the time (a strong per-cell signal that a per-donor test does not support). Prints each expert's
take and the PI synthesis for both arms.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, "/Users/yijunsun/Documents/BioAgentPrototype./.claude/worktrees/"
                   "vcf-normalization-variants-cef98c/src")

from bioagent.agents.research_harness import HarnessContext        # noqa: E402
from bioagent.agents.research_lab import (                          # noqa: E402
    CriticVerdict, LabConfig, LabRound, ResearchLab, Specialist,
)
from bioagent.gateway import vllm_client                            # noqa: E402

MODEL = "QuantTrio/Qwen3.6-35B-A3B-AWQ"

EXPERTS = (Specialist("Single-cell bioinformatician",
                      "You specialise in scRNA-seq analysis, clustering and differential expression."),
           Specialist("Biostatistician",
                      "You specialise in experimental design, replication and inference validity."),
           Specialist("Retina disease biologist",
                      "You specialise in retinal cell biology and inherited retinal disease."))

# Evidence that genuinely conflicts depending on which slice you hold.
FINDINGS = [
    ("Per-cell Wilcoxon DE, DDX41 vs WT within each subclass",
     "1,204 genes reach adjusted p < 0.05 in Rod cells; oxidative phosphorylation is strongly "
     "up-regulated and synaptic transmission down-regulated."),
    ("Replication structure audit",
     "obs['orig.ident'] has exactly ONE level: there is one sample per arm. No biological "
     "replication exists in this dataset."),
    ("Pseudobulk DE across samples",
     "run_pseudobulk_de refused every cell type: needs >=2 samples per arm, has 1. No valid "
     "condition-level p-value could be computed."),
    ("Pathway enrichment on the per-cell DE genes",
     "Reactome: Oxidative Phosphorylation adj-p 1e-12, Neuronal System adj-p 3e-9 — a coherent, "
     "highly significant mitochondrial/synaptic programme."),
    ("Cell-type coverage",
     "7 of 12 subclasses were tested; Endothelial_pericyte, HC, Microglia, RGC and RPE were "
     "skipped for having 12-38 cells in one arm."),
    ("Literature",
     "DDX41 loss is reported to impair mitochondrial function in haematopoietic cells; no retina "
     "study exists."),
]


def _rounds() -> list[LabRound]:
    return [LabRound(i, i, step, "X",
                     {"final_answer": ans, "steps": [{"tool": "run_de", "ok": True,
                                                      "result": {"status": "ok",
                                                                 "tables": [f"tables/t{i}.csv"]}}]},
                     CriticVerdict("accept", 0.9, ""))
            for i, (step, ans) in enumerate(FINDINGS, 1)]


class _Stub:
    catalog: list = []

    def add_tools(self, *_a, **_k):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()

    def complete(messages):
        return vllm_client.complete(args.port, MODEL, messages, timeout=600.0)

    topic = ("Interpret the results: what can we conclude about the DDX41 effect, and how "
             "confident should the manuscript be?")
    q = "Compare DDX41 mutant vs wild-type retina per cell class."

    for arm in ("SHARED", "SPLIT"):
        lab = ResearchLab(HarnessContext(decisions={}, workspace=Path("/tmp")),
                          LabConfig(auto_select_skill=False, meeting_rounds=1,
                                    meeting_tools=False),      # isolate the asymmetry variable
                          complete_fn=complete, scientist=_Stub())
        if arm == "SHARED":
            import bioagent.agents.research_lab as m
            orig = m._assign_evidence
            # Reproduce the OLD behaviour: everyone sees everything (i.e. nothing assigned).
            m._assign_evidence = lambda experts, rounds: [""] * len(experts)
            try:
                out = lab._team_meeting(q, topic, EXPERTS, "interpretation", lambda _e: None,
                                        rounds_ctx=_rounds())
            finally:
                m._assign_evidence = orig
        else:
            out = lab._team_meeting(q, topic, EXPERTS, "interpretation", lambda _e: None,
                                    rounds_ctx=_rounds())

        print("\n" + "=" * 78)
        print(f"===== {arm} — PI synthesis =====")
        print(out[:2600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
