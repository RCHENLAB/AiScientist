"""A step's role comes from the tool it declares, not from its prose.

The production failure these pin down: plan step "Run `scgpt_annotate` … to request
reference-transferred labels. Record the checkpoint, reference, preprocessing information …" was
classified as a LITERATURE step, because the phrase list treats the bare word ``reference`` as a
literature signal and the guard that should have vetoed it — "this step names an analysis tool" —
only recognised names starting with ``run_``. Nine of the catalog's twenty-two tools do not.

The consequences compounded: the step was routed to the literature path (four ``literature_search``
calls, ``scgpt_annotate`` never invoked), the run ended on ``literature_search_multi_query``, the
Critic scored it 0.1 — and because literature steps force-advance instead of retrying, the whole
scGPT arm of the pipeline died silently while the run reported progress.
"""
from __future__ import annotations

import pytest

from bioagent.agents.research_lab import _declared_tools, _is_literature_step

# The live failure, verbatim in shape: a non-``run_`` tool plus unavoidable domain vocabulary.
_SCGPT_STEP = (
    "**Run foundation-model annotation** — Run `scgpt_annotate` on the unchanged uploaded AnnData, "
    "before any external normalization, to request reference-transferred labels and confidence "
    "scores for all 15,307 original cell barcodes. Record the checkpoint, reference, preprocessing "
    "information, and inference settings actually returned."
)


def test_the_scgpt_step_that_died_is_not_a_literature_step():
    assert _declared_tools(_SCGPT_STEP) == frozenset({"scgpt_annotate"})
    assert _is_literature_step(_SCGPT_STEP) is False


@pytest.mark.parametrize("step", [
    # "reference genome" / "reference allele" are not optional vocabulary in variant work.
    "Run `annotate_variants` on the cohort VCF against the GRCh38 reference genome, recording the "
    "reference allele and assembly actually used.",
    "Use `map_phenotype_to_hpo` to map the free-text phenotype, keeping the ontology reference "
    "version that was applied.",
    "Run `diagnose_disease` and record which reference panel supported each call.",
    "Use `inspect_dataset` to profile the object and note the reference annotations present.",
    # The ``run_`` family was already protected; it must stay protected.
    'Run `run_de` stratified by majorclass with reference="WT".',
    "Audit reference coverage: use `run_code` to compare the scGPT reference taxonomy against the "
    "11 existing major classes.",
])
def test_a_step_naming_an_analysis_tool_is_never_literature(step):
    assert _is_literature_step(step) is False


@pytest.mark.parametrize("step", [
    "Use `literature_search` to find DDX41 retinal phenotype papers.",
    "Run `deep_literature` over the retina corpus for Muller gliosis.",
])
def test_a_step_naming_a_literature_tool_still_is_one(step):
    # The historical keep-set named run_literature/run_paperqa, neither of which the Scientist
    # catalog serves — so the moment the guard could see tool names, a real literature step would
    # have been classified as NOT-literature had the set not been corrected with it.
    assert _is_literature_step(step) is True


def test_prose_still_decides_when_no_tool_is_named():
    # The only case where prose is the only evidence available.
    assert _is_literature_step("Search the published literature for DDX41 retinal phenotypes.") is True
    assert _is_literature_step("Summarise the biological interpretation from published background.") is True


def test_a_backticked_obs_column_is_not_a_tool():
    # Backticks carry column names too; treating any identifier as a tool would let a genuine
    # literature step be vetoed by the word `celltype`.
    step = ("Compare `majorclass` and `celltype` across `sampleid` arms using `run_composition`; "
            "note the reference proportions.")
    assert _declared_tools(step) == frozenset({"run_composition"})
    assert _is_literature_step(step) is False


def test_declared_tools_reads_a_call_with_arguments():
    # The PI is instructed to write the tool WITH its key settings, which is how it usually appears.
    step = 'Run `run_de(stratify_by="majorclass", reference="WT")` on the QC checkpoint.'
    assert "run_de" in _declared_tools(step)
    assert _is_literature_step(step) is False


def test_an_empty_or_toolless_step_does_not_crash():
    assert _is_literature_step("") is False
    assert _declared_tools("") == frozenset()
