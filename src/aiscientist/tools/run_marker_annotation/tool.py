"""The ``run_marker_annotation`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_advanced.py`` by ``scripts/refactor/split_tools.py``: the code is the
old module's text, verbatim, with only the imports rewritten. Helpers that several tools use live
in ``aiscientist.tools._lib.scrna``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
    _dirs,
    _import_scanpy,
    _missing,
    _rel,
    _run_rel,
    _step_input,
    _write_table,
)
from ..sdk import HarnessTool


# Curated reference panels, one JSON per tissue: {species: {cell type: {panel, discriminators,
# aliases}}} plus the lineages every sample of the tissue contains (``expected``). A label is a
# claim about biology; its markers come from here, not from a model's memory. On 2026-10-02 a
# model-written retina panel put OPN4/NEFM/MEF2C under cones, RLBP1 under bipolar cells and
# astrocyte markers under Muller glia, and 1,631 Muller glia ended up "Unassigned" or "ganglion".
_REFERENCES = Path(__file__).with_name("references")
# The canonical check: a label stands only if one of its lineage-specific markers is clearly higher
# in the cluster than in clusters given OTHER labels (log-normalised mean difference) and detected
# in enough of the cluster's cells.
_CANON_MIN_DELTA = 0.5
_CANON_MIN_FRACTION = 0.2


def available_references() -> list[str]:
    """The tissues a curated reference panel exists for (``reference`` values)."""
    return sorted(p.stem for p in _REFERENCES.glob("*.json"))


def _load_reference(name: str) -> "dict[str, Any] | None":
    path = _REFERENCES / f"{name}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", str(name).lower().replace("ü", "u"))


def _match_type(types: dict[str, Any], caller_name: str) -> "str | None":
    """The reference cell type a caller's label means ("Photoreceptor (rod)" -> "Rod photoreceptor")."""
    key = _norm(caller_name)
    for ref_type, spec in types.items():
        if _norm(ref_type) == key or any(a in key for a in spec.get("aliases", [])):
            return ref_type
    return None


def _pick_species(ref: dict[str, Any], present: set[str]) -> str:
    """The species whose symbols the object uses: the one with the most discriminators present."""
    def hits(sp: str) -> int:
        return sum(g in present for spec in ref["species"][sp].values() for g in spec["discriminators"])
    return max(ref["species"], key=hits)


def _resolve_panel(args: dict[str, Any], present: set[str]) -> "tuple[dict, dict, dict, list[str]]":
    """``(panel, discriminators, reference_info, warnings)`` for this call.

    With a reference (named, or ``auto`` when three or more of the caller's cell types are that
    tissue's lineages) the curated definitions are used for every lineage the reference knows. A
    caller's extra cell type is kept, minus any gene that is another lineage's specific marker."""
    caller_panel = args.get("panel") if isinstance(args.get("panel"), dict) else {}
    caller_disc = args.get("discriminators") if isinstance(args.get("discriminators"), dict) else {}
    wanted = str(args.get("reference") or "auto").strip().lower()
    warnings: list[str] = []
    ref, ref_name, auto = None, "", False
    if wanted not in ("auto", "none", ""):
        ref, ref_name = _load_reference(wanted), wanted
        if ref is None:
            raise ValueError(f"no curated reference '{wanted}'; available: {available_references()}")
    elif wanted == "auto" and caller_panel:
        for name in available_references():
            cand = _load_reference(name) or {}
            types = next(iter(cand.get("species", {}).values()), {})
            if sum(_match_type(types, c) is not None for c in caller_panel) >= 3:
                ref, ref_name, auto = cand, name, True
                break
    if ref is None:
        return dict(caller_panel), dict(caller_disc), {}, warnings

    species = _pick_species(ref, present)
    types = ref["species"][species]
    panel = {t: list(spec["panel"]) for t, spec in types.items()}
    disc = {t: list(spec["discriminators"]) for t, spec in types.items()}
    owner = {g: t for t, spec in types.items() for g in spec["discriminators"]}
    mapped: dict[str, str] = {}
    dropped: dict[str, list[str]] = {}
    for cname, genes in caller_panel.items():
        ref_type = _match_type(types, cname)
        if ref_type:
            mapped[cname] = ref_type        # the curated definition wins
            continue
        keep = [g for g in genes if g not in owner]
        lost = sorted(set(genes) - set(keep))
        if lost:
            dropped[cname] = lost
            warnings.append(f"Removed {', '.join(lost)} from '{cname}': each is a specific marker of "
                            f"another lineage ({', '.join(f'{g}: {owner[g]}' for g in lost)}).")
        if keep:
            panel[cname] = keep
            disc[cname] = [g for g in (caller_disc.get(cname) or keep) if g not in owner] or keep
    info = {"name": ref_name, "species": species, "auto_applied": auto, "types": list(types),
            "caller_types_mapped": mapped, "genes_dropped": dropped,
            "expected": ref.get("expected", []), "usually": ref.get("usually", []),
            "source": ref.get("source", "")}
    return panel, disc, info, warnings


