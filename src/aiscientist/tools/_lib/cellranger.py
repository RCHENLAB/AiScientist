"""Recognise 10x Cell Ranger deliveries in a folder, from a plain listing of its paths.

One function, used in two places that must agree: the gateway, which lists an uploaded folder
(on dfs3b, with ``find``) and tells the planner what the data is, and ``run_cellqc``, which stages the
same libraries for CellQC. Standard library only at import — it runs in the gateway and inside job
images; :func:`describe_10x_h5`, which reads a matrix file, imports h5py and numpy when called.

A LIBRARY is a directory holding both a raw and a filtered feature-barcode matrix, in any of the
forms Cell Ranger and 10x Cloud deliver them: ``<m>.h5``, an unpacked ``<m>/`` directory, or a
``<m>.tar.gz`` archive of the bare files (10x Cloud). Its sample id is the directory's name, or its
parent's when the directory is the usual ``outs/``. A run that binds several folders describes each
and merges them (:func:`merge_cellranger_layouts`): a library that is its folder is then named after
the dataset, not the directory.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
import io
from collections import Counter
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

RAW = "raw_feature_bc_matrix"
FILTERED = "filtered_feature_bc_matrix"
BAM = "possorted_genome_bam.bam"
# What a listing must include for :func:`describe_cellranger_layout`: `find` with these name
# patterns, at depth <= 4 (root/<sample>/outs/<file>), is enough.
FIND_NAMES = (f"{RAW}*", f"{FILTERED}*", f"{BAM}*", "metrics_summary.csv", "analysis",
              "analysis.tar.gz")
FIND_MAXDEPTH = 4


def _forms(names: "set[str]", stem: str) -> "list[str]":
    return [f for f, n in (("h5", f"{stem}.h5"), ("dir", stem), ("tar.gz", f"{stem}.tar.gz"))
            if n in names]


def describe_cellranger_layout(paths: Iterable[str], root: str) -> "dict[str, Any] | None":
    """The Cell Ranger libraries under ``root``, from ``paths`` (absolute or root-relative, files and
    directories). None when there is no library at all.

    Returns ``{"root", "n_libraries", "libraries": [{"sample", "dir", "rel", "raw", "filtered",
    "bam", "clusters", "metrics", "packed", "mtx_dirs"}], "all_have_bam", "any_packed"}`` where
    ``raw``/``filtered`` list the forms present (``h5``, ``dir``, ``tar.gz``), ``clusters`` says
    whether Cell Ranger's clustering is there (SoupX estimates ambient RNA from it), ``packed``
    whether a matrix directory is only present as a ``.tar.gz``, and ``mtx_dirs`` whether both
    matrix directories are available (unpacked or packed)."""
    root_p = PurePosixPath(root.rstrip("/") or "/")
    by_dir: dict[PurePosixPath, set[str]] = {}
    for raw_path in paths:
        p = PurePosixPath(str(raw_path).rstrip("/"))
        if not p.is_absolute():
            p = root_p / p
        by_dir.setdefault(p.parent, set()).add(p.name)
    libraries = []
    for d in sorted(by_dir):
        names = by_dir[d]
        raw, filtered = _forms(names, RAW), _forms(names, FILTERED)
        if not (raw and filtered):
            continue
        sample = d.parent.name if d.name == "outs" and d.parent != d else d.name
        rel = str(d.relative_to(root_p)) if d.is_relative_to(root_p) else str(d)
        libraries.append({
            "sample": sample or root_p.name,
            "dir": str(d),
            "rel": rel,
            "raw": raw,
            "filtered": filtered,
            "bam": BAM in names and f"{BAM}.bai" in names,
            "clusters": "analysis" in names or "analysis.tar.gz" in names,
            "metrics": "metrics_summary.csv" in names,
            # A matrix directory present only as a .tar.gz: it is unpacked before CellQC runs.
            "packed": any("dir" not in forms and "tar.gz" in forms for forms in (raw, filtered)),
            # SoupX reads the matrix DIRECTORIES; without them ambient correction falls back.
            "mtx_dirs": all(("dir" in forms or "tar.gz" in forms) for forms in (raw, filtered)),
        })
    if not libraries:
        return None
    return {
        "root": str(root_p),
        "n_libraries": len(libraries),
        "libraries": libraries,
        "all_have_bam": all(lib["bam"] for lib in libraries),
        "any_packed": any(lib["packed"] for lib in libraries),
    }


def sample_label(name: str) -> str:
    """A sample id made from a dataset's name as the user sees it (``Sample1_WT/``): the characters a
    file name and CellQC's sample sheet take, everything else as ``_``."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip().rstrip("/")).strip("._-")
    return label or "sample"


