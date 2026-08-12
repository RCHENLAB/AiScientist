"""The perturbation_analysis (Perturb-seq) preset pipeline loads into the registry, its Perturb-seq
atomic skills are in the shared skills/ library, and every skill template at least COMPILES.

The skills are CodeAct templates the Scientist adapts via run_code — they are never imported or
executed in CI, so a syntax slip would otherwise ship silently. compile() parses without running any
imports, so the scanpy/pertpy-importing templates are safe to check this way."""
from pathlib import Path

from bioagent.agents.presets import get_preset, list_presets
from bioagent.agents.skills import SKILLS


def test_perturbation_analysis_skill_loads():
    p = get_preset("perturbation_analysis")
    assert p is not None, "perturbation_analysis skill did not load into the preset registry"
    assert p.prompt.strip()                                  # body = the PI planning guidance
    low = p.prompt.lower()
    assert "perturb" in low and "control" in low and "e-distance" in low
    # composes the DE + code-exec tools — the shared-reference (vs one control) DE needs run_code,
    # which run_de (per-cluster one-vs-rest) does not cover.
    for t in ("run_de", "run_enrichment", "run_code"):
        assert t in p.tools, f"expected tool {t!r} in {p.tools}"
    # The Perturb-seq CodeAct templates now live in the shared atomic-skill library (skills/),
    # surfaced by progressive disclosure — not bundled on the pipeline.
    assert {"perturbation_de_vs_control", "perturbation_edistance"} <= set(SKILLS)
    for name in ("perturbation_de_vs_control", "perturbation_edistance"):
        assert SKILLS[name].summary.startswith("Reference template"), (name, SKILLS[name].summary)


def test_perturbation_analysis_in_selector():
    assert "perturbation_analysis" in {d["key"] for d in list_presets()}


def test_all_skill_scripts_compile():
    # Each skill is a folder skills/<name>/ with a SKILL.md + reference.py (+ any bundle); compile
    # every bundled .py so a syntax slip in a never-imported CodeAct template can't ship silently.
    skills = Path(__file__).resolve().parents[1] / "skills"
    scripts = sorted(skills.glob("*/*.py"))
    assert scripts, "no skills/*/*.py found — wrong dir?"
    for s in scripts:
        compile(s.read_text(encoding="utf-8"), str(s), "exec")   # parse only; raises SyntaxError
