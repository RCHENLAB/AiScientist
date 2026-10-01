"""A plan must say where each of its choices came from.

From a production run (Ziyaoma/f5111e1a2382). The approved plan named no tool, no parameter and no
protocol; the run then applied a mitochondrial threshold nobody had chosen, bypassed a statistical
guard, dropped a step that was never announced, and produced a manuscript in which every one of
those decisions reads like standard pipeline behaviour. Nothing in the bundle could distinguish
"the protocol specifies this" from "the model picked it this once".

Four separate mechanisms cover that, and each is here because its absence was survivable — which
is precisely why it stayed absent:

* the PI's ``SELF-SOURCED:`` disclosure — what it decided on its own knowledge;
* ``nondefault_params`` — every value that was not the tool's declared default;
* the protocol's identity persisted next to its text;
* the read-back filter — a step that re-reads an earlier step's output is not analysis.
"""

from __future__ import annotations

from bioagent.agents.research_harness import nondefault_params
from bioagent.gateway.settings import LAB_STORAGE, REFERENCE_ROOT, SHARED_ROOT  # noqa: F401
from bioagent.agents.research_lab import (
    _is_readback_step,
    _is_report_busywork,
    _split_self_sourced,
)


# --- the PI's own-knowledge disclosure ---------------------------------------


def test_the_disclosure_is_separated_from_the_executable_agenda():
    """Left in the agenda it would be dispatched to the Scientist as a step to run."""
    agenda, disclosure = _split_self_sourced([
        "Assess per-cell quality with `run_scanpy_qc`.",
        "Compare the conditions within each cell type with `run_pseudobulk_de`.",
        "SELF-SOURCED: chose max_pct_mt=10 rather than the default 20 because retina nuclei "
        "tolerate less mitochondrial signal; the protocol names no threshold.",
    ])
    assert len(agenda) == 2
    assert all("SELF-SOURCED" not in s for s in agenda)
    assert disclosure.startswith("chose max_pct_mt=10")


def test_the_disclosure_is_found_wherever_the_model_put_it():
    """Models put it first about as often as last, and a list-position rule would silently ship
    the disclosure to the Scientist as a step half the time."""
    for steps in (
        ["SELF-SOURCED: departed from the protocol.", "Run QC", "Run DE"],
        ["Run QC", "SELF-SOURCED: departed from the protocol.", "Run DE"],
        ["- **SELF-SOURCED:** departed from the protocol.", "Run QC", "Run DE"],
    ):
        agenda, disclosure = _split_self_sourced(steps)
        assert agenda == ["Run QC", "Run DE"]
        assert "departed from the protocol" in disclosure


def test_a_plan_with_nothing_to_disclose_yields_no_section():
    agenda, disclosure = _split_self_sourced(["Run QC", "Run DE"])
    assert agenda == ["Run QC", "Run DE"] and disclosure == ""


# --- non-default parameter provenance ----------------------------------------

_QC_SCHEMA = {"type": "object", "properties": {
    "min_genes": {"type": "integer", "default": 200, "description": "drop sparse cells"},
    "max_pct_mt": {"type": "number", "default": 20.0, "description": "drop high-mito cells"},
    "force": {"type": "boolean", "default": False, "description": "bypass the guard"},
    "groupby": {"type": "string", "default": "leiden", "description": "column compared"},
    "path": {"type": "string"},
}}


def test_the_run_that_started_this_would_now_declare_its_invented_threshold():
    """The exact call from the production run: two values match the declared defaults, two do not,
    and the manuscript reported all four identically."""
    found = {d["param"]: d for d in nondefault_params(
        _QC_SCHEMA, {"min_genes": 200, "max_pct_mt": 10, "force": True})}
    assert "min_genes" not in found                      # matched the default — nothing to say
    assert found["max_pct_mt"]["value"] == 10 and found["max_pct_mt"]["default"] == 20.0
    assert found["max_pct_mt"]["kind"] == "threshold"
    assert found["max_pct_mt"]["meaning"]                # carries WHAT it does, not just the number
    assert found["force"]["kind"] == "switch"            # a guard bypass, flagged louder