def merge_cellranger_layouts(parts: "Iterable[tuple[str, dict[str, Any] | None]]") -> "dict[str, Any] | None":
    """ONE layout for the Cell Ranger libraries of several bound folders.

    ``parts`` pairs each folder's dataset name with that folder's own
    :func:`describe_cellranger_layout` (None for a folder without a library). A library that IS its
    folder, or that folder's ``outs/``, is named after the dataset: a dataset kept on the server
    lives in ``…/datasets/<uuid>/original``, so the directory's name says nothing (both of two such
    folders were "original"). A library in a subfolder keeps that subfolder's name, and a name two
    libraries would share gets ``_2``, ``_3``. Each library records the ``dataset`` it came from, and
    ``datasets`` lists the folders that hold one. The gateway and ``run_cellqc`` both build their
    layout here, so the sample ids the planner reads are the ones CellQC runs under. A part without a
    name keeps the directory's names, as a single folder always did. None when no folder holds a
    library."""
    libraries: list[dict[str, Any]] = []
    roots: list[str] = []
    datasets: list[str] = []
    seen: set[str] = set()
    for name, layout in parts:
        if not layout:
            continue
        roots.append(layout["root"])
        datasets.append(name)
        for lib in layout["libraries"]:
            own = lib["rel"] in (".", "outs")
            base = sample_label(name) if (name and own) else lib["sample"]
            label, k = base, 1
            while label in seen:
                k += 1
                label = f"{base}_{k}"
            seen.add(label)
            libraries.append({**lib, "sample": label, "dataset": name})
    if not libraries:
        return None
    return {
        "root": roots[0],
        "roots": roots,
        "datasets": datasets,
        "n_libraries": len(libraries),
        "libraries": libraries,
        "all_have_bam": all(lib["bam"] for lib in libraries),
        "any_packed": any(lib["packed"] for lib in libraries),
    }


# --- what the matrices hold ------------------------------------------------------------------------
#
# Read at upload so that neither the planner nor the Scientist has to find out by hand. Run
# f3b8268c4fd4 planned a read-only "audit the count matrices" step for a Cell Ranger delivery and spent
# 14 minutes and 11 `run_code` calls on it (6 failed: the v2 layout guessed for a v3 file, the matrix
# orientation mixed up twice, a features group asserted absent) without producing the ruling, which
# the file answers in a second: Cell Ranger writes integer UMI counts and its own summary of them.

# Count statistics read every stored value; above this many the profile keeps the header facts only.
MAX_PROFILE_ENTRIES = 250_000_000
_CHUNK_ENTRIES = 16_000_000
_GEX = "Gene Expression"
# The species a Cell Ranger reference name implies (GRCh38, GRCm39-2024-A, mm10, ...). Stated in the
# profile because run f3b8268c4fd4's file description called a GRCm39 delivery "GRCh38" — both are
# six bytes, and the description had seen the dtype, not the value — and the plan built a human panel.
_GENOME_SPECIES = (("grch", "human"), ("hg19", "human"), ("hg38", "human"),
                   ("grcm", "mouse"), ("mm10", "mouse"), ("mm39", "mouse"))
# The matrix facts Cell Ranger also reports: (fact, metrics_summary.csv column, words, tolerance).
_METRIC_CHECKS = (("n_barcodes", "Estimated Number of Cells", "cells", 0.0),
                  # Cell Ranger prints whole numbers; a median over an even count can end in .5.
                  ("median_umis_per_barcode", "Median UMI Counts per Cell", "median UMIs per cell", 1.0),
                  ("median_genes_per_barcode", "Median Genes per Cell", "median genes per cell", 1.0))


