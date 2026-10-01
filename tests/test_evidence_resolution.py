"""A claimed artifact that is not on disk must not back a conclusion.

`evidence_pointers` is called in four places and NOTHING checked that the files it names exist. So
a tool could name a figure it never wrote and the Critic would ground its verdict on it — which is
exactly what `run_de` did: it returned `figures/rank_genes_groups_leiden_de.png` hardcoded, so the
pointer dangled for every DE grouped by anything other than `leiden`, and it survived in production
until someone read the code.

This is the cheapest member of the failure class that actually matters in this system: the tool
SUCCEEDS, every system-level contract holds, and only an independent check against the filesystem
can contradict it. A model reviewer cannot catch it by reading the result, because the result is
well-formed — it can only catch it if something hands it the negative space.

The check therefore REPORTS rather than judges: an artifact can be mirrored back from HPC3 on its
own schedule, and rejecting real work over a timing race would be a worse failure than the one being
caught.
"""

from __future__ import annotations

from pathlib import Path

from aiscientist.agents.research_lab import resolve_evidence


def _ws(tmp_path: Path, *rel: str) -> Path:
    for r in rel:
        p = tmp_path / "artifacts" / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return tmp_path


def test_present_and_missing_are_separated(tmp_path):
    ws = _ws(tmp_path, "figures/real.png", "tables/de_all.csv")

    present, missing = resolve_evidence(
        ["figures/real.png", "tables/de_all.csv", "figures/never_written.png"], ws)

    assert present == ["figures/real.png", "tables/de_all.csv"]
    assert missing == ["figures/never_written.png"]


def test_the_exact_production_defect_is_caught(tmp_path):
    """run_de grouped by `majorclass` wrote rank_genes_groups_majorclass_de.png but REPORTED
    rank_genes_groups_leiden_de.png."""
    ws = _ws(tmp_path, "figures/rank_genes_groups_majorclass_de.png")

    _present, missing = resolve_evidence(["figures/rank_genes_groups_leiden_de.png"], ws)

    assert missing == ["figures/rank_genes_groups_leiden_de.png"]


def test_paths_resolve_against_the_artifacts_root_not_the_workspace(tmp_path):
    """Tools return paths relative to artifacts/ — resolving against the workspace would mark
    every real artifact missing and make the check worse than useless."""
    ws = _ws(tmp_path, "figures/a.png")
    assert resolve_evidence(["figures/a.png"], ws) == (["figures/a.png"], [])


def test_absolute_paths_are_honoured(tmp_path):
    real = tmp_path / "elsewhere.txt"
    real.write_text("x", encoding="utf-8")
    present, missing = resolve_evidence([str(real), "/nope/missing.txt"], tmp_path)
    assert present == [str(real)] and missing == ["/nope/missing.txt"]


def test_no_workspace_means_no_claim_either_way(tmp_path):
    """Offline tests and library use have no workspace; the check must not invent failures."""
    present, missing = resolve_evidence(["figures/a.png"], None)
    assert present == ["figures/a.png"] and missing == []


def test_blank_entries_are_ignored(tmp_path):
    assert resolve_evidence(["", "   "], _ws(tmp_path)) == ([], [])


def test_nothing_claimed_is_not_a_failure(tmp_path):
    assert resolve_evidence([], _ws(tmp_path)) == ([], [])


# --- how the Critic is told -----------------------------------------------------


def test_the_critic_is_given_only_resolvable_evidence_and_told_about_the_rest(tmp_path):
    import json

    from aiscientist.agents.research_harness import HarnessContext, HarnessResult
    from aiscientist.agents.research_lab import LabConfig, ResearchLab

    _ws(tmp_path, "figures/real.png")
    seen: list = []

    def fn(messages):
        seen.append(messages)
        return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})

    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    lab = ResearchLab(HarnessContext(decisions={}, workspace=tmp_path),
                      LabConfig(auto_select_skill=False), complete_fn=fn, scientist=_Stub())
    result = HarnessResult(
        status="ok", stop_reason=None, final_answer="done",
        steps=[{"tool": "run_de", "ok": True,
                "result": {"status": "ok",
                           "figures": ["figures/real.png", "figures/ghost.png"]}}],
        errors=[])
    events: list = []

    lab._critic("q", "step", result, events.append)

    payload = json.loads(seen[0][1]["content"])
    assert payload["evidence"] == ["figures/real.png"], "a dangling path must not read as evidence"
    assert payload["evidence_MISSING_from_disk"] == ["figures/ghost.png"]
    assert "unsupported" in payload["evidence_note"]
    assert any(e.get("type") == "evidence_missing" for e in events), "and a human must be told"


def test_a_fully_resolvable_step_adds_no_noise(tmp_path):
    import json

    from aiscientist.agents.research_harness import HarnessContext, HarnessResult
    from aiscientist.agents.research_lab import LabConfig, ResearchLab

    _ws(tmp_path, "figures/real.png")
    seen: list = []

    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    lab = ResearchLab(HarnessContext(decisions={}, workspace=tmp_path),
                      LabConfig(auto_select_skill=False),
                      complete_fn=lambda m: (seen.append(m), json.dumps(
                          {"verdict": "accept", "score": 0.9, "critique": "ok"}))[1],
                      scientist=_Stub())
    events: list = []
    lab._critic("q", "step", HarnessResult(
        status="ok", stop_reason=None, final_answer="done",
        steps=[{"tool": "run_de", "ok": True,
                "result": {"status": "ok", "figures": ["figures/real.png"]}}],
        errors=[]), events.append)

    payload = json.loads(seen[0][1]["content"])
    assert "evidence_MISSING_from_disk" not in payload
    assert not [e for e in events if e.get("type") == "evidence_missing"]