def test_an_int_that_equals_a_float_default_is_not_a_deviation():
    """`20` and `20.0` are the same threshold. Reporting it as a decision would train the reader
    to skip the whole section."""
    assert nondefault_params(_QC_SCHEMA, {"max_pct_mt": 20}) == []


def test_a_column_choice_is_recorded_but_not_treated_as_an_invented_number():
    """Which column to group by is study design, stated in the plan, and would fire on every
    ordinary run — so it is kept in the record and kept out of the live warning."""
    (entry,) = nondefault_params(_QC_SCHEMA, {"groupby": "sampleid"})
    assert entry["kind"] == "selection"


def test_a_parameter_with_no_declared_default_says_nothing():
    """Tools that have not adopted the declared table must not produce noise."""
    assert nondefault_params(_QC_SCHEMA, {"path": "/tmp/x.h5ad"}) == []
    assert nondefault_params({}, {"anything": 1}) == []


# --- the read-back step ------------------------------------------------------


def test_a_step_that_only_re_reads_an_earlier_result_is_recognised():
    """Verbatim from the production plan. It consumed one of six steps, produced nothing the DE
    step had not already returned, and displaced the composition analysis the data needed."""
    assert _is_readback_step(
        "Parse the generated DE tables to extract and report the exact tabular output, including "
        "the total number of genes called significant at default thresholds.")


def test_real_analysis_steps_that_happen_to_say_report_survive():
    """Half of a well-written plan says "report" — the word alone cannot be the signal."""
    for step in (
        "Assess per-cell data quality with `run_scanpy_qc` and report how many cells and genes "
        "remain after filtering.",
        "Compare cell-type proportions between the arms with `run_composition` and report which "
        "populations shift.",
        "Identify the genes marking each population with `run_de` and report their effect sizes.",
    ):
        assert not _is_readback_step(step), step


def test_the_existing_report_filter_would_not_have_caught_it():
    """Why a new predicate rather than pointing the old one at the initial agenda.

    `_is_report_busywork` targets RENDERING a deliverable — it needs a write/compile/export verb
    next to a report noun. The step that got through was phrased as reading, not writing ("Parse …
    to extract and report the exact tabular output"), so it matched neither half. Widening that
    filter to bare "report" would have deleted the three legitimate steps above instead.
    """
    readback = ("Parse the generated DE tables to extract and report the exact tabular output.")
    assert _is_readback_step(readback)
    assert not _is_report_busywork(readback)
    # The old filter still owns what it was built for.
    assert _is_report_busywork("Compile the final report into a .docx deliverable.")


def test_a_plan_naming_an_invented_tool_is_annotated_not_pruned():
    """From a production team-mode plan that named `run_wilcoxon_DE`, `run_cell_bootstrapping` and
    `run_summary_visualization` — none of which exist. Improvising with run_code is ALLOWED, so the
    step must survive; what must not survive is the reviewer's belief that a tested tool will run."""
    import json as _json

    from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
    from bioagent.agents.research_lab import LabConfig, ResearchLab

    agenda = ["**Contrast** — Compare arms with `run_wilcoxon_DE` stratified by majorclass",
              "**Stability** — Bootstrap rankings with `run_cell_bootstrapping`"]

    def complete(messages):
        sys_prompt = messages[0]["content"]
        if "reviewing a DRAFT analysis plan" in sys_prompt:
            return _json.dumps({"issues": [], "revised_agenda": []})
        if "finalizing the analysis plan" in sys_prompt:
            return _json.dumps({"final_agenda": list(agenda)})
        if "Principal Investigator of a bioinformatics lab" in sys_prompt:
            return _json.dumps({"agenda": list(agenda)})
        return "FINAL REPORT: done."

    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), LabConfig(),
                      complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(),
                                                chat_fn=lambda *_a: {"content": "", "tool_calls": [
                                                    {"id": "f", "type": "function",
                                                     "function": {"name": "finish",
                                                                  "arguments": '{"answer": "ok"}'}}]}))
    events: list[dict] = []
    result = lab.run("Compare the arms", on_event=events.append)

    flagged = [f for e in events if e["type"] == "plan_tooling" for f in e["findings"]]
    # Neither name has a single obvious referent in THIS catalog (run_qc / run_de_markers / finish),
    # so both are reported rather than corrected.
    assert {f["tool"] for f in flagged} == {"run_cell_bootstrapping", "run_wilcoxon_DE"}
    assert {f["kind"] for f in flagged} == {"unknown_tool"}
    assert list(result.agenda) == agenda, "annotation must not prune or rewrite the steps"