def _canonical_check(sc: Any, adata: Any, cluster_key: str, labels: dict[str, dict[str, Any]],
                     disc: dict[str, list[str]], use_raw: bool, warnings: list[str]) -> list[str]:
    """Withdraw a label none of whose lineage-specific markers is enriched in its cluster.

    Enriched = the cluster's mean (log-normalised) exceeds the mean over cells of clusters given a
    DIFFERENT label by ``_CANON_MIN_DELTA``, with the gene detected in ``_CANON_MIN_FRACTION`` of
    the cluster's cells. Comparing against other LINEAGES, not all other cells, keeps a dominant
    lineage testable (in a bipolar-rich sample most "other cells" are bipolar too). The raw check
    asks which lineage's markers are highest; this asks whether the winner's markers are there at
    all — a cluster called ganglion on CHL1=1.30 with RBPMS=0.06 passes the first, not this one."""
    genes = sorted({g for gs in disc.values() for g in gs})
    if not genes:
        return []
    df = sc.get.obs_df(adata, keys=genes, use_raw=use_raw)
    clusters = adata.obs[cluster_key].astype(str)
    lab_of_cell = clusters.map(lambda c: labels[c]["cell_type"])
    failed: list[str] = []
    for cl, info in labels.items():
        lab = info["cell_type"]
        markers = [g for g in disc.get(lab, []) if g in df.columns]
        if lab == "Unassigned" or not markers:
            continue
        inside = (clusters == cl).to_numpy()
        outside = (lab_of_cell != lab).to_numpy()
        if not outside.any():
            continue                    # every cluster has this label: nothing to compare against
        delta = df.loc[inside, markers].mean() - df.loc[outside, markers].mean()
        frac = (df.loc[inside, markers] > 0).mean()
        ok = [g for g in markers if delta[g] >= _CANON_MIN_DELTA and frac[g] >= _CANON_MIN_FRACTION]
        if ok:
            info["canonical_markers_enriched"] = ok
            continue
        failed.append(cl)
        shown = ", ".join(f"{g} {delta[g]:+.2f} in {frac[g]:.0%}" for g in markers)
        info.update({"cell_type": "Unassigned", "confidence": "none",
                     "withdrawn_label": lab,
                     "evidence": f"{lab} withdrawn: none of its markers is enriched ({shown})"})
    if failed:
        warnings.append(f"{len(failed)} cluster(s) lost their label because none of that lineage's "
                        f"specific markers is enriched in them: {', '.join(sorted(failed, key=_ckey))}. "
                        "See withdrawn_label / evidence in cluster_cell_types.csv.")
    return sorted(failed, key=_ckey)


