"""The multi-pipeline guidance composer (preset_pipelines.compose_pipeline_prompts): the loaded
pipelines — pinned (console multi-select) + the PI's auto pick — are composed into ONE PI-steering
block. One → its prompt verbatim; several → labeled sections + a reconcile header; empty/None → ''."""
from bioagent.agents.preset_pipelines import (
    PIPELINES,
    compose_pipeline_prompts,
    drop_conflicting_pinned,
    get_pipeline,
)
from bioagent.agents.registry import build_scientist_catalog


def test_compose_empty_is_blank():
    assert compose_pipeline_prompts([]) == ""
    assert compose_pipeline_prompts([None]) == ""


def test_compose_single_is_verbatim_prompt():
    p = get_pipeline("celltype_annotation")
    assert p is not None
    assert compose_pipeline_prompts([p]) == p.prompt


def test_compose_multiple_labels_and_reconcile():
    a, b = get_pipeline("celltype_annotation"), get_pipeline("differential_expression")
    out = compose_pipeline_prompts([a, b])
    assert a.label in out and b.label in out       # both protocols labeled
    assert a.prompt in out and b.prompt in out      # both bodies included in full
    assert "reconcile" in out.lower()               # header tells the PI to merge, not double-run


# --- the frontmatter is a contract with the real catalog ---------------------

def test_every_pipeline_declares_real_tools():
    """A pipeline's ``tools:`` frontmatter is surfaced on the System page as the capability boundary it
    composes. Nothing resolves those names at load time, so a typo would silently advertise a tool that
    does not exist — and the PI would plan a step around it."""
    catalog = {t.name for t in build_scientist_catalog()}
    unknown = {k: [t for t in p.tools if t not in catalog] for k, p in PIPELINES.items()}
    assert {k: v for k, v in unknown.items() if v} == {}


def test_pipelines_carry_data_type_modality():
    assert get_pipeline("variant_annotation").data_type == "variants"
    assert get_pipeline("celltype_annotation").data_type == "scrna"


def test_phenotype_pipeline_is_the_vcf_plus_case_path():
    """The VCF+HPO protocol: same modality as variant_annotation (both are VCF studies), but it composes
    the phenotype tools on top. The two must stay distinguishable to the router by description alone."""
    p = get_pipeline("phenotype_variant_diagnosis")
    assert p.data_type == "variants"                       # so a scanpy pinned pipeline is dropped on it
    assert {"map_phenotype_to_hpo", "run_lirical", "annotate_variants"} <= set(p.tools)
    # the precondition a router / PI must be able to read off the one-liner
    assert "only when" in p.label.lower() or "only if" in p.label.lower()


def test_auto_pick_drops_a_conflicting_pinned_pipeline():
    # A single-cell pipeline was pinned in the chat, but the dataset is a VCF → the PI auto-selects
    # variant_annotation. The dataset wins: the pinned scanpy pipeline is dropped so it is never
    # composed onto the VCF plan.
    scrna = get_pipeline("celltype_annotation")
    variant = get_pipeline("variant_annotation")
    kept, dropped = drop_conflicting_pinned([scrna], variant)
    assert dropped == [scrna] and kept == []


def test_matching_modality_pinned_is_kept():
    # Two single-cell pipelines never conflict — the pinned one survives the auto pick.
    a, b = get_pipeline("celltype_annotation"), get_pipeline("differential_expression")
    kept, dropped = drop_conflicting_pinned([a], b)
    assert kept == [a] and dropped == []


def test_modality_agnostic_pick_drops_nothing():
    # A chosen pipeline with no declared modality can't override a pinned one.
    scrna = get_pipeline("celltype_annotation")
    agnostic = get_pipeline("variant_annotation").__class__(
        key="misc", label="misc", prompt="x", data_type="")
    kept, dropped = drop_conflicting_pinned([scrna], agnostic)
    assert kept == [scrna] and dropped == []


# --- deployment availability -------------------------------------------------
# A preset pipeline can be the best match for the data and still be missing the tool it exists for.
# The live failure: a run announced "Loaded preset pipeline: scGPT foundation-model annotation",
# planned five steps around `scgpt_annotate`, and only the empty sections of the final report
# revealed that the tool could not run. Selection now knows, and says so.

def _pipe(key="scgpt_annotation", tools=("scgpt_annotate", "run_clustering", "run_code"),
          data_type="scrna"):
    from bioagent.agents.preset_pipelines import PresetPipeline
    return PresetPipeline(key=key, label=f"{key} label", prompt="steer", tools=tuple(tools),
                          data_type=data_type)


def test_missing_tools_is_skipped_entirely_when_availability_is_unknown():
    # None means "no catalog to read" — skip the check rather than declare everything unavailable.
    assert _pipe().missing_tools(None) == ()


def test_missing_tools_reports_only_the_gaps_in_declaration_order():
    available = frozenset({"run_clustering", "run_code"})
    assert _pipe().missing_tools(available) == ("scgpt_annotate",)
    assert _pipe().missing_tools(frozenset({"scgpt_annotate", "run_clustering", "run_code"})) == ()