def test_enrichment_without_a_de_producer_gets_a_contrast_step_inserted():
    """From a production plan (Ziyao's A7): ORA + GSEA whose text said 'reads the DE table the
    previous step wrote' with NO step producing one. The Critic passed it; at run time it would have
    failed as 'no significant DE genes' and read like a null result."""
    import json as _json

    from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
    from bioagent.agents.research_lab import LabConfig, ResearchLab

    agenda = ["**QC** — Filter and normalize with `run_scanpy_qc`",
              "**Pathways** — Test enriched pathways with `run_enrichment` on the DE table"]

    def complete(messages):
        sys_prompt = messages[0]["content"]
        if "reviewing a DRAFT analysis plan" in sys_prompt:
            return _json.dumps({"issues": [], "revised_agenda": []})
        if "finalizing the analysis plan" in sys_prompt:
            return _json.dumps({"final_agenda": list(agenda)})
        if "Principal Investigator of a bioinformatics lab" in sys_prompt:
            return _json.dumps({"agenda": list(agenda)})
        return "FINAL REPORT: done."

    profile = {"obs_categoricals": {"sampleid": {"n": 2, "values": ["DDX41", "WT"]},
                                    "majorclass": {"n": 11, "values": ["Rod", "Cone"]}}}
    lab = ResearchLab(HarnessContext(decisions={"dataset_result": profile}, tunnel_port=1,
                                     model="m"), LabConfig(), complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(),
                                                chat_fn=lambda *_a: {"content": "", "tool_calls": [
                                                    {"id": "f", "type": "function",
                                                     "function": {"name": "finish",
                                                                  "arguments": '{"answer": "ok"}'}}]}))
    events: list[dict] = []
    result = lab.run("What changes between the arms?", on_event=events.append)

    fixed = [e for e in events if e["type"] == "plan_dependency_fixed"]
    assert fixed and fixed[0]["before_step"] == 2
    assert len(result.agenda) == 3
    inserted = result.agenda[1]
    assert "`run_de`" in inserted and "sampleid" in inserted and "WT" in inserted
    assert "stratify_by='majorclass'" in inserted
    assert result.agenda[2] == agenda[1], "the enrichment step itself is untouched"


def test_the_literature_label_never_prints_instruction_residue():
    """The exact strings that reached production plan cards."""
    from bioagent.agents.research_lab import _literature_step_text as f
    generic = "Literature search for the key genes and pathways found"
    assert f("This dataset has 3 donors — please run pseudobulk differential expression for me.") == generic
    assert f("help me analyze this dataset and complete the report.") == generic
    assert f("Analyze this dataset") == generic
    zh = f("DDX41 突变体和 WT 的视网膜之间有什么变化?")
    assert zh == "Literature search for DDX41", zh      # the entity, never a punched-out sentence
    assert f("What changes in Rod cells in the DDX41 mutant?") == "Literature search for Rod DDX41 mutant"