def _plausibility(sc: Any, adata: Any, labels: dict[str, dict[str, Any]], ref_info: dict[str, Any],
                  disc: dict[str, list[str]], use_raw: bool, warnings: list[str],
                  notes: list[str]) -> None:
    """Warn when a lineage every sample of this tissue contains got no cluster at all, and note
    (with how often its markers are detected) each usual lineage that is absent."""
    found = {v["cell_type"] for v in labels.values()}
    names = list(adata.raw.var_names if use_raw else adata.var_names)
    for lineage in ref_info.get("expected", []):
        if lineage in found:
            continue
        markers = [g for g in disc.get(lineage, []) if g in names]
        detail = ""
        if markers:
            frac = (sc.get.obs_df(adata, keys=markers, use_raw=use_raw) > 0).mean()
            detail = " (" + ", ".join(f"{g} detected in {frac[g]:.0%} of cells" for g in markers) + ")"
        warnings.append(f"No cluster was labelled {lineage}, which every {ref_info['name']} sample "
                        f"contains{detail}. Check the Unassigned clusters before reporting the "
                        "composition.")
    usual = [lin for lin in ref_info.get("usually", []) if lin not in found]
    if usual and len(usual) == len(ref_info.get("usually", [])):
        warnings.append(f"None of the usual {ref_info['name']} lineages ({', '.join(usual)}) was found: "
                        "either the sample was sorted/depleted, or the labelling failed.")
    for lineage in usual:
        markers = [g for g in disc.get(lineage, []) if g in names]
        if markers:
            frac = (sc.get.obs_df(adata, keys=markers, use_raw=use_raw) > 0).mean()
            seen = ", ".join(f"{g} detected in {frac[g]:.1%} of cells" for g in markers)
            notes.append(f"No {lineage} cluster: {seen}. " + (
                "Detection this low fits a sample depleted of this lineage; say so in the report."
                if float(frac.median()) < 0.05 else
                "Its markers ARE detected, so it may sit inside an Unassigned or mislabelled "
                "cluster: check before reporting the composition."))


def _ckey(cluster: str) -> "tuple[int, str]":
    return (int(cluster), "") if str(cluster).isdigit() else (10**9, str(cluster))