def genome_species(genome: str) -> str:
    """``human`` / ``mouse`` for a Cell Ranger reference name, else ''."""
    low = (genome or "").lower()
    return next((species for key, species in _GENOME_SPECIES if key in low), "")


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def describe_10x_h5(path: str, *, max_entries: int = MAX_PROFILE_ENTRIES) -> "dict[str, Any] | None":
    """What a 10x Cell Ranger feature-barcode matrix ``.h5`` holds, read from the file itself. None
    when it is not one (an ``.h5ad``, ``molecule_info.h5``), h5py is missing, or it will not open.

    Both layouts are read: Cell Ranger >= 3 (``/matrix`` with a ``features/`` group) and 2 (one group
    per genome holding ``genes``). Either way the matrix is CSC with ONE COLUMN PER BARCODE and
    ``shape`` = [n_features, n_barcodes]. Returns ``{"layout", "n_barcodes", "n_features",
    "genomes": {name: n_features}, "feature_types": {type: n}, "dtype", "nnz", "chemistry",
    "library_ids", "software_version", "counts"}``; ``counts`` (None above ``max_entries`` stored
    values) is ``{"integer", "non_negative", "total_umis", "median_umis_per_barcode",
    "median_genes_per_barcode", "empty_barcodes"}``, with UMIs and genes counted over the Gene
    Expression features only, as Cell Ranger's ``metrics_summary.csv`` counts them. Reads the values
    in chunks of whole barcodes, so memory stays bounded; ~1-3 s for a 40M-value filtered matrix."""
    try:
        import h5py
        import numpy as np
    except ImportError:
        return None
    try:
        with h5py.File(path, "r") as f:
            return _describe_open_10x(f, h5py, np, max_entries)
    except Exception:  # noqa: BLE001 - not HDF5, truncated, unreadable: no facts, never a failure
        return None


def _describe_open_10x(f: Any, h5py: Any, np: Any, max_entries: int) -> "dict[str, Any] | None":
    g = f.get("matrix")
    if isinstance(g, h5py.Group) and isinstance(g.get("features"), h5py.Group):
        layout, feats = "v3", g["features"]
        types = [_text(x) for x in feats["feature_type"][:]] if "feature_type" in feats else []
        genomes = Counter(_text(x) for x in feats["genome"][:]) if "genome" in feats else Counter()
    else:
        groups = [k for k in f if isinstance(f[k], h5py.Group) and "genes" in f[k]]
        if not groups:
            return None
        layout, g, types = "v2", f[groups[0]], []
        genomes = Counter({k: int(f[k]["shape"][0]) for k in groups})
    if not all(k in g for k in ("data", "indices", "indptr", "shape")):
        return None
    n_features, n_barcodes = (int(x) for x in g["shape"][:2])
    attrs, data = f.attrs, g["data"]
    facts: dict[str, Any] = {
        "layout": layout, "n_barcodes": n_barcodes, "n_features": n_features,
        "genomes": dict(genomes), "feature_types": dict(Counter(types)) or {_GEX: n_features},
        "dtype": str(data.dtype), "nnz": int(data.shape[0]),
        "chemistry": _text(attrs["chemistry_description"]) if "chemistry_description" in attrs else "",
        "library_ids": [_text(x) for x in attrs["library_ids"]] if "library_ids" in attrs else [],
        "software_version": _text(attrs["software_version"]) if "software_version" in attrs else "",
        "counts": None,
    }
    if data.shape[0] <= max_entries:
        gex = np.array([t == _GEX for t in types]) if types and set(types) != {_GEX} else None
        names_ds = feats.get("name") if layout == "v3" else g.get("gene_names")
        names = [_text(x) for x in names_ds[:]] if names_ds is not None else []
        facts["counts"] = _count_stats(g, n_barcodes, gex, np, names)
    return facts