def test_the_router_listing_flags_a_pipeline_it_cannot_run():
    from bioagent.agents.preset_pipelines import select_pipeline

    seen = {}

    def fake_complete(messages):
        seen["listing"] = messages[-1]["content"]
        return "scgpt_annotation"

    select_pipeline(fake_complete, "annotate my h5ad", "",
                    (_pipe(), _pipe(key="celltype_annotation",
                                    tools=("run_clustering", "run_de"))),
                    None, available_tools=frozenset({"run_clustering", "run_de", "run_code"}))
    # The model picks by description; the gap has to be IN the description it reads.
    assert "[NOT AVAILABLE in this deployment: scgpt_annotate]" in seen["listing"]
    assert "celltype_annotation label\n" in seen["listing"] + "\n"   # the intact one is unmarked


def test_the_deterministic_content_route_still_reports_the_gap():
    from bioagent.agents.preset_pipelines import select_pipeline

    events = []
    chosen = select_pipeline(lambda m: "none", "q", "", (_pipe(),), events.append,
                             content_modality="scrna", content_confidence="high",
                             available_tools=frozenset({"run_clustering", "run_code"}))
    # A single modality match is chosen with NO model call — the one path where a silent gap
    # would never be seen by anything.
    assert chosen is not None and chosen.key == "scgpt_annotation"
    picked = [e for e in events if e.get("type") == "skill_selected"]
    assert picked and picked[0]["unavailable_tools"] == ["scgpt_annotate"]


def test_a_tool_is_enabled_by_default_and_scgpt_reports_its_own_state():
    from bioagent.tools.scgpt_annotate.tool import make_scgpt_annotate_tool

    # Present either way — the roster stays honest about what exists — but not usable without a
    # live GPU session, which is exactly the distinction pipeline routing needs.
    assert make_scgpt_annotate_tool(None).enabled is False
    assert make_scgpt_annotate_tool(lambda args, ctx: {"status": "ok"}).enabled is True


# --- the provenance gate must stay two-sided ----------------------------------------------
#
# The first version of this gate said only what NOT to do: "neither integer-like values nor a
# layer named counts proves original UMI provenance ... if none supports counts, drop the
# count-dependent steps". Run 3c5fbc8608a7 followed it exactly and stopped dead on a matrix whose
# values were all non-negative integers, whose row sums reproduced nCount_RNA for all 15,307
# cells, whose non-zero counts reproduced nFeature_RNA, and whose percent.mt agreed to 9e-16 --
# the only thing missing was a written record in `uns`. Six of the fifteen planned steps depended
# on the representation it refused to build. A prohibition with no sufficiency clause does not
# make the analysis careful, it makes it impossible.

def test_the_provenance_gate_says_when_to_proceed_not_only_when_to_stop():
    from bioagent.agents.preset_pipelines import PIPELINES

    for key in ("celltype_annotation", "differential_expression", "scgpt_annotation"):
        prompt = PIPELINES[key].prompt
        # The stop half: a differently-scaled or non-integer matrix is still refused, and counts
        # are never manufactured.
        assert "Not counts." in prompt, key
        for forbidden in ("exponentiating", "rounding", "renaming"):
            assert forbidden in prompt, (key, forbidden)
        # The proceed half: numeric agreement with the stored QC fields is SUFFICIENT, and a
        # missing transformation history is reportable rather than disqualifying.
        assert "That is sufficient — proceed." in prompt, key
        assert "not a stop condition" in prompt, key
        assert "nCount_RNA" in prompt and "total-count field" in prompt, key


def test_the_provenance_gate_never_reverts_to_a_bare_prohibition():
    """The exact sentence that caused the stall. Keeping it out is the regression test — reading
    'nothing proves provenance' with no counter-clause is what sent the model into the dead end."""
    from bioagent.agents.preset_pipelines import PIPELINES

    banned = "Neither integer-like values nor a layer named `counts` proves original UMI"
    for key, pipe in PIPELINES.items():
        assert banned not in pipe.prompt, key


def test_every_skill_points_pathway_work_at_the_offline_tool():
    """Run 3c5fbc8608a7's pathway step hand-rolled its own scoring in run_code, went looking for a
    gene-set collection to download, was declined at the non-allowlisted-host confirmation, and
    recorded "no verified authorized species-compatible collection" -- while GO_Biological_Process_2023,
    Reactome_2022 and MSigDB_Hallmark_2020 sat in the directory run_enrichment reads from, on both
    the gateway host and HPC3. Nothing in the plan's context said those files existed."""
    from bioagent.agents.preset_pipelines import PIPELINES

    for key in ("celltype_annotation", "differential_expression", "scgpt_annotation"):
        # Collapse whitespace: these are wrapped markdown paragraphs, so a phrase that reads as one
        # line in the file can be split across two, and a raw substring check then fails for a
        # reason that has nothing to do with the content being present.
        prompt = " ".join(PIPELINES[key].prompt.split())
        assert "run_enrichment" in prompt, key
        assert "ALREADY ON DISK" in prompt, key
        assert "offline, no download, no network" in prompt, key
        assert "GO_Biological_Process_2023" in prompt, key
        # ...AND the case the first version of this guidance got wrong: run_enrichment consumes
        # run_de's table and returns ORA p-values, so a design with no replication cannot use it.
        # Telling the model to use it anyway sends it at a tool that cannot run; what it needs to
        # know is that the gene sets stay available even when the tool does not.
        assert "no biological replication" in prompt, key
        assert "read the `.gmt` files from that same local directory" in prompt, key