def run_marker_annotation(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Assign a cell type per cluster: signature score first pass, RAW expression decides.

    Previously a ``run_code`` template, i.e. code the model rewrote every run — the single most
    consequential step in the pipeline, and the least reproducible. As a tool the procedure is
    fixed and the judgement stays with the panel, which is where it belongs.

    Per-cell-type z-scoring inflates weak and ambient signal into confident-looking maxima, so
    the z-argmax is a FIRST PASS. The label is assigned only when that lineage's own
    discriminators are the dominant raw signal by ``dominance_ratio``; where the raw check
    disagrees with the first pass BOTH are reported; and a cluster with no dominant coherent
    signal stays ``Unassigned`` instead of being pushed into the nearest label.
    """
    try:
        sc = _import_scanpy()
        import numpy as np
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt, err = _step_input(ctx, args, (work / "adata_de.h5ad", work / "adata_clustered.h5ad"),
                            "run_clustering (and ideally run_de) must run first")
    if err:
        return {"status": "error", "step": "annotation", "error": err["error"]}

    if not isinstance(args.get("panel"), dict) and str(args.get("reference") or "auto").lower() in (
            "auto", "none", ""):
        return {"status": "error", "step": "annotation",
                "error": ("Give `reference` (a curated panel: " + ", ".join(available_references())
                          + ") or a `panel` {cell type: [marker symbols]} for THIS tissue. There is "
                          "no safe default — a panel from another tissue produces confident wrong "
                          "labels.")}
    cluster_key = str(args.get("cluster_key", "leiden"))
    min_mean = float(args.get("min_discriminator_mean", 0.20))
    ratio = float(args.get("dominance_ratio", 1.5))

    adata = sc.read_h5ad(ckpt)
    if cluster_key not in adata.obs:
        return {"status": "error", "step": "annotation",
                "error": f"cluster_key '{cluster_key}' not in obs; available: {list(adata.obs.columns)}"}

    use_raw = adata.raw is not None
    present = set(adata.raw.var_names if use_raw else adata.var_names)
    try:
        panel, discriminators, ref_info, warnings = _resolve_panel(args, present)
    except ValueError as exc:
        return {"status": "error", "step": "annotation", "error": str(exc)}
    requested = dict(panel)
    panel = {ct: [g for g in gs if g in present] for ct, gs in panel.items()}
    absent = {ct: sorted(set(gs) - present) for ct, gs in requested.items() if set(gs) - present}
    # A curated panel lists alternatives (SEPT4/SEPTIN4 across annotation releases), so a reference
    # gene missing from this object is expected and reported apart from the caller's own genes.
    ref_types = set(ref_info.get("types", []))
    reference_absent = {ct: gs for ct, gs in absent.items() if ct in ref_types}
    absent = {ct: gs for ct, gs in absent.items() if ct not in ref_types}
    not_testable = sorted(ct for ct, gs in panel.items() if not gs)
    panel = {ct: gs for ct, gs in panel.items() if gs}
    if not panel:
        return {"status": "error", "step": "annotation",
                "error": "no panel gene is present in the object — check the symbol nomenclature "
                         "(human HGNC vs mouse MGI) before anything else."}
    # No discriminators given: fall back to the panel itself, and SAY so — the disambiguation
    # is weaker without lineage-specific genes, and that changes how the labels should be read.
    disc = {ct: [g for g in (discriminators.get(ct) or panel[ct]) if g in present]
            for ct in panel}
    disc_note = "" if discriminators else (
        "no `discriminators` given, so the full panel was used for the raw check. Shared "
        "markers therefore discriminate less well; supply lineage-specific genes per type.")

    for ct, gs in panel.items():
        try:
            sc.tl.score_genes(adata, gs, score_name=f"score_{ct}", use_raw=use_raw)
        except RuntimeError as exc:
            # "No control genes found in any cut": a signature in an expression bin no other gene
            # shares (sparse data, few genes). Scoring against the whole bin pool still works.
            if "control genes" not in str(exc):
                raise
            try:
                sc.tl.score_genes(adata, gs, score_name=f"score_{ct}", use_raw=use_raw,
                                  ctrl_as_ref=False)
            except (RuntimeError, TypeError):
                # Still no control pool: the plain mean of the signature is the first-pass score.
                # The label itself is settled by the raw and canonical checks below either way.
                adata.obs[f"score_{ct}"] = sc.get.obs_df(adata, keys=gs, use_raw=use_raw).mean(axis=1)
    cols = [f"score_{ct}" for ct in panel]
    scores = adata.obs.groupby(cluster_key, observed=True)[cols].mean()
    scores.columns = [c[len("score_"):] for c in scores.columns]
    scores.to_csv(tables / "celltype_scores_by_cluster.csv")

    # A cell type whose score is constant across clusters (std 0, or a single cluster) carries
    # no information about which cluster is which; -inf keeps it out of the argmax instead of
    # leaving a NaN row, which pandas warns about and will eventually raise on.
    z = ((scores - scores.mean()) / scores.std().replace(0, np.nan)).fillna(float("-inf"))
    first_pass = z.idxmax(axis=1)

    disc_genes = sorted({g for gs in disc.values() for g in gs})
    raw_means = sc.get.obs_df(adata, keys=[*disc_genes, cluster_key], use_raw=use_raw) \
                  .groupby(cluster_key, observed=True)[disc_genes].mean()

    labels: dict[str, dict[str, Any]] = {}
    for cl in [str(c) for c in scores.index]:
        per_lineage = {ct: float(raw_means.loc[cl, gs].mean()) for ct, gs in disc.items() if gs}
        ranked = sorted(per_lineage.items(), key=lambda kv: kv[1], reverse=True)
        best, best_val = ranked[0] if ranked else ("", 0.0)
        runner, runner_val = ranked[1] if len(ranked) > 1 else ("", 0.0)
        dominant = best_val >= min_mean and (runner_val <= 0 or best_val >= ratio * runner_val)
        fp = str(first_pass.get(cl, "")) if cl in first_pass.index else ""
        labels[cl] = {
            "cell_type": best if dominant else "Unassigned",
            "first_pass_label": fp,
            "corrected_by_raw_check": bool(dominant and fp and fp != best),
            "n_cells": int((adata.obs[cluster_key].astype(str) == cl).sum()),
            "confidence": ("high" if dominant and best_val >= 2 * min_mean
                           else "medium" if dominant else "none"),
            "discriminator_mean": round(best_val, 4),
            "runner_up": runner, "runner_up_mean": round(runner_val, 4),
            "evidence": ", ".join(f"{g}={raw_means.loc[cl, g]:.2f}"
                                  for g in disc.get(best, [])[:5]) if dominant
                        else "no dominant lineage signal",
        }

    canonical_failed = _canonical_check(sc, adata, cluster_key, labels, disc, use_raw, warnings)
    composition_notes: list[str] = []
    if ref_info:
        _plausibility(sc, adata, labels, ref_info, disc, use_raw, warnings, composition_notes)

    adata.obs["cell_type"] = adata.obs[cluster_key].map(
        lambda c: labels[str(c)]["cell_type"]).astype("category")
    adata.obs["cluster_note"] = adata.obs[cluster_key].map(lambda c: labels[str(c)]["evidence"])
    # h5py rejects '/' in obs keys (score_Pericyte/SMC) — sanitize before writing.
    adata.obs.columns = [c.replace("/", "_") for c in adata.obs.columns]
    adata.write(work / "adata_annotated.h5ad")

    comp = adata.obs["cell_type"].value_counts()
    total = int(comp.sum()) or 1
    _write_table(tables / "celltype_composition.csv",
                 [{"cell_type": str(k), "n_cells": int(v), "pct": round(100 * int(v) / total, 2)}
                  for k, v in comp.items()], ["cell_type", "n_cells", "pct"])
    _write_table(tables / "cluster_cell_types.csv",
                 [{"cluster": c, **{k: v for k, v in d.items()}} for c, d in sorted(labels.items())],
                 ["cluster", "cell_type", "first_pass_label", "corrected_by_raw_check",
                  "n_cells", "confidence", "discriminator_mean", "runner_up", "runner_up_mean",
                  "evidence", "canonical_markers_enriched", "withdrawn_label"])
    (tables / "cluster_cell_types.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")

    corrected = sorted(c for c, v in labels.items() if v["corrected_by_raw_check"])
    unassigned = sorted(c for c, v in labels.items() if v["cell_type"] == "Unassigned")
    return {
        "status": "ok",
        "step": "annotation",
        "cluster_key": cluster_key,
        "n_clusters": len(labels),
        "labels": {c: v["cell_type"] for c, v in sorted(labels.items())},
        # These three belong in the write-up. An annotation whose corrections and unassigned
        # clusters are invisible cannot be reviewed by anyone.
        "corrected_by_raw_check": corrected,
        "unassigned_clusters": unassigned,
        "lineages_not_testable": not_testable,
        "panel_genes_absent_from_object": absent,
        # Labels withdrawn because none of their lineage-specific markers is enriched in the
        # cluster: what the raw check alone let through on 2026-10-02.
        "canonical_check_failed": canonical_failed,
        "composition_notes": composition_notes,
        "reference": ({k: v for k, v in ref_info.items() if k not in ("types",)}
                      | {"genes_absent_from_object": reference_absent}) if ref_info else {},
        "warnings": warnings,
        "thresholds": {"min_discriminator_mean": min_mean, "dominance_ratio": ratio,
                       "canonical_min_delta": _CANON_MIN_DELTA,
                       "canonical_min_fraction": _CANON_MIN_FRACTION},
        "note": disc_note,
        "read_from": _run_rel(ctx, ckpt),
        "checkpoint": "adata_annotated.h5ad",
        "tables": [_rel(art, tables / "cluster_cell_types.csv"),
                   _rel(art, tables / "celltype_composition.csv"),
                   _rel(art, tables / "celltype_scores_by_cluster.csv")],
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_marker_annotation`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        "run_marker_annotation",
        "Assign a cell type to each cluster from marker genes. For a tissue with a curated "
        "reference (" + ", ".join(available_references()) + ") pass `reference` and NO panel: "
        "the curated markers are used, in the object's species. Otherwise give a `panel` "
        "({cell type: [symbols]}) with `discriminators` ({cell type: [2-4 lineage-SPECIFIC "
        "symbols]}); a panel whose types match a reference uses the reference automatically. "
        "Signature scores give a first-pass z-argmax; the final label comes from RAW marker "
        "expression, assigned only when that lineage's discriminators dominate AND at least "
        "one of them is enriched in the cluster versus other lineages; otherwise 'Unassigned'. "
        "Returns which clusters the raw check CORRECTED, which are unassigned or WITHDRAWN, "
        "and warnings (e.g. a lineage every sample of the tissue contains is missing); all "
        "belong in the report.",
        {"type": "object", "properties": {
            "reference": {"type": "string", "enum": ["auto", "none", *available_references()],
                          "description": ("curated marker reference for the tissue; 'auto' (default) "
                                          "applies one when your panel's cell types match it")},
            "panel": {"type": "object"}, "discriminators": {"type": "object"},
            "cluster_key": {"type": "string"},
            "min_discriminator_mean": {"type": "number"},
            "dominance_ratio": {"type": "number"},
            "input": _INPUT_SPEC}},
        run_marker_annotation,
        reads_private_data=True, category="analysis", requires=("scanpy",),
    )
