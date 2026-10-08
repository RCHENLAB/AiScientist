"""The ``run_cellqc`` tool. Documentation: ``TOOL.md`` in this folder.

Runs CellQC (github.com/lijinbio/cellqc, a Snakemake + R pipeline by Jin Li) over the Cell Ranger
libraries in a bound folder, then merges its per-library output into the checkpoint the rest of the
single-cell line reads. The job runs in the image the TOOL.md declares, which carries CellQC, R and
the Bioconductor stack; nothing here installs anything.

The steps follow the CellQC standalone skill (skills/single-cell/qc-snrna-with-cellqc-standalone):
stage the deliveries without writing into them, write the sample file and config, dry-run, run, and
validate ``result/`` before anything is used — keeping "the run completed" apart from "the result
can be trusted".
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from .._lib.cellranger import describe_cellranger_layout
from .._lib.scrna import _dirs, _import_scanpy, _p, _rel, _schema
from ..sdk import HarnessTool

_TOOL = "run_cellqc"

# Gene sets per reference, from the CellQC skill. Patterns are matched case-insensitively by CellQC;
# a set given here REPLACES its default definition, so each is written out in full.
_RIBO_EXCLUDE_HUMAN = ["^RPS6K", "^RPS19BP"]
_GENESETS: dict[str, dict[str, dict[str, Any]]] = {
    "mouse": {
        "mt": {"label": "% mitochondrial", "patterns": ["^mt-"], "symbols": [], "exclude": []},
        "ribo": {"label": "% ribosomal", "patterns": ["^Rp[sl]\\d", "^Rplp\\d", "^Rpsa$"],
                 "symbols": [], "exclude": ["^Rps6k", "^Rps19bp"]},
        "hb": {"label": "% hemoglobin", "patterns": ["^Hb[ab]-"], "symbols": [], "exclude": []},
    },
    "human": {
        "mt": {"label": "% mitochondrial", "patterns": ["^MT-"], "symbols": [], "exclude": []},
        "ribo": {"label": "% ribosomal", "patterns": ["^RP[SL]\\d", "^RPLP\\d", "^RPSA$"],
                 "symbols": [], "exclude": _RIBO_EXCLUDE_HUMAN},
        "hb": {"label": "% hemoglobin",
               "patterns": ["^HB[ABDEGMQZ]([0-9][AB]?)?$", "^HB[AB]-[A-Z0-9]+$"],
               "symbols": [], "exclude": []},
    },
}
# Ensembl Mmul_10 names mtDNA genes bare (ND1, COX1, ...): the symbols are a fallback CellQC uses
# only when no pattern matches, so on an MT- reference a bare COX1 (an old alias of PTGS1) never is.
_GENESETS["macaque"] = {
    **_GENESETS["human"],
    "mt": {"label": "% mitochondrial", "patterns": ["^MT-"], "exclude": [],
           "symbols": ["ND1", "ND2", "ND3", "ND4", "ND4L", "ND5", "ND6", "COX1", "COX2", "COX3",
                       "ATP6", "ATP8", "CYTB"]},
}
# Expected doublet rate at `capacity` cells per reaction. GEM-X (3' v4) has about half the Next GEM
# multiplet rate; 0.052 at 13000 encodes that (the CellQC skill's reference configuration).
_DOUBLET_RATE = {"next_gem": 0.1, "gem_x": 0.052}
_CAPACITY = 13000
_KAPPA_LOW = 0.2


def _paths_under(root: Path, maxdepth: int = 4) -> list[str]:
    """Every path under ``root`` down to ``maxdepth`` levels (enough to recognise the libraries)."""
    out: list[str] = []
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        depth = len(here.parts) - base
        if depth >= maxdepth:
            dirnames[:] = []
        out.extend(str(here / n) for n in (*dirnames, *filenames))
    return out


def _web_summary_field(lib_dir: Path, field: str) -> str:
    """A value from Cell Ranger's ``web_summary.html`` (``["Chemistry","Single Cell 3' v4"]``)."""
    page = lib_dir / "web_summary.html"
    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(rf'"{re.escape(field)}"\s*,\s*"([^"]+)"', text)
    return m.group(1).strip() if m else ""


