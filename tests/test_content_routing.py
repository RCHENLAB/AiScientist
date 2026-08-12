"""Phase B — content-triage OVERRIDES suffix routing (feature ② + feature ①).

``select_pipeline`` now takes the run-start content modality (from feature ①'s peek/describe). When it
is a real, confident modality, routing is RESTRICTED to that modality's pipelines — the file's actual
BYTES pick the bucket, not its extension. A ``.txt``-named file that is really a VCF must route to a
variant protocol. When triage is unavailable / low-confidence / names a modality with no matching
pipeline, routing falls back to the full-library LLM router whose hint is the suffix-derived profile.
"""

from __future__ import annotations

from bioagent.agents.preset_pipelines import (
    PresetPipeline,
    drop_conflicting_pinned,
    get_pipeline,
    select_pipeline,
)


class _Complete:
    """A stub lab chat callable: returns a fixed reply and records every call (so a test can assert
    the LLM was — or was NOT — consulted, and inspect the protocol listing it was shown)."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        return self.reply


VARIANT_ANNO = get_pipeline("variant_annotation")           # data_type=variants
PHENO = get_pipeline("phenotype_variant_diagnosis")         # data_type=variants
SCRNA = get_pipeline("celltype_annotation")                 # data_type=scrna
DE = get_pipeline("differential_expression")                # data_type=scrna


# --- the override --------------------------------------------------------------------------------


def test_content_variants_routes_to_variants_even_when_llm_would_pick_scrna():
    """The core case: content says variants, the LLM (fed a suffix-y hint) would pick the scrna
    pipeline. With ONE variants pipeline in the library the pick is deterministic — the LLM is not even
    called — and it is the variants pipeline. Content beat the suffix."""
    complete = _Complete(reply="celltype_annotation")       # the LLM's (wrong) suffix-based choice
    lib = (PHENO, SCRNA)
    chosen = select_pipeline(complete, "complete the analysis", "Dataset profile: text file",
                             lib, content_modality="variants", content_confidence="high")
    assert chosen is PHENO
    assert complete.calls == []                             # deterministic single-match: no LLM call


def test_content_variants_with_several_matches_lets_the_llm_pick_within_the_bucket():
    """More than one variants pipeline → the LLM chooses, but only AMONG the variants pipelines: the
    scrna pipeline is never in the listing it sees, so it cannot be chosen."""
    complete = _Complete(reply="variant_annotation")
    lib = (VARIANT_ANNO, PHENO, SCRNA)
    chosen = select_pipeline(complete, "annotate variants", "", lib,
                             content_modality="variants", content_confidence="medium")
    assert chosen is VARIANT_ANNO
    shown = complete.calls[0][1]["content"]                 # the user message with the protocol listing
    assert "variant_annotation" in shown and "phenotype_variant_diagnosis" in shown
    assert "celltype_annotation" not in shown              # the scrna pipeline was excluded from routing


def test_synonym_modality_still_routes():
    """A model that answered 'single_cell' (not the canonical 'scrna') still maps to the scrna bucket."""
    complete = _Complete(reply="anything")
    chosen = select_pipeline(complete, "q", "", (SCRNA,),
                             content_modality="single_cell", content_confidence="high")
    assert chosen is SCRNA and complete.calls == []


# --- the suffix fallback -------------------------------------------------------------------------


def test_low_confidence_content_falls_back_to_the_full_library():
    """Low-confidence triage must NOT override — routing uses the full library + the LLM (suffix hint).
    Here the LLM picks the scrna pipeline and it wins, proving content did not force variants."""
    complete = _Complete(reply="celltype_annotation")
    lib = (PHENO, SCRNA)
    chosen = select_pipeline(complete, "q", "Dataset profile: 5000 cells x 2000 genes", lib,
                             content_modality="variants", content_confidence="low")
    assert chosen is SCRNA
    shown = complete.calls[0][1]["content"]
    assert "phenotype_variant_diagnosis" in shown and "celltype_annotation" in shown  # full library


def test_unknown_modality_falls_back_to_the_full_library():
    complete = _Complete(reply="celltype_annotation")
    chosen = select_pipeline(complete, "q", "", (PHENO, SCRNA),
                             content_modality="unknown", content_confidence="high")
    assert chosen is SCRNA and len(complete.calls) == 1


def test_modality_with_no_matching_pipeline_falls_back():
    """A confident modality that no pipeline serves (a plain table) has no bucket → full-library LLM."""
    complete = _Complete(reply="differential_expression")
    chosen = select_pipeline(complete, "q", "", (SCRNA, DE),
                             content_modality="tabular", content_confidence="high")
    assert chosen is DE and len(complete.calls) == 1       # routed over the full library, not a bucket


def test_absent_content_modality_is_todays_behaviour():
    """No content modality at all → exactly the pre-Phase-B path: full-library LLM router."""
    complete = _Complete(reply="variant_annotation")
    chosen = select_pipeline(complete, "q", "", (VARIANT_ANNO, SCRNA))
    assert chosen is VARIANT_ANNO and len(complete.calls) == 1


# --- coherence with drop_conflicting_pinned -----------------------------------------------------


def test_content_routed_pick_drops_a_conflicting_pinned_pipeline():
    """The content-derived pick carries a real ``data_type``, so drop_conflicting_pinned still reconciles
    a mismatched pinned pipeline against it (a pinned scrna protocol is dropped when content = variants)."""
    complete = _Complete(reply="")
    chosen = select_pipeline(complete, "q", "", (PHENO, SCRNA),
                             content_modality="variants", content_confidence="high")
    assert chosen is PHENO
    kept, dropped = drop_conflicting_pinned([SCRNA], chosen)
    assert dropped == [SCRNA] and kept == []


def test_emit_reason_records_the_content_route():
    events: list[dict] = []
    select_pipeline(_Complete(reply=""), "q", "", (PHENO, SCRNA), emit=events.append,
                    content_modality="variants", content_confidence="high")
    assert events and events[-1]["key"] == "phenotype_variant_diagnosis"
    assert events[-1].get("reason") == "content:variants"


def test_isinstance_guard_and_custom_pipeline_bucket():
    """Sanity: a custom single-pipeline modality bucket routes deterministically too (no dependence on
    the shipped library's exact contents)."""
    v = PresetPipeline(key="myvar", label="my variant path", prompt="x", data_type="variants")
    s = PresetPipeline(key="mysc", label="my sc path", prompt="y", data_type="scrna")
    chosen = select_pipeline(_Complete(reply="mysc"), "q", "", (v, s),
                             content_modality="variants", content_confidence="high")
    assert chosen is v
