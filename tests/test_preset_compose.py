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
