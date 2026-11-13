"""Translate existing deployment reference settings for the native VEP service.

Only administrator settings are admitted here. Explicit VARIANT_* values take
precedence, including an empty value that disables a build-specific reference.
"""
from __future__ import annotations

import math
import os


def _enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def deployment_values() -> dict[str, str]:
    from ..gateway.settings import HPCSettings

    st = HPCSettings.from_env()
    values = {
        "VARIANT_VEP_CACHE_DIR_GRCH37": st.vep_cache_dir_grch37,
        "VARIANT_VEP_CACHE_DIR_GRCH38": st.vep_cache_dir_grch38,
        "VARIANT_VEP_CLINVAR_GRCH37": st.vep_clinvar_grch37,
        "VARIANT_VEP_CLINVAR_GRCH38": st.vep_clinvar_grch38,
        "VARIANT_VEP_FORK": str(st.vep_fork),
        "VARIANT_VEP_PLUGINS": str(int(st.vep_plugins_enabled)),
        "VARIANT_IRD_ANNOTATE": str(int(st.ird_annotate_enabled)),
        "VARIANT_SPLICEAI": str(int(st.spliceai_enabled)),
        "VARIANT_SPLICEAI_MAX_VARIANTS": str(st.spliceai_max_variants),
        "VARIANT_DEFAULT_GENE_PANEL": st.default_gene_panel,
        "VARIANT_DEFAULT_MAX_POP_AF": str(st.default_max_pop_af),
        "VARIANT_DEFAULT_REGIONS_BED": st.default_regions_bed,
    }
    plugins = _enabled(os.environ.get("VARIANT_VEP_PLUGINS", values["VARIANT_VEP_PLUGINS"]))
    for name, attr in (("VEP_CADD_SNV", "vep_cadd_snv"), ("VEP_CADD_INDELS", "vep_cadd_indels"),
                       ("VEP_ALPHAMISSENSE", "vep_alphamissense"), ("VEP_REVEL", "vep_revel")):
        for build, suffix in (("GRCH38", ""), ("GRCH37", "_grch37")):
            # Preserve an empty GRCh37 counterpart: using the GRCh38 coordinates
            # for an unstaged GRCh37 predictor would change the science.
            values[f"VARIANT_{name}_{build}"] = str(getattr(st, attr + suffix)) if plugins else ""
    values["VARIANT_VEP_PLUGINS_DIR"] = st.vep_plugins_dir if plugins else ""
    # Normalization, HGVS and SpliceAI also use FASTA when predictor plugins are off.
    values["VARIANT_REF_FASTA_GRCH38"] = st.vep_ref_fasta
    values["VARIANT_REF_FASTA_GRCH37"] = st.vep_ref_fasta_grch37
    if _enabled(os.environ.get("VARIANT_IRD_ANNOTATE", values["VARIANT_IRD_ANNOTATE"])):
        for name, attr in (("IRD_HGMD", "ird_hgmd"), ("IRD_DBSCSNV", "ird_dbscsnv"),
                           ("IRD_RETINA_EXONS", "ird_retina_exons"), ("IRD_ATAC", "ird_atac")):
            values["VARIANT_" + name] = str(getattr(st, attr))
    if _enabled(os.environ.get("VARIANT_SPLICEAI", values["VARIANT_SPLICEAI"])):
        values["VARIANT_SPLICEAI_MODELS"] = st.spliceai_models
        values["VARIANT_SPLICEAI_BIN"] = st.spliceai_bin
    for name in tuple(values):
        if name.endswith(("_GRCH37", "_GRCH38")):
            generic = name.rsplit("_", 1)[0]
            if generic in os.environ and name not in os.environ:
                values[name] = os.environ[generic]
    return values


def default_arguments(values: dict[str, str], provided: dict | None = None) -> dict:
    """Keep the lab's filtering defaults; explicit per-call arguments win."""
    provided = provided or {}
    args = {}
    panel = values.get("VARIANT_DEFAULT_GENE_PANEL", "")
    if panel and "genes" not in provided:
        from ..tools.api import load_gene_panel
        args["genes"] = load_gene_panel(panel)
    if "max_pop_af" not in provided:
        threshold = float(values.get("VARIANT_DEFAULT_MAX_POP_AF") or "0")
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("configured default variant allele frequency must be between 0 and 1")
        if threshold > 0:
            args["max_pop_af"] = threshold
    if "regions_bed" not in provided and values.get("VARIANT_DEFAULT_REGIONS_BED"):
        args["regions_bed"] = values["VARIANT_DEFAULT_REGIONS_BED"]
    return args