def _chemistry(lib_dir: Path) -> "tuple[str, str]":
    """``(next_gem | gem_x | "", what it was read from)`` for one library."""
    chem = _web_summary_field(lib_dir, "Chemistry")
    if not chem:
        return "", ""
    low = chem.lower()
    if "v4" in low or "gem-x" in low or ("5'" in chem and "v3" in low):
        return "gem_x", chem
    if any(v in low for v in ("v2", "v3")):
        return "next_gem", chem
    return "", chem


def _species_from_transcriptome(text: str) -> str:
    low = text.lower()
    if "grcm" in low or "mm10" in low or "mm39" in low or "mouse" in low:
        return "mouse"
    if "grch" in low or "hg19" in low or "hg38" in low or "human" in low:
        return "human"
    if "mmul" in low or "macaque" in low or "macaca" in low:
        return "macaque"
    return ""


def _species_from_genes(lib_dir: Path) -> str:
    """From the gene names in the filtered matrix: ``mt-`` is mouse, ``MT-`` human, bare ``ND1``
    without any ``MT-`` the Ensembl macaque convention."""
    try:
        import h5py  # noqa: PLC0415 - present wherever CellQC runs
        with h5py.File(lib_dir / "filtered_feature_bc_matrix.h5", "r") as f:
            names = [n.decode() if isinstance(n, bytes) else str(n)
                     for n in f["matrix/features/name"][:]]
    except Exception:  # noqa: BLE001 - no .h5, or not readable: no guess
        return ""
    if any(n.startswith("mt-") for n in names):
        return "mouse"
    if any(n.startswith("MT-") for n in names):
        return "human"
    if {"ND1", "COX1", "CYTB"} <= set(names):
        return "macaque"
    return ""


def _resolve(choice: str, per_lib: "list[tuple[str, str]]", fallback: str, what: str,
             warnings: list[str]) -> "tuple[str, str]":
    """The value to use for a cohort-wide setting: the caller's, else the libraries' consensus."""
    if choice != "auto":
        return choice, "set by the caller"
    found = [(v, src) for v, src in per_lib if v]
    values = sorted({v for v, _ in found})
    if not values:
        warnings.append(f"Could not read the {what} from the libraries; assumed '{fallback}'. "
                        "State this assumption, or set it explicitly.")
        return fallback, "assumed (not found in the data)"
    if len(values) > 1:
        top = max(values, key=lambda v: sum(1 for x, _ in found if x == v))
        warnings.append(f"The libraries disagree on the {what} ({', '.join(values)}); used '{top}' "
                        "for all of them. CellQC applies one setting per run: run them separately "
                        "if this matters.")
        return top, "majority of the libraries"
    return values[0], f"read from Cell Ranger output ({found[0][1]})"


def _unpack(archive: Path, dest_parent: Path) -> None:
    """Unpack ``<name>.tar.gz`` into ``dest_parent/<name>/``. 10x Cloud archives hold the bare files;
    one that already has a top-level ``<name>/`` is unpacked into ``dest_parent`` instead, so the
    result is ``<name>/`` either way (the layout CellQC and SoupX expect)."""
    name = archive.name[: -len(".tar.gz")]
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        nested = members and all(m.name == name or m.name.startswith(name + "/") for m in members)
        target = dest_parent if nested else dest_parent / name
        target.mkdir(parents=True, exist_ok=True)
        try:
            tf.extractall(target, filter="data")
        except TypeError:          # a Python without extraction filters: refuse unsafe members
            for m in members:
                if m.name.startswith("/") or ".." in Path(m.name).parts or not (m.isfile() or m.isdir()):
                    raise ValueError(f"unsafe member {m.name!r} in {archive}") from None
            tf.extractall(target)


def _stage(lib: dict[str, Any], stage_root: Path) -> Path:
    """A writable directory for one library that LINKS every file of the (read-only) delivery and
    unpacks its matrix archives, recreating the ``outs/`` layout. Safe to rerun: existing links and
    unpacked directories are left alone."""
    src = Path(lib["dir"])
    dest = stage_root / lib["sample"]
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        link = dest / entry.name
        if not (link.exists() or link.is_symlink()):
            link.symlink_to(entry)
    for archive in sorted(src.glob("*.tar.gz")):
        name = archive.name[: -len(".tar.gz")]
        if not (dest / name).exists():
            _unpack(archive, dest)
    return dest


def _read_csv(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh, delimiter=delimiter))
    except OSError:
        return []


