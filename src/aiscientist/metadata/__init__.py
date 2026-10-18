"""Croissant metadata for AiScientist's datasets and analysis results (RCHENLAB/AiScientist issue 9).

Every dataset and every analysis result carries a ``croissant.json`` (MLCommons Croissant 1.1,
JSON-LD on schema.org): what it is, where it came from, how its files are laid out, what its tables'
columns are, and how it may be reused, linked to EDAM, NCBI Taxonomy, UBERON, Cell Ontology and EFO.
The development guideline is ``docs/development/metadata.md``.

This package builds and checks the documents; it knows nothing about the web server or the
database (``aiscientist.gateway.metadata_store`` keeps them in the registry). Generate, don't
hand-write: every document comes from :func:`build_dataset` or :func:`build_result`.
"""

from .croissant import (AISCIENTIST_NS, CONTEXT, CROISSANT_1_1, RAI_1_0, Built, build_dataset,
                        build_result, result_uuid, suggestions)
from .facts import (accession_info, compact_peek, dataset_facts, file_fact, find_accessions,
                    inspect_table, result_files, run_parameters, safe_value, scrub_text)
from .form import FIELDS, clean_user_fields, form_view, planner_note
from .preview import pick_record_set, preview_rows
from .validate import mlcroissant_version, own_checks, validate
from .vocab import public_vocabulary


def vocabulary() -> dict:
    """The review form's choices: ontology terms per field, and the licenses."""
    return public_vocabulary()


__all__ = [
    "AISCIENTIST_NS", "CONTEXT", "CROISSANT_1_1", "FIELDS", "RAI_1_0", "Built", "accession_info",
    "build_dataset", "build_result", "clean_user_fields", "compact_peek", "dataset_facts", "file_fact",
    "find_accessions", "form_view", "inspect_table", "mlcroissant_version", "own_checks",
    "pick_record_set", "planner_note", "preview_rows", "result_files", "result_uuid", "run_parameters",
    "safe_value", "scrub_text", "suggestions", "validate", "vocabulary",
]