def test_an_inserted_step_is_written_by_the_pi_not_a_template():
    """Deciding THAT a step exists is the guard's job; deciding WHAT it says is the planner's. The
    inserted contrast step must be the PI's sentence when a model is wired — the template is only
    the offline fallback."""
    import json as _json

    from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
    from bioagent.agents.research_lab import LabConfig, ResearchLab

    agenda = ["**QC** — Filter and normalize with `run_scanpy_qc`",
              "**Pathways** — Test enriched pathways with `run_enrichment` on the DE table"]
    PI_SENTENCE = ("**Per-cell-type DDX41 vs WT contrast** — Within each `majorclass` population, "
                   "compare DDX41 against WT cells with `run_de` (Wilcoxon, reference='WT', "
                   "stratify_by='majorclass'), testing genes detected in >=10% of either arm and "
                   "calling changes at adjusted p<0.05 with |log2FC|>=0.25; the cell is the unit, "
                   "so this is an exploratory ranking.")

    def complete(messages):
        sys_prompt = messages[0]["content"]
        if "Your analysis plan is missing ONE step" in sys_prompt:
            return PI_SENTENCE
        if "reviewing a DRAFT analysis plan" in sys_prompt:
            return _json.dumps({"issues": [], "revised_agenda": []})
        if "finalizing the analysis plan" in sys_prompt:
            return _json.dumps({"final_agenda": list(agenda)})
        if "Principal Investigator of a bioinformatics lab" in sys_prompt:
            return _json.dumps({"agenda": list(agenda)})
        return "FINAL REPORT: done."

    profile = {"obs_categoricals": {"sampleid": {"n": 2, "values": ["DDX41", "WT"]},
                                    "majorclass": {"n": 11, "values": ["Rod", "Cone"]}}}
    lab = ResearchLab(HarnessContext(decisions={"dataset_result": profile}, tunnel_port=1,
                                     model="m"), LabConfig(), complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(),
                                                chat_fn=lambda *_a: {"content": "", "tool_calls": [
                                                    {"id": "f", "type": "function",
                                                     "function": {"name": "finish",
                                                                  "arguments": '{"answer": "ok"}'}}]}))
    result = lab.run("What changes between the arms?", on_event=lambda _e: None)
    assert result.agenda[1] == PI_SENTENCE


def test_a_step_naming_an_analysis_tool_is_never_a_literature_step():
    """A production DE step ending '…to guide biological interpretation' matched the literature
    phrase list and was routed to the literature fast path: four literature_search calls, run_de
    never ran, the plan's central contrast was lost twice."""
    from bioagent.agents.research_lab import _is_literature_step
    de = ("**Descriptive differential ranking** — Rank genes with `run_de` (reference='WT', "
          "stratify_by='majorclass') … This yields a ranking to guide biological interpretation.")
    assert not _is_literature_step(de)
    assert _is_literature_step("**Literature grounding** — Search the literature with "
                               "`deep_literature` for DDX41 and attach DOI-backed citations.")
    assert _is_literature_step("Summarize findings with literature context")


def test_every_checkpoint_reading_analysis_tool_is_offloaded_to_hpc():
    """run_composition ran IN-PROCESS on the eyeserver while QC's checkpoint sat on dfs3b — three
    rounds of 'no analysis checkpoint found' on a labelled two-arm dataset, i.e. the protocol's
    first analysis was structurally un-runnable. Every tool scrna_cli can dispatch must be routed."""
    from bioagent.agents.registry import _HPC_ANALYSIS_TOOLS
    from bioagent.tools.scrna_cli import _analysis_tools
    assert set(_analysis_tools()) <= set(_HPC_ANALYSIS_TOOLS)


def test_mode_routing_is_decided_by_the_dataset_first():
    """Routing was an LLM call over the question alone: two near-identical questions on the same
    two-arm annotated dataset went team (7 min, 8-9 sound steps) and single (1 min, 4 steps, no
    composition). The dataset settles it deterministically; the LLM only breaks ties."""
    from bioagent.agents.research_lab import _dataset_mode_rule
    two_arm_labelled = {"obs_categoricals": {"sampleid": {"n": 2, "values": ["DDX41", "WT"]},
                                             "majorclass": {"n": 11, "values": ["Rod"]},
                                             "orig.ident": {"n": 1, "values": ["0"]}}}
    labels_only = {"obs_categoricals": {"majorclass": {"n": 11, "values": ["Rod"]},
                                        "orig.ident": {"n": 1, "values": ["0"]}}}
    assert _dataset_mode_rule(two_arm_labelled) == "team"
    assert _dataset_mode_rule(labels_only) == "single"
    assert _dataset_mode_rule({"obs_categoricals": {}}) == ""     # nothing to go on -> defer
    assert _dataset_mode_rule(None) == ""
    # a QC-ish 2-level column is not a contrast
    assert _dataset_mode_rule({"obs_categoricals": {"DF.classifications": {"n": 2, "values": ["Singlet", "Doublet"]},
                                                    "majorclass": {"n": 11}}}) == "single"


