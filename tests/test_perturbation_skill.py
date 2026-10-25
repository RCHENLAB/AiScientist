"""The perturbation_analysis (Perturb-seq) preset pipeline loads into the registry with the tools it
composes. Its CodeAct templates (perturbation_de_vs_control, perturbation_edistance) are skills of
the pinned aiscientist-skills release, checked there."""

from aiscientist.agents.presets import get_preset, list_presets


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


def test_perturbation_analysis_in_selector():
    assert "perturbation_analysis" in {d["key"] for d in list_presets()}