def _count_stats(g: Any, n_barcodes: int, gex: Any, np: Any,
                 names: "list[str] | None" = None) -> "dict[str, Any]":
    """Per-barcode UMI and gene counts over a CSC matrix (``gex``: a feature mask, or None for all),
    and the most abundant features by total UMIs (``top_genes``, when ``names`` are given)."""
    data, indptr = g["data"], g["indptr"][:].astype(np.int64)
    n_features = len(names) if names else 0
    feature_totals = np.zeros(n_features, dtype=np.float64) if n_features else None
    integer_dtype = data.dtype.kind in "iu"
    acc = np.int64 if integer_dtype else np.float64
    umis = np.zeros(n_barcodes, dtype=acc)
    genes = np.diff(indptr) if gex is None else np.zeros(n_barcodes, dtype=np.int64)
    negative, fractional, b0 = False, 0, 0
    while b0 < n_barcodes:
        b1 = int(np.searchsorted(indptr, indptr[b0] + _CHUNK_ENTRIES, side="right")) - 1
        b1 = min(max(b1, b0 + 1), n_barcodes)
        lo, hi = int(indptr[b0]), int(indptr[b1])
        if hi > lo:
            vals = data[lo:hi]
            negative = negative or bool(vals.min() < 0)
            if not integer_dtype:
                fractional += int(np.count_nonzero(vals != np.floor(vals)))
            starts, ends = indptr[b0:b1] - lo, indptr[b0 + 1:b1 + 1] - lo
            if gex is not None:
                keep = gex[g["indices"][lo:hi]]
                vals = np.where(keep, vals, 0)
                kept = np.concatenate([[0], np.cumsum(keep & (vals != 0), dtype=np.int64)])
                genes[b0:b1] = kept[ends] - kept[starts]
            cum = np.concatenate([[0], np.cumsum(vals, dtype=acc)])
            umis[b0:b1] = cum[ends] - cum[starts]
            if feature_totals is not None:
                idx = g["indices"][lo:hi]
                feature_totals += np.bincount(idx, weights=vals, minlength=n_features)[:n_features]
        b0 = b1
    total = umis.sum()
    top: list[str] = []
    if feature_totals is not None:
        order = np.argsort(-feature_totals)[:50]
        top = [names[i] for i in order if feature_totals[i] > 0]
    return {
        "integer": integer_dtype or fractional == 0,
        "non_negative": not negative,
        "total_umis": int(total) if integer_dtype else float(total),
        "median_umis_per_barcode": float(np.median(umis)) if n_barcodes else 0.0,
        "median_genes_per_barcode": float(np.median(genes)) if n_barcodes else 0.0,
        "empty_barcodes": int(np.count_nonzero(umis == 0)),
        "top_genes": top,
    }


def parse_metrics_summary(text: str) -> "dict[str, float]":
    """Cell Ranger's ``metrics_summary.csv`` (one header row, one row of values such as ``"18,681"``
    and ``97.8%``) as numbers, a percentage as a fraction. {} when the text is not that file."""
    rows = [row for row in csv.reader(io.StringIO(text or "")) if row]
    if len(rows) < 2:
        return {}
    out: dict[str, float] = {}
    for key, raw in zip(rows[0], rows[1]):
        value = raw.strip().replace(",", "")
        try:
            out[key.strip()] = float(value[:-1]) / 100 if value.endswith("%") else float(value)
        except ValueError:
            continue
    return out


def check_against_metrics(matrix: "dict[str, Any]",
                          metrics: "dict[str, float]") -> "dict[str, Any] | None":
    """Does a FILTERED matrix reproduce its library's ``metrics_summary.csv``? ``{"agrees",
    "checks": [{"metric", "matrix", "metrics_summary", "match"}]}``, or None when nothing compares
    (no counts read, or a multi-genome summary whose columns are per genome)."""
    values = {"n_barcodes": matrix.get("n_barcodes"), **(matrix.get("counts") or {})}
    checks = []
    for key, column, words, tolerance in _METRIC_CHECKS:
        mine, theirs = values.get(key), metrics.get(column)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            checks.append({"metric": words, "matrix": mine, "metrics_summary": theirs,
                           "match": abs(mine - theirs) <= tolerance})
    return {"agrees": all(c["match"] for c in checks), "checks": checks} if checks else None


def profile_cellranger_libraries(layout: "dict[str, Any] | None", *,
                                 local_path: "Callable[[str], str | None]",
                                 read_text: "Callable[[str], str | None]",
                                 max_libraries: int = 8) -> None:
    """Add to each library of ``layout``, in place, what its filtered matrix holds (``matrix``: see
    :func:`describe_10x_h5`), the ``metrics_summary.csv`` numbers it can be checked against, and the
    check (``metrics_check``). ``local_path`` maps a file of the delivery to a path readable here
    (None when it is not — a dfs3b library whose matrix was not staged); ``read_text`` returns a small
    text file's content (None on failure). Best effort: a library that cannot be read keeps no facts,
    and nothing raises."""
    for lib in (layout or {}).get("libraries", [])[:max_libraries]:
        try:
            matrix = None
            if "h5" in lib.get("filtered", []):
                path = local_path(f"{lib['dir']}/{FILTERED}.h5")
                matrix = describe_10x_h5(path) if path else None
            if matrix:
                lib["matrix"] = matrix
            metrics = (parse_metrics_summary(read_text(f"{lib['dir']}/metrics_summary.csv") or "")
                       if lib.get("metrics") else {})
            if metrics:
                lib["metrics_summary"] = {col: metrics[col] for _, col, _, _ in _METRIC_CHECKS
                                          if col in metrics}
            check = check_against_metrics(matrix, metrics) if matrix and metrics else None
            if check:
                lib["metrics_check"] = check
        except Exception:  # noqa: BLE001 - the facts are a hint; a library without them is still a library
            continue