def test_remote_dfs3b_paths_are_not_reported_as_missing_evidence(tmp_path):
    """The HPC3 shell tools return the dfs3b paths they looked at; checked on the gateway host
    those never exist, so a production ORA step was told ALL its evidence was missing while its
    tables sat in artifacts/. Remote artifacts map to their local mirror; other remote paths are
    unverifiable here, not missing."""
    from bioagent.agents.research_lab import resolve_evidence
    art = tmp_path / "artifacts" / "tables"; art.mkdir(parents=True)
    (art / "enrichment_AC_up.csv").write_text("x")
    present, missing = resolve_evidence([
        "tables/enrichment_AC_up.csv",                                       # relative, exists
        "tables/nope.csv",                                                   # relative, missing
        f"{SHARED_ROOT}/Temp/u/analysis/r/artifacts/tables/enrichment_AC_up.csv",
        f"{SHARED_ROOT}/Temp/u/analysis/r/work",     # remote, not artifact
        f"{SHARED_ROOT}/Temp/u/analysis/r/artifacts/tables/gone.csv",
    ], tmp_path)
    assert "tables/enrichment_AC_up.csv" in present
    assert "tables/nope.csv" in missing
    assert any(p.endswith("/artifacts/tables/enrichment_AC_up.csv") for p in present)
    assert not any(p.endswith("/analysis/r/work") for p in missing), "remote non-artifact is not 'missing'"
    assert any(p.endswith("/artifacts/tables/gone.csv") for p in missing)


def test_a_step_that_names_a_tool_the_scientist_never_called_is_nudged_once():
    """From E2E run 3: the DE step ended on the model's own final text after five reconnaissance
    calls, run_de never called; the Critic bounced it and a whole extra round fixed it. One
    deterministic nudge is cheaper. Never applied when the tool WAS called."""
    import json as _json
    from types import SimpleNamespace

    from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
    from bioagent.agents.research_lab import LabConfig, ResearchLab, Specialist

    calls: list[str] = []
    def chat_fn(messages, tools):
        calls.append(messages[-1]["content"] if messages else "")
        n = len(calls)
        # first attempt: recon then final text; second (nudged) attempt: call run_qc then finish
        if n == 1:
            return {"content": "", "tool_calls": [{"id": "a", "type": "function",
                    "function": {"name": "inspect_dataset", "arguments": "{}"}}]}
        if n == 2:
            return {"content": "Ready to run the QC now.", "tool_calls": []}
        if n == 3:
            return {"content": "", "tool_calls": [{"id": "b", "type": "function",
                    "function": {"name": "run_qc", "arguments": "{}"}}]}
        return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                "function": {"name": "finish", "arguments": '{"answer": "done"}'}}]}

    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), LabConfig(),
                      complete_fn=lambda m: "x",
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=chat_fn))
    events: list[dict] = []
    res = lab._scientist("q", "**QC** — Filter cells with `run_qc` at the defaults.",
                         Specialist("Sci", "persona"), "", [], events.append)
    nudges = [e for e in events if e["type"] == "tool_nudge"]
    assert nudges and nudges[0]["tools"] == ["run_qc"]
    assert any(st.get("tool") == "run_qc" for st in res.steps), "the nudged attempt ran the tool"
    assert "never called" not in (calls[0] or ""), "first attempt is un-nudged"
    assert any("without calling `run_qc`" in c for c in calls[1:]), "the nudge names the tool"