def _num(value: Any) -> "float | None":
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return None if x != x else x          # NaN -> None


def _int(value: Any) -> "int | None":
    x = _num(value)
    return int(x) if x is not None else None


def _tail(path: Path, n: int = 3000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def _run(cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> int:
    with log.open("a", encoding="utf-8") as fh:
        fh.write("\n$ " + " ".join(cmd) + "\n")
        fh.flush()
        return subprocess.run(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                              check=False).returncode


def _library_summary(row: dict[str, str], decider: str, comparer: str) -> dict[str, Any]:
    """One library's numbers from CellQC's ``metrics.csv``, under names a reader can follow."""
    sample = row.get("sampleid") or next(iter(row.values()), "")
    kappa_key = next((k for k in row if k.startswith("concordance_") and k.endswith("_kappa")), "")
    return {
        "sample": sample,
        "cells_cellranger": _int(row.get("filter_ncell_before")),
        "cells_after_filter": _int(row.get("filter_ncell_after")),
        "cells_after_doublets": _int(row.get("doublet_ncell_after")),
        "frac_retained": _num(row.get("frac_retained")),
        "removed_only_by": {
            "min_counts": _int(row.get("filter_fail_mincount_only")),
            "min_features": _int(row.get("filter_fail_minfeature_only")),
            "max_pct_mt": _int(row.get("filter_fail_mito_only")),
            "several_thresholds": _int(row.get("filter_fail_multiple")),
        },
        "removed_by_mito_total": _int(row.get("filter_fail_mito")),
        "n_mt_genes": _int(row.get("filter_n_mt_genes")),
        "mt_matched_by": row.get("filter_mt_matched_by") or "",
        "median_pct_mt": _num(row.get("filter_median_pct_counts_mt")),
        "ambient_soupx_contamination_mean": _num(row.get("ambient_soupx_contamination_mean")),
        # The compared method never touches the counts (its counts_removed_frac is NA): its own
        # contamination estimate is what sits next to the applied one.
        f"ambient_{comparer}_contamination_mean": (
            _num(row.get(f"ambient_{comparer}_contamination_mean")) if comparer else None),
        "doublets_removed": _int(row.get(f"doublet_{decider}_ndoublet")),
        "doublet_kappa": _num(row.get(kappa_key)) if kappa_key else None,
        "nuclear_fraction_median": _num(row.get("nf_median")),
        "nuclear_fraction_iqr": [_num(row.get("nf_q25")), _num(row.get("nf_q75"))],
    }


def _validate(libs: list[dict[str, Any]], status_rows: list[dict[str, str]],
              manifest_rows: list[dict[str, str]], args: dict[str, Any]) -> list[str]:
    """The CellQC validation checklist, as warnings a reader cannot miss."""
    warnings: list[str] = []
    for row in status_rows:
        st = (row.get("status") or "").strip()
        if st in ("failed", "fallback") or (st == "skipped" and row.get("step") != "nuclear_fraction"):
            warnings.append(f"{row.get('sample')}: step {row.get('step')} {st} — "
                            f"{(row.get('message') or '').strip()[:200]}")
    for row in manifest_rows:
        if str(row.get("included", "")).strip().lower() not in ("true", "1", "yes"):
            warnings.append(f"{row.get('sample')} was EXCLUDED from the result: "
                            f"{(row.get('reason') or 'no reason given').strip()}")
    for lib in libs:
        if lib["n_mt_genes"] == 0:
            warnings.append(f"{lib['sample']}: NO mitochondrial gene matched, so the "
                            f"max_pct_mt filter removed nothing. Do NOT state that high-"
                            "mitochondrial cells were filtered; check the species / gene set.")
    matched = {lib["mt_matched_by"] for lib in libs if lib["mt_matched_by"]}
    if len(matched) > 1:
        warnings.append(f"The mitochondrial genes were matched differently across libraries "
                        f"({', '.join(sorted(matched))}); the mito threshold does not mean the "
                        "same thing in each.")
    if libs and all((lib["removed_by_mito_total"] or 0) == 0 for lib in libs):
        warnings.append("The mitochondrial threshold removed no cell in any library. That is not "
                        "evidence the data are clean: say the threshold was inert at this value.")
    keep_floor = float(_p(_TOOL, "warn_retained_below", args))
    fracs = sorted(lib["frac_retained"] for lib in libs if lib["frac_retained"] is not None)
    median = fracs[len(fracs) // 2] if fracs else None
    for lib in libs:
        f = lib["frac_retained"]
        if f is None:
            continue
        if f < keep_floor or (median is not None and len(fracs) > 2 and median - f > 0.2):
            versus = f" (cohort median {median:.0%})" if len(fracs) > 1 else ""
            cause = max(lib["removed_only_by"].items(), key=lambda kv: kv[1] or 0)
            why = (f"; {cause[1]:,} cells failed only the {cause[0]} threshold"
                   if cause[1] else "")
            warnings.append(f"{lib['sample']} kept only {f:.0%} of its Cell Ranger cells{versus}"
                            f"{why}. Check that threshold against this tissue before using the "
                            "result; do not average this library away.")
    amb_cap = float(_p(_TOOL, "warn_ambient_frac", args))
    for lib in libs:
        a = lib["ambient_soupx_contamination_mean"]
        if a is not None and a > amb_cap:
            warnings.append(f"{lib['sample']}: mean ambient contamination {a:.0%} (SoupX). High "
                            "ambient load fits both real cell loss in the tissue and a technical "
                            "failure; if it differs between conditions, carry it as a covariate.")
    for lib in libs:
        k = lib["doublet_kappa"]
        if k is not None and k < _KAPPA_LOW:
            warnings.append(f"{lib['sample']}: the two doublet callers agree poorly (kappa {k:.2f}), "
                            "so the choice of decider drives which cells are removed. Report it.")
    return warnings


def _plain_strings(adata: Any) -> None:
    """Text indexes, columns and categories as plain ``object`` arrays, in place.

    This job runs in CellQC's image (anndata 0.13, pandas 3), whose default string dtype is a
    pandas nullable StringArray. The next tools run in analysis.sif (anndata 0.12): they read such a
    file fine, but cannot WRITE the nullable strings back ("allow_write_nullable_strings is False"),
    so run_clustering died saving its checkpoint (job 57636705, 2026-10-02). Plain object strings
    read and write the same in both versions."""
    import pandas as pd  # noqa: PLC0415

    def obj_index(idx: Any) -> Any:
        if pd.api.types.is_numeric_dtype(idx.dtype):
            return idx
        return pd.Index(idx.astype(str).to_numpy(dtype=object), dtype=object, name=idx.name)

    def obj_series(values: Any) -> Any:
        # dtype=object explicitly: pandas 3 re-infers a numpy array of str as its string dtype.
        return pd.Series(values.to_numpy(dtype=object), index=values.index, dtype=object)

    for df in (adata.obs, adata.var):
        df.index = obj_index(df.index)
        for col in list(df.columns):
            values = df[col]
            if isinstance(values.dtype, pd.CategoricalDtype):
                cats = values.cat.categories
                if not pd.api.types.is_numeric_dtype(cats.dtype):
                    df[col] = pd.Categorical(values.astype(object).to_numpy(dtype=object),
                                             categories=obj_index(cats),
                                             ordered=values.cat.ordered)
            elif pd.api.types.is_string_dtype(values.dtype) and values.dtype != object:
                df[col] = obj_series(values)
    if adata.raw is not None and adata.raw.var.index.dtype != object:
        raw = adata.raw.to_adata()
        raw.var.index = obj_index(raw.var.index)
        for col in list(raw.var.columns):
            if pd.api.types.is_string_dtype(raw.var[col].dtype) and raw.var[col].dtype != object:
                raw.var[col] = obj_series(raw.var[col])
        adata.raw = raw


def _merge_and_preprocess(sc: Any, h5ads: list[Path], n_top_genes: int, figs: Path) -> Any:
    """CellQC's per-library results as ONE object, in the checkpoint shape the later tools read:
    counts in ``layers['counts']``, log-normalised ``.X`` and ``.raw``, highly-variable genes."""
    import anndata as ad  # noqa: PLC0415
    parts = [sc.read_h5ad(p) for p in h5ads]
    adata = ad.concat(parts, join="inner", merge="same") if len(parts) > 1 else parts[0]
    if "sampleid" in adata.obs:
        adata.obs["sampleid"] = adata.obs["sampleid"].astype("category")
    _plain_strings(adata)                   # before .raw copies var: see _plain_strings
    adata.layers["counts"] = adata.X.copy()
    sc.settings.figdir = str(figs)
    keys = [k for k in ("n_genes_by_counts", "total_counts", "pct_counts_mt") if k in adata.obs]
    if keys:
        sc.pl.violin(adata, keys, groupby="sampleid" if "sampleid" in adata.obs else None,
                     multi_panel=True, show=False, save="_cellqc_after_qc.png")
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.raw = adata
    batch = "sampleid" if "sampleid" in adata.obs and adata.obs["sampleid"].nunique() > 1 else None
    sc.pp.highly_variable_genes(adata, n_top_genes=min(n_top_genes, adata.n_vars), batch_key=batch)
    return adata


def run_cellqc(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """CellQC over the bound folder's Cell Ranger libraries -> ``work/adata_qc.h5ad`` + CellQC's
    own report and tables, with its validation checklist turned into warnings."""
    root = str((getattr(ctx, "decisions", None) or {}).get("dataset_root") or "")
    if not root or not Path(root).is_dir():
        return {"status": "error", "step": "qc",
                "error": ("run_cellqc reads a FOLDER of Cell Ranger outputs (raw + filtered matrices "
                          "per library), and the bound dataset is not one. For a single matrix or "
                          "an .h5ad, use run_scanpy_qc.")}
    cellqc = shutil.which("cellqc")
    if cellqc is None:
        return {"status": "not_enabled", "step": "qc",
                "note": ("CellQC is not available where this step ran. run_cellqc runs as an HPC3 "
                         "job in its own image; without HPC3, use run_scanpy_qc instead and say "
                         "that ambient correction was not done.")}
    layout = describe_cellranger_layout(_paths_under(Path(root)), root)
    if not layout:
        return {"status": "error", "step": "qc",
                "error": (f"No Cell Ranger library under {Path(root).name}/: a library is a "
                          "directory holding both raw_feature_bc_matrix and "
                          "filtered_feature_bc_matrix (.h5, a directory, or .tar.gz).")}
    wanted = [str(s) for s in (args.get("samples") or []) if str(s).strip()]
    libs = [lib for lib in layout["libraries"] if not wanted or lib["sample"] in wanted]
    if not libs:
        return {"status": "error", "step": "qc",
                "error": f"none of {wanted} is a library here; found "
                         f"{[lib['sample'] for lib in layout['libraries']]}"}
    sample_ids = [lib["sample"] for lib in libs]
    if len(set(sample_ids)) != len(sample_ids):
        return {"status": "error", "step": "qc",
                "error": f"two libraries share a sample id: {sample_ids}. Rename the folders."}

    work, art, figs, tables = _dirs(ctx)
    qc = work / "cellqc"
    out = qc / "out"
    stage_root = qc / "lnfiles"
    for d in (qc / "home", qc / "tmp", qc / "cache", stage_root):
        d.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    staged = {lib["sample"]: _stage(lib, stage_root) for lib in libs}

    lib_species = [(_species_from_transcriptome(_web_summary_field(Path(lib["dir"]), "Transcriptome"))
                    or _species_from_genes(staged[lib["sample"]]),
                    _web_summary_field(Path(lib["dir"]), "Transcriptome") or "gene names")
                   for lib in libs]
    # One gene set per run: unlike a chemistry mismatch (a doublet-rate guess), a species mismatch
    # filters one species' cells with the other's mitochondrial genes, so it is refused, not averaged.
    by_species: dict[str, list[str]] = {}
    for lib, (sp, _src) in zip(libs, lib_species):
        if sp:
            by_species.setdefault(sp, []).append(lib["sample"])
    if len(by_species) > 1:
        groups = "; ".join(f"{sp}: {', '.join(names)}" for sp, names in sorted(by_species.items()))
        return {"status": "error", "step": "qc", "species_by_library": by_species,
                "error": (f"The libraries are from different species ({groups}). CellQC applies one "
                          "mitochondrial and ribosomal gene set per run, so one species would be "
                          "filtered with the other's genes. QC one species at a time: pass `samples` "
                          "with one species' libraries, or bind each species' folders as a separate "
                          "dataset.")}
    species, species_src = _resolve(str(_p(_TOOL, "species", args)), lib_species, "human",
                                    "species", warnings)
    chemistry, chem_src = _resolve(
        str(_p(_TOOL, "chemistry", args)),
        [_chemistry(Path(lib["dir"])) for lib in libs], "next_gem", "10x chemistry", warnings)
    decider = str(_p(_TOOL, "doublet_decider", args))
    callers = ["doubletfinder", "scdblfinder"]
    comparer = str(_p(_TOOL, "ambient_compare", args) or "").strip()
    ambient = str(_p(_TOOL, "ambient_method", args))
    nreaction = int(_p(_TOOL, "nreaction", args))
    config = {
        "seed": int(_p(_TOOL, "seed", args)),
        "ambient": {"method": ambient,
                    "compare": [comparer] if comparer and comparer != ambient else []},
        "filterbycount": {"mincount": int(_p(_TOOL, "min_counts", args)),
                          "minfeature": int(_p(_TOOL, "min_features", args)),
                          "mito": float(_p(_TOOL, "max_pct_mt", args))},
        "geneset": _GENESETS[species],
        "doublet": {"run": callers, "decider": decider, "findpK": False, "pK": 0.01,
                    "numthreads": 5, "rate": _DOUBLET_RATE[chemistry], "capacity": _CAPACITY,
                    "nreaction": nreaction},
    }
    # CellQC reads YAML, and JSON is YAML: no YAML writer needed in any image.
    (qc / "config.yaml").write_text(json.dumps(config, indent=2), encoding="utf-8")
    with (qc / "samples.txt").open("w", encoding="utf-8") as fh:
        fh.write("sample\tcellranger\tnreaction\n")
        for lib in libs:
            fh.write(f"{lib['sample']}\t{staged[lib['sample']]}\t{nreaction}\n")

    threads = int(os.environ.get("AISCIENTIST_JOB_CPUS") or os.cpu_count() or 4)
    env = {**os.environ, "HOME": str(qc / "home"), "TMPDIR": str(qc / "tmp"),
           "XDG_CACHE_HOME": str(qc / "cache"), "MPLBACKEND": "Agg"}
    log = qc / "cellqc.log"
    base_cmd = [cellqc, "-d", str(out), "-t", str(threads), "-c", str(qc / "config.yaml")]
    if _run([*base_cmd, "-n", "--", str(qc / "samples.txt")], qc, env, log) != 0:
        return {"status": "error", "step": "qc",
                "error": "CellQC rejected the configuration (dry run failed): " + _tail(log, 1500)}
    rc = _run([*base_cmd, "--", str(qc / "samples.txt")], qc, env, log)
    if rc != 0 and "older modification time" in _tail(log, 20000):
        # An intermittent CellQC publish bug (hard-linked outputs keep their source's timestamp),
        # not a clock problem; the CellQC skill's remedy is a fresh output directory.
        shutil.rmtree(out, ignore_errors=True)
        rc = _run([*base_cmd, "--", str(qc / "samples.txt")], qc, env, log)

    result_dir = out / "result"
    manifest_rows = _read_csv(result_dir / "manifest.tsv", "\t")
    status_rows = _read_csv(result_dir / "qc_status.csv")
    metric_rows = _read_csv(result_dir / "metrics.csv")
    included = [r["sample"] for r in manifest_rows
                if str(r.get("included", "")).strip().lower() in ("true", "1", "yes")]
    h5ads = [result_dir / f"{s}.h5ad" for s in included if (result_dir / f"{s}.h5ad").is_file()]
    if not h5ads:
        return {"status": "error", "step": "qc",
                "error": f"CellQC produced no usable library (exit {rc}): " + _tail(log, 2000),
                "log": str(log)}
    if rc != 0:
        warnings.append(f"CellQC exited with status {rc} although {len(h5ads)} librar"
                        f"{'y' if len(h5ads) == 1 else 'ies'} reached result/ — most often the "
                        "slide deck (a LaTeX step) failed. The QC'd matrices and metrics are used; "
                        "check cellqc/cellqc.log.")

    lib_summaries = [_library_summary(r, decider, comparer) for r in metric_rows]
    warnings.extend(_validate(lib_summaries, status_rows, manifest_rows, args))

    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return {"status": "error", "step": "qc", "error": f"scanpy is missing: {exc}"}
    adata = _merge_and_preprocess(sc, h5ads, int(_p(_TOOL, "n_top_genes", args)), figs)
    adata.uns["cellqc"] = {"species": species, "chemistry": chemistry, "decider": decider,
                           "ambient_method": ambient, "libraries": included}
    _plain_strings(adata)                   # HVG / plotting may have added text columns
    adata.write(work / "adata_qc.h5ad")

    # CellQC's own outputs, where the report bundle and the files browser find them.
    cq_art = art / "cellqc"
    cq_art.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in ("report.html", "report_slides.pdf", "metrics.csv", "qc_status.csv", "manifest.tsv"):
        src = result_dir / name
        if src.is_file():
            shutil.copy2(src, cq_art / name)
            copied.append(_rel(art, cq_art / name))
    for src in sorted(result_dir.glob("*_doublet_*.txt")):
        shutil.copy2(src, cq_art / src.name)
        copied.append(_rel(art, cq_art / src.name))
    if (result_dir / "metrics.csv").is_file():
        shutil.copy2(result_dir / "metrics.csv", tables / "cellqc_metrics.csv")
    figures = [_rel(art, p) for p in sorted(figs.glob("*cellqc_after_qc*.png"))]

    n_mt = [lib["n_mt_genes"] for lib in lib_summaries if lib["n_mt_genes"] is not None]
    cells_before = sum(lib["cells_cellranger"] or 0 for lib in lib_summaries)
    return {
        "status": "ok",
        "step": "qc",
        "method": "CellQC 0.3.6 (SoupX ambient correction, count/feature/mito filtering, "
                  "DoubletFinder + scDblFinder, nuclear fraction)",
        "libraries": lib_summaries,
        "n_libraries": len(h5ads),
        "excluded_libraries": [s for s in sample_ids if s not in included],
        "cells_before": cells_before,
        "cells_after": int(adata.n_obs),
        "genes_after": int(adata.n_vars),
        "n_hvg": int(adata.var["highly_variable"].sum()) if "highly_variable" in adata.var else 0,
        "pct_cells_removed": round(100.0 * (cells_before - adata.n_obs) / cells_before, 2)
        if cells_before else 0.0,
        "n_mt_genes": min(n_mt) if n_mt else 0,
        "mt_filter_effective": bool(n_mt) and min(n_mt) > 0,
        "thresholds": {"min_counts": config["filterbycount"]["mincount"],
                       "min_features": config["filterbycount"]["minfeature"],
                       "max_pct_mt": config["filterbycount"]["mito"]},
        "assumptions": {"species": [species, species_src], "chemistry": [chemistry, chem_src],
                        "doublet_rate": f"{_DOUBLET_RATE[chemistry]} at {_CAPACITY} cells per "
                                        "reaction", "nreaction": [nreaction, "set by the caller"
                                                                  if args.get("nreaction") else
                                                                  "default 1 (not measured; confirm)"]},
        "ambient": {"applied": ambient, "compared": comparer or "none"},
        "doublets": {"decider": decider, "callers": callers, "removed_upstream": True},
        "warnings": warnings,
        "obs_sample_key": "sampleid",
        "layers": ["counts"],
        "raw_slot": "log1p_normalized",
        "checkpoint": "adata_qc.h5ad",
        "figures": figures,
        "tables": ["tables/cellqc_metrics.csv"] if (tables / "cellqc_metrics.csv").is_file() else [],
        "cellqc_outputs": copied,
        "read_from": f"the bound folder ({Path(root).name}/)",
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_cellqc`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        _TOOL,
        "CellQC quality control for a FOLDER of 10x Cell Ranger outputs (one or more libraries with "
        "raw AND filtered matrices): SoupX ambient-RNA correction, count/feature/mitochondrial "
        "filtering, DoubletFinder doublet removal (scDblFinder as a second opinion), and the "
        "nuclear fraction from the BAM. Merges the libraries (obs 'sampleid') and writes the "
        "normalized checkpoint the later tools read. Use it INSTEAD of run_scanpy_qc when the data "
        "are Cell Ranger outputs; do not run run_doublet_detection after it.",
        _schema(_TOOL, samples={
            "type": "array", "items": {"type": "string"},
            "description": "only these libraries (by sample id = folder name); empty = all"}),
        run_cellqc,
        reads_private_data=True, category="qc", requires=("scanpy",),
    )