def matrix_facts(matrix: "dict[str, Any]") -> str:
    """What :func:`describe_10x_h5` found, in one clause."""
    types = matrix.get("feature_types") or {}
    kinds = ("all Gene Expression" if set(types) == {_GEX} else
             ", ".join(f"{n:,} {t}" for t, n in types.items()))
    genomes = ", ".join(g + (f" = {genome_species(g)}" if genome_species(g) else "")
                        for g in matrix.get("genomes") or {})
    text = (f"{matrix['n_barcodes']:,} barcodes x {matrix['n_features']:,} features ({kinds}"
            + (f"; genome {genomes}" if genomes else "") + ")")
    counts = matrix.get("counts")
    if not counts:
        return text + f", values stored as {matrix['dtype']}"
    kind = ("integer UMI counts" if counts["integer"] else "NON-INTEGER values") + (
        "" if counts["non_negative"] else ", some NEGATIVE")
    return (text + f", {matrix['dtype']} {kind}: {counts['total_umis']:,.0f} UMIs, median "
            f"{counts['median_umis_per_barcode']:,.0f} UMIs and "
            f"{counts['median_genes_per_barcode']:,.0f} genes per barcode")


def is_count_matrix(matrix: "dict[str, Any]") -> bool:
    """Integer and non-negative, as Cell Ranger writes it (from the dtype when no values were read)."""
    counts = matrix.get("counts")
    if counts:
        return bool(counts["integer"] and counts["non_negative"])
    return str(matrix.get("dtype", "")).startswith(("int", "uint"))


def species_by_library(libs: "list[dict[str, Any]]") -> "dict[str, list[str]]":
    """``{species: [sample, ...]}`` from the genomes of the libraries' read matrices, in order."""
    out: dict[str, list[str]] = {}
    for lib in libs:
        for sp in dict.fromkeys(genome_species(g) for g in (lib.get("matrix") or {}).get("genomes", {})):
            if sp:
                out.setdefault(sp, []).append(lib["sample"])
    return out


def _species(libs: "list[dict[str, Any]]") -> str:
    found = {genome_species(g) for lib in libs for g in (lib.get("matrix") or {}).get("genomes", {})}
    return found.pop() if len(found) == 1 and "" not in found else ""


# The curated marker references live with the annotation tool; read as data, never imported.
_MARKER_REFERENCES = Path(__file__).resolve().parents[1] / "run_marker_annotation" / "references"


def tissue_hint(top_genes: "list[str]") -> str:
    """Which curated tissue the most abundant genes point to, as a planner-facing sentence ('' when
    none does). A delivery carries no tissue label, so on 2026-10-08 the PI guessed: one rerun of
    f3b8268c4fd4's analysis refused the retina reference as "a fabrication" and wrote its own panel
    (rods came out cones), the next planned "a general mouse hematopoietic/immune panel" for a retina. Abundance is a hint, not a label;
    three or more of a reference's marker genes among the 50 most abundant is enough to say which."""
    best: "tuple[str, dict[str, str]] | None" = None
    wanted = set(top_genes)
    for path in sorted(_MARKER_REFERENCES.glob("*.json")):
        try:
            ref = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hits: dict[str, str] = {}
        for types in (ref.get("species") or {}).values():
            for lineage, spec in types.items():
                for gene in list(spec.get("discriminators", [])) + list(spec.get("panel", [])):
                    if gene in wanted:
                        hits.setdefault(gene, lineage)
        if len(hits) >= 3 and (best is None or len(hits) > len(best[1])):
            best = (path.stem, hits)
    if best is None:
        return ""
    name, hits = best
    order = [g for g in top_genes if g in hits]
    shown = ", ".join(f"{g} ({hits[g]})" for g in order[:6])
    return (f"TISSUE HINT (from gene abundance — no tissue label came with the data): the 50 most "
            f"abundant genes include {shown}, markers in the curated {name} reference, so the tissue "
            f"is most likely {name}. A step that annotates or validates cell types passes "
            f"`reference: \"{name}\"` to `run_marker_annotation` unless the marker genes say "
            "otherwise, and the report says that the tissue was inferred. ")


def _provenance(libs: "list[dict[str, Any]]") -> str:
    """The facts read from the libraries' filtered matrices and the ruling they support."""
    read = [lib for lib in libs if isinstance(lib.get("matrix"), dict)]
    altered = [lib for lib in read if not is_count_matrix(lib["matrix"])]
    if altered:
        return ("⚠ PROVENANCE: " + "; ".join(
            f"{lib['sample']}'s filtered matrix holds {matrix_facts(lib['matrix'])}" for lib in altered)
            + ". Cell Ranger writes integer UMI counts and nothing else into these files, so they "
            "were changed after Cell Ranger: establish what they hold before any count-based step.")
    facts = []
    for lib in read[:4]:
        line = f"{lib['sample']}: {matrix_facts(lib['matrix'])}"
        check = lib.get("metrics_check")
        if check and check["agrees"]:
            line += (" — it reproduces its metrics_summary.csv ("
                     + ", ".join(f"{c['metrics_summary']:,.0f} {c['metric']}" for c in check["checks"])
                     + ")")
        elif check:
            line += (" — it does NOT reproduce its metrics_summary.csv ("
                     + "; ".join(f"{c['metric']}: matrix {c['matrix']:,.0f}, summary "
                                 f"{c['metrics_summary']:,.0f}" for c in check["checks"]
                                 if not c["match"])
                     + "), so the summary may come from another Cell Ranger run: say so in the report")
        facts.append(line)
    text = ""
    if facts:
        more = f" (and {len(read) - 4} more)" if len(read) > 4 else ""
        text = "Filtered matrices, read at upload — " + "; ".join(facts) + more + ". "
    species = _species(read)
    if species:
        text += (f"The reference genome makes the species {species.upper()}: plan {species} gene "
                 f"symbols and {species} marker panels. ")
    by_species = species_by_library(read)
    if len(by_species) > 1:
        first = next(iter(by_species))
        text += ("The libraries are from DIFFERENT SPECIES ("
                 + "; ".join(f"{sp}: {', '.join(names)}" for sp, names in by_species.items())
                 + "). `run_cellqc` QCs each species in its own CellQC run, with its own gene sets, "
                 f"in the same step. Later steps read the {first} libraries' checkpoint only (the "
                 "others' QC'd data is kept beside it): plan them for one species, with that "
                 "species' gene symbols, unless the user asks for a cross-species comparison. ")
    text += tissue_hint([g for lib in read for g in ((lib["matrix"].get("counts") or {})
                                                      .get("top_genes") or [])])
    return text + (
        "PROVENANCE IS SETTLED: these are Cell Ranger's own unnormalized integer UMI counts. Do NOT "
        "plan a `run_code` step to open, audit or re-verify the .h5 matrices (their layout, "
        "orientation, integer values or totals) — cite these facts instead; `run_cellqc` reads the "
        "files itself.")


def cellranger_layout_hint(layout: "dict[str, Any] | None") -> str:
    """One planner-facing paragraph for a recognised delivery ('' when there is none): what the data
    is, which QC route that implies, and — with what :func:`profile_cellranger_libraries` read — what
    the matrices hold and that their provenance needs no audit step."""
    if not layout:
        return ""
    libs = layout["libraries"]
    names = ", ".join(lib["sample"] for lib in libs[:8]) + (" …" if len(libs) > 8 else "")
    bam = ("every library has its BAM" if layout["all_have_bam"] else
           "no BAM" if not any(lib["bam"] for lib in libs) else "some libraries have a BAM")
    folders = len(layout.get("datasets") or ())
    together = (f" in {folders} attached folders; `run_cellqc` reads them all in one step, so plan "
                "ONE `run_cellqc` over every library (not one per folder, and none left out unless "
                "the user asked)" if folders > 1 else "")
    return (
        f"INPUT is a 10x Cell Ranger delivery: {layout['n_libraries']} librar"
        f"{'y' if layout['n_libraries'] == 1 else 'ies'} ({names}){together} with raw AND filtered "
        f"matrices; {bam}. QC route: `run_cellqc` (CellQC: SoupX ambient-RNA correction from the raw matrix, "
        "count/feature/mitochondrial filtering, DoubletFinder doublet removal with scDblFinder as a "
        "second opinion, and the nuclear fraction from the BAM), which also writes the normalized "
        "checkpoint every later tool reads. Do NOT plan `run_scanpy_qc` for this input: it reads one "
        "filtered matrix only and cannot correct ambient RNA. Do NOT plan `run_doublet_detection` "
        "after `run_cellqc`: doublets are already called and removed. " + _provenance(libs))
