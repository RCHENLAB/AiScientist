"""The ``run_cellqc`` tool. Documentation: ``TOOL.md`` in this folder.

Runs CellQC (github.com/lijinbio/cellqc, a Snakemake + R pipeline by Jin Li) over the Cell Ranger
libraries in a bound folder, then merges its per-library output into the checkpoint the rest of the
single-cell line reads. The job runs in the image of the CellQC skill its TOOL.md names, which carries CellQC, R and the
Bioconductor stack; nothing here installs anything. The skill also supplies CellQC's reference
settings (gene sets, doublet rates, default filters): see :func:`_settings`.

The steps follow the CellQC standalone skill (skills/single-cell/qc-snrna-with-cellqc-standalone):
stage the deliveries without writing into them, write the sample file and config, dry-run, run, and
validate ``result/`` before anything is used — keeping "the run completed" apart from "the result
can be trusted".
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from .._lib.cellranger import describe_cellranger_layout, merge_cellranger_layouts
from .._lib.result_dataset import CELLQC_POINTER as RESULT_POINTER
from .. import catalog
from .._lib.scrna import _dirs, _import_scanpy, _p, _rel, _schema
from ..sdk import HarnessTool

_TOOL = "run_cellqc"

# CellQC's reference settings (gene sets per reference, the expected doublet rate per chemistry and
# the reaction capacity, the default count filters) are the CellQC skill's: its
# assets/cellqc-defaults.json, sent with the job by the gateway (see _settings). They are not copied
# here, so updating the skill updates this tool.
_SETTINGS_FILE = "assets/cellqc-defaults.json"
_THRESHOLDS = ("min_counts", "min_features", "max_pct_mt")
_KAPPA_LOW = 0.2


class _SettingsError(ValueError):
    """The CellQC skill's settings are missing or unusable."""


def _settings(args: dict[str, Any]) -> dict[str, Any]:
    """CellQC's reference settings from the CellQC skill: the gene sets per reference, the expected
    doublet rate per chemistry and the reaction capacity, and the default count filters. The
    platform attaches the skill to every call (``args[catalog.SKILL_ARG]``), on HPC3 and in-process
    alike, and records its version in the run's manifest.json."""
    sent = args.get(catalog.SKILL_ARG)
    if not isinstance(sent, dict):
        raise _SettingsError("the CellQC skill was not attached to this call (the platform's tool "
                             "router attaches it; see TOOL.md)")
    text = (sent.get("files") or {}).get(_SETTINGS_FILE)
    where = f"skill {sent.get('name')!r} {sent.get('version') or ''}".strip()
    if not text:
        raise _SettingsError(f"{where} has no {_SETTINGS_FILE}, which holds CellQC's settings")
    try:
        settings = json.loads(text)
        ok = (isinstance(settings["genesets"], dict) and isinstance(settings["doublet"]["rate"], dict)
              and int(settings["doublet"]["capacity"]) > 0
              and all(k in settings["thresholds"] for k in _THRESHOLDS))
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise _SettingsError(f"{where}: {_SETTINGS_FILE} is not valid (it needs genesets, "
                             f"doublet.rate, doublet.capacity and thresholds {list(_THRESHOLDS)})")
    return settings


def _threshold(name: str, args: dict[str, Any], settings: dict[str, Any]) -> Any:
    """The caller's value for a count filter, else the CellQC skill's default."""
    return args[name] if args.get(name) is not None else settings["thresholds"][name]


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
            # The warnings are copied into the user's report as they are: facts, no instructions.
            warnings.append(f"{lib['sample']}: the two doublet callers agree poorly (kappa {k:.2f}), "
                            "so the choice of decider drives which cells are removed.")
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


# The bundle is copied from HPC3 to the gateway after every step, file by file: a table bigger than
# this (the per-cell obs of a very large library) stays with the complete directory only.
_BUNDLE_FILE_MAX = 64 * 1024 * 1024


def _link_or_copy(src: Path, dst: Path) -> None:
    """A second name for ``src`` where the filesystem allows it (no bytes copied), else a copy."""
    try:
        os.link(os.path.realpath(src), dst)          # the file itself, not a link CellQC published
    except OSError:
        shutil.copy2(src, dst)


def _publish_result(result_dir: Path, workspace: Path, art: Path,
                    included: list[str]) -> "tuple[list[str], dict[str, Any]]":
    """Hand on CellQC's ``result/`` directory, complete and under its own name.

    It used to be a hand-picked seven of its files, in ``artifacts/cellqc/`` (which the bundle
    then filed under ``extra/`` as a stray): the per-library ``.h5ad``, the ``_obs`` / ``_var``
    tables and the slide deck's source were left behind in ``work/`` (RCHENLAB/AiScientist
    issue 3). Now:

    - ``artifacts/result/`` gets every file except the matrices (and any table over
      ``_BUNDLE_FILE_MAX``), so the bundle and the files browser show CellQC's directory as CellQC
      wrote it;
    - ``<run>/result/cellqc/`` gets every file, matrices included. It is outside ``artifacts/``
      for the reason the processed dataset is: on HPC3 everything under artifacts is copied to
      the gateway after each step, and a matrix has no business there. The console downloads this
      directory as one zip, and "save to my data" moves it to the user's folder as a dataset.

    Returns the bundle paths written, and the pointer the console reads to offer both (also
    written to ``data/cellqc_result.json``, beside ``result_dataset.json``).
    """
    files = sorted(p for p in result_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    bundle_dir = art / "result"
    full_dir = workspace / "result" / "cellqc"
    for d in (bundle_dir, full_dir):
        shutil.rmtree(d, ignore_errors=True)       # a re-run replaces the earlier run's files
        d.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    listing: list[dict[str, Any]] = []
    for src in files:
        size = src.stat().st_size
        _link_or_copy(src, full_dir / src.name)
        listing.append({"name": src.name, "size": size})
        if src.suffix != ".h5ad" and size <= _BUNDLE_FILE_MAX:
            shutil.copy2(src, bundle_dir / src.name)
            copied.append(_rel(art, bundle_dir / src.name))
    matrices = [f["name"] for f in listing if f["name"].endswith(".h5ad")]
    in_bundle = {Path(c).name for c in copied}
    pointer = {
        "path": str(full_dir), "files": listing,
        "size_bytes": sum(f["size"] for f in listing),
        "libraries": included, "matrices": matrices,
        "in_bundle": "result/",
        "not_in_bundle": [f["name"] for f in listing if f["name"] not in in_bundle],
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "saved_to": None,
    }
    (art / "data").mkdir(parents=True, exist_ok=True)
    (art / "data" / RESULT_POINTER).write_text(json.dumps(pointer, indent=2), encoding="utf-8")
    return copied, pointer


def _counts_layer_note(ambient: str) -> str:
    """One sentence on what ``layers['counts']`` holds, for the checkpoint, the result and the report."""
    if str(ambient).lower() in ("none", "", "false", "off"):
        return ("integer UMI counts of the cells that passed CellQC, with no ambient-RNA correction "
                "(ambient_method=none)")
    return (f"ambient-corrected integer UMI counts from CellQC ({ambient}), not the Cell Ranger "
            "matrix: ambient RNA has already been subtracted from every cell")


def _merge_and_preprocess(sc: Any, h5ads: list[Path], n_top_genes: int, figs: Path,
                          tag: str = "") -> Any:
    """CellQC's per-library results as ONE object, in the checkpoint shape the later tools read:
    counts in ``layers['counts']``, log-normalised ``.X`` and ``.raw``, highly-variable genes.
    ``tag`` (a species) goes into the figure's name when several species are QC'd apart."""
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
                     multi_panel=True, show=False,
                     save=f"_cellqc_after_qc{'_' + tag if tag else ''}.png")
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.raw = adata
    batch = "sampleid" if "sampleid" in adata.obs and adata.obs["sampleid"].nunique() > 1 else None
    sc.pp.highly_variable_genes(adata, n_top_genes=min(n_top_genes, adata.n_vars), batch_key=batch)
    return adata


def _add_kept_median_mt(adata: Any, libs: list[dict[str, Any]]) -> None:
    """Each library's median % mitochondrial over the cells it KEPT, as ``median_pct_mt_kept``.
    CellQC's own ``median_pct_mt`` is the filter step's, over every Cell Ranger cell before the
    filters (run 6275df1213ab: 0.77 there, 0.69 over the 14,553 kept cells)."""
    if "pct_counts_mt" not in adata.obs:
        return
    import numpy as np  # noqa: PLC0415
    pct = adata.obs["pct_counts_mt"].to_numpy(dtype=float)
    sample = (adata.obs["sampleid"].astype(str).to_numpy() if "sampleid" in adata.obs else None)
    if sample is None and len(libs) > 1:
        return
    for lib in libs:
        sel = pct if sample is None else pct[sample == str(lib["sample"])]
        sel = sel[~np.isnan(sel)]
        lib["median_pct_mt_kept"] = round(float(np.median(sel)), 4) if sel.size else None


def _input_folders(ctx: Any) -> list[dict[str, str]]:
    """The folders this step reads, as ``{"name", "path"}``: every folder bound to the run
    (``decisions["input_folders"]``, named as the user sees them), else the one bound folder
    (``dataset_root``) of a run that set nothing else."""
    decisions = getattr(ctx, "decisions", None) or {}
    folders = [{"name": str(f.get("name") or ""), "path": str(f["path"])}
               for f in decisions.get("input_folders") or []
               if isinstance(f, dict) and f.get("path")]
    if folders:
        return folders
    root = str(decisions.get("dataset_root") or "")
    return [{"name": "", "path": root}] if root else []


def _folder_name(folder: dict[str, str]) -> str:
    return (folder["name"] or Path(folder["path"]).name).rstrip("/") + "/"


def run_cellqc(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """CellQC over the Cell Ranger libraries of every bound folder -> ``work/adata_qc.h5ad`` +
    CellQC's own report and tables, with its validation checklist turned into warnings."""
    folders = _input_folders(ctx)
    readable = [f for f in folders if Path(f["path"]).is_dir()]
    if not readable:
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
    try:
        settings = _settings(args)
    except _SettingsError as exc:
        return {"status": "error", "step": "qc", "error": f"run_cellqc cannot read CellQC's settings: {exc}"}
    parts = [(f, describe_cellranger_layout(_paths_under(Path(f["path"])), f["path"]))
             for f in readable]
    layout = merge_cellranger_layouts([(f["name"], part) for f, part in parts])
    if not layout:
        return {"status": "error", "step": "qc",
                "error": (f"No Cell Ranger library in {', '.join(_folder_name(f) for f in readable)}: "
                          "a library is a directory holding both raw_feature_bc_matrix and "
                          "filtered_feature_bc_matrix (.h5, a directory, or .tar.gz).")}
    # A bound folder this step cannot use is named, never dropped without a word.
    skipped = [f"{_folder_name(f)} could not be read where this step ran, so it was not QC'd."
               for f in folders if f not in readable]
    skipped += [f"{_folder_name(f)} holds no Cell Ranger library (raw AND filtered matrices), so it "
                "was not QC'd." for f, part in parts if not part]
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
    base = work / "cellqc"
    stage_root = base / "lnfiles"
    stage_root.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = list(skipped)
    staged = {lib["sample"]: _stage(lib, stage_root) for lib in libs}

    lib_species = [(_species_from_transcriptome(_web_summary_field(Path(lib["dir"]), "Transcriptome"))
                    or _species_from_genes(staged[lib["sample"]]),
                    _web_summary_field(Path(lib["dir"]), "Transcriptome") or "gene names")
                   for lib in libs]
    groups = _species_groups(libs, lib_species, str(_p(_TOOL, "species", args)), warnings)
    several = len(groups) > 1
    decider = str(_p(_TOOL, "doublet_decider", args))
    comparer = str(_p(_TOOL, "ambient_compare", args) or "").strip()
    ambient = str(_p(_TOOL, "ambient_method", args))
    nreaction = int(_p(_TOOL, "nreaction", args))

    # One CellQC run per species (one gene set per run), each in its own directory when there are
    # several; a single species keeps the one directory it always had.
    runs: list[dict[str, Any]] = []
    for species, species_src, members in groups:
        qc = base / species if several else base
        chemistry, chem_src = _resolve(
            str(_p(_TOOL, "chemistry", args)),
            [_chemistry(Path(lib["dir"])) for lib in members], "next_gem", "10x chemistry", warnings)
        if species not in settings["genesets"] or chemistry not in settings["doublet"]["rate"]:
            done = {"error": (f"the CellQC skill's settings have no "
                              + (f"gene sets for {species}" if species not in settings["genesets"]
                                 else f"doublet rate for the {chemistry} chemistry")
                              + f" (it has {sorted(settings['genesets'])} and "
                                f"{sorted(settings['doublet']['rate'])})")}
        else:
            done = _run_cellqc_once(cellqc, members, staged, species, chemistry, qc, args, settings)
        done.update(species=species, species_src=species_src, chemistry=chemistry,
                    chem_src=chem_src, libs=members)
        if done.get("error") and not several:
            return {"status": "error", "step": "qc", **{k: done[k] for k in ("error", "log") if k in done}}
        if done.get("error"):
            warnings.append(f"The {species} libraries ({', '.join(lib['sample'] for lib in members)}) "
                            f"were not QC'd: {done['error'][:600]}")
            continue
        if done["rc"] != 0:
            warnings.append(f"CellQC exited with status {done['rc']} although {len(done['h5ads'])} "
                            f"librar{'y' if len(done['h5ads']) == 1 else 'ies'} reached result/ — most "
                            "often the slide deck (a LaTeX step) failed. The QC'd matrices and metrics "
                            f"are used; check cellqc/{species + '/' if several else ''}cellqc.log.")
        done["summaries"] = [{**_library_summary(r, decider, comparer), "species": species}
                             for r in done["metric_rows"]]
        warnings.extend(_validate(done["summaries"], done["status_rows"], done["manifest_rows"], args))
        runs.append(done)
    if not runs:
        return {"status": "error", "step": "qc",
                "error": "CellQC produced no usable library for any species: " + " ".join(warnings)[-2000:]}

    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return {"status": "error", "step": "qc", "error": f"scanpy is missing: {exc}"}
    # What the counts layer IS. CellQC writes the AMBIENT-CORRECTED matrix, so these are not Cell
    # Ranger's counts: checked on run f3b8268c4fd4 (SoupX), every cell's total was lower and the
    # layer summed to 97.4% of the filtered matrix, yet its manuscript called the layer "pre-SoupX".
    counts_layer = _counts_layer_note(ambient)
    checkpoints: dict[str, dict[str, Any]] = {}
    first = None
    for i, done in enumerate(runs):
        # Later steps read adata_qc.h5ad: the first species' (the primary folder's). Another species'
        # libraries are not merged into it (their genes are other genes); each has its own file.
        adata = _merge_and_preprocess(sc, done["h5ads"], int(_p(_TOOL, "n_top_genes", args)), figs,
                                      tag=done["species"] if several else "")
        _add_kept_median_mt(adata, done["summaries"])
        adata.uns["cellqc"] = {"species": done["species"], "chemistry": done["chemistry"],
                               "decider": decider, "ambient_method": ambient,
                               "libraries": done["included"]}
        adata.uns["counts_layer"] = counts_layer
        _plain_strings(adata)                   # HVG / plotting may have added text columns
        name = "adata_qc.h5ad" if i == 0 else f"adata_qc.{done['species']}.h5ad"
        adata.write(work / name)
        checkpoints[done["species"]] = {"checkpoint": name, "libraries": done["included"],
                                        "cells": int(adata.n_obs), "genes": int(adata.n_vars),
                                        "n_hvg": int(adata.var["highly_variable"].sum())
                                        if "highly_variable" in adata.var else 0}
        if first is None:
            first = checkpoints[done["species"]]
    if several:
        names = {sp: c["checkpoint"] for sp, c in checkpoints.items()}
        warnings.append(
            "The libraries are from different species ("
            + "; ".join(f"{d['species']}: {', '.join(lib['sample'] for lib in d['libs'])}" for d in runs)
            + "). CellQC applies one mitochondrial, ribosomal and hemoglobin gene set per run, so each "
            "species was QC'd in its own CellQC run with its own gene sets. Later steps read the "
            f"{runs[0]['species']} libraries' checkpoint ({names[runs[0]['species']]}); "
            + "; ".join(f"the {sp} libraries' QC'd data is work/{n}" for sp, n in names.items()
                        if n != "adata_qc.h5ad")
            + ". All their CellQC files are in result/.")

    # CellQC's own result/ directory, complete: see _publish_result for what goes where. Several
    # species' runs are put in ONE flat directory (the download and "Save to my data" read it so).
    result_dir = runs[0]["result_dir"] if not several else _combine_results(runs, base / "result")
    included = [s for done in runs for s in done["included"]]
    copied, cellqc_result = _publish_result(result_dir, work.parent, art, included)
    if (result_dir / "metrics.csv").is_file():
        shutil.copy2(result_dir / "metrics.csv", tables / "cellqc_metrics.csv")
    figures = [_rel(art, p) for p in sorted(figs.glob("*cellqc_after_qc*.png"))]

    lib_summaries = [lib for done in runs for lib in done["summaries"]]
    n_mt = [lib["n_mt_genes"] for lib in lib_summaries if lib["n_mt_genes"] is not None]
    cells_before = sum(lib["cells_cellranger"] or 0 for lib in lib_summaries)
    cells_after = sum(c["cells"] for c in checkpoints.values())
    head = runs[0]
    config = head["config"]
    species_note = ("; ".join(f"{d['species']} for {', '.join(d['included'])} ({d['species_src']})"
                    for d in runs) if several else head["species_src"])
    chem_note = ("; ".join(f"{d['chemistry']} for the {d['species']} libraries ({d['chem_src']})"
                 for d in runs) if several else head["chem_src"])
    out = {
        "status": "ok",
        "step": "qc",
        "method": "CellQC 0.3.6 (SoupX ambient correction, count/feature/mito filtering, "
                  "DoubletFinder + scDblFinder, nuclear fraction)",
        "libraries": lib_summaries,
        "n_libraries": sum(len(d["h5ads"]) for d in runs),
        "excluded_libraries": [s for s in sample_ids if s not in included],
        "cells_before": cells_before,
        "cells_after": cells_after,
        "genes_after": first["genes"],
        "n_hvg": first["n_hvg"],
        "pct_cells_removed": round(100.0 * (cells_before - cells_after) / cells_before, 2)
        if cells_before else 0.0,
        "n_mt_genes": min(n_mt) if n_mt else 0,
        "mt_filter_effective": bool(n_mt) and min(n_mt) > 0,
        "thresholds": {"min_counts": config["filterbycount"]["mincount"],
                       "min_features": config["filterbycount"]["minfeature"],
                       "max_pct_mt": config["filterbycount"]["mito"]},
        "assumptions": {"species": [head["species"] if not several else
                                    "+".join(d["species"] for d in runs), species_note],
                        "chemistry": [head["chemistry"] if not several else
                                      "+".join(d["chemistry"] for d in runs), chem_note],
                        "doublet_rate": "; ".join(dict.fromkeys(
                            f"{settings['doublet']['rate'][d['chemistry']]} at "
                            f"{settings['doublet']['capacity']} cells per reaction"
                            for d in runs)),
                        "nreaction": [nreaction, "set by the caller" if args.get("nreaction") else
                                      "default 1 (not measured; confirm)"]},
        "ambient": {"applied": ambient, "compared": comparer or "none"},
        "doublets": {"decider": decider, "callers": ["doubletfinder", "scdblfinder"],
                     "removed_upstream": True},
        "warnings": warnings,
        "obs_sample_key": "sampleid",
        "layers": ["counts"],
        "counts_layer": counts_layer,
        "raw_slot": "log1p_normalized",
        "checkpoint": "adata_qc.h5ad",
        "figures": figures,
        "tables": ["tables/cellqc_metrics.csv"] if (tables / "cellqc_metrics.csv").is_file() else [],
        "cellqc_outputs": copied,
        "cellqc_result": {"n_files": len(cellqc_result["files"]),
                          "size_bytes": cellqc_result["size_bytes"],
                          "matrices": cellqc_result["matrices"],
                          "note": ("CellQC's complete result/ directory is kept with the run (the "
                                   "per-library .h5ad included): downloadable as one zip, and "
                                   "saveable to the user's data as a dataset. The bundle's result/ "
                                   "holds every file except the matrices.")},
        "read_from": (f"the bound folder{'s' if len(readable) > 1 else ''} "
                      f"({', '.join(_folder_name(f) for f, part in parts if part)})"),
        "raw_data_to_llm": False,
    }
    if several:
        out["checkpoints"] = checkpoints
    return out


def _species_groups(libs: list[dict[str, Any]], lib_species: "list[tuple[str, str]]", choice: str,
                    warnings: list[str]) -> "list[tuple[str, str, list[dict[str, Any]]]]":
    """The libraries as ``(species, how it was decided, libraries)`` groups, one CellQC run each.

    CellQC applies one gene set per run, and a species mismatch filters one species' cells with the
    other's mitochondrial genes (the 2026-10-02 test folder: human cells filtered on mouse "^mt-"),
    so it is never averaged. Libraries of different species are QC'd in separate runs instead: the
    two folders of RCHENLAB/AiScientist issue 22 were a human and a mouse library, and the run used
    to refuse them. The species of the FIRST library (the primary folder's) comes first. A library
    whose species cannot be read joins that first group, with a warning; a species set by the
    caller applies to every library."""
    if choice != "auto":
        return [(choice, "set by the caller", list(libs))]
    by_species: dict[str, list[dict[str, Any]]] = {}
    source: dict[str, str] = {}
    for lib, (sp, src) in zip(libs, lib_species):
        if sp:
            by_species.setdefault(sp, []).append(lib)
            source.setdefault(sp, src)
    if len(by_species) <= 1:
        species, src = _resolve(choice, lib_species, "human", "species", warnings)
        return [(species, src, list(libs))]
    unknown = [lib for lib, (sp, _src) in zip(libs, lib_species) if not sp]
    groups = [(sp, f"read from Cell Ranger output ({source[sp]})", members)
              for sp, members in by_species.items()]
    if unknown:
        groups[0][2].extend(unknown)
        warnings.append(f"Could not read the species of {', '.join(lib['sample'] for lib in unknown)}; "
                        f"QC'd with the {groups[0][0]} libraries. State this assumption, or set "
                        "`species` and pass `samples`.")
    return groups


def _run_cellqc_once(cellqc: str, libs: list[dict[str, Any]], staged: dict[str, Path], species: str,
                     chemistry: str, qc: Path, args: dict[str, Any],
                     settings: dict[str, Any]) -> dict[str, Any]:
    """Configure and run CellQC once, over ``libs`` (one species), in ``qc``: dry run, run, and what
    it wrote. ``{"error", "log"}`` when nothing usable came out."""
    for d in (qc / "home", qc / "tmp", qc / "cache"):
        d.mkdir(parents=True, exist_ok=True)
    out = qc / "out"
    nreaction = int(_p(_TOOL, "nreaction", args))
    comparer = str(_p(_TOOL, "ambient_compare", args) or "").strip()
    ambient = str(_p(_TOOL, "ambient_method", args))
    config = {
        "seed": int(_p(_TOOL, "seed", args)),
        "ambient": {"method": ambient,
                    "compare": [comparer] if comparer and comparer != ambient else []},
        "filterbycount": {"mincount": int(_threshold("min_counts", args, settings)),
                          "minfeature": int(_threshold("min_features", args, settings)),
                          "mito": float(_threshold("max_pct_mt", args, settings))},
        "geneset": settings["genesets"][species],
        "doublet": {"run": ["doubletfinder", "scdblfinder"],
                    "decider": str(_p(_TOOL, "doublet_decider", args)), "findpK": False, "pK": 0.01,
                    "numthreads": 5, "rate": settings["doublet"]["rate"][chemistry],
                    "capacity": int(settings["doublet"]["capacity"]),
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
        return {"error": "CellQC rejected the configuration (dry run failed): " + _tail(log, 1500)}
    rc = _run([*base_cmd, "--", str(qc / "samples.txt")], qc, env, log)
    if rc != 0 and "older modification time" in _tail(log, 20000):
        # An intermittent CellQC publish bug (hard-linked outputs keep their source's timestamp),
        # not a clock problem; the CellQC skill's remedy is a fresh output directory.
        shutil.rmtree(out, ignore_errors=True)
        rc = _run([*base_cmd, "--", str(qc / "samples.txt")], qc, env, log)

    result_dir = out / "result"
    manifest_rows = _read_csv(result_dir / "manifest.tsv", "\t")
    included = [r["sample"] for r in manifest_rows
                if str(r.get("included", "")).strip().lower() in ("true", "1", "yes")]
    h5ads = [result_dir / f"{s}.h5ad" for s in included if (result_dir / f"{s}.h5ad").is_file()]
    if not h5ads:
        return {"error": f"CellQC produced no usable library (exit {rc}): " + _tail(log, 2000),
                "log": str(log)}
    return {"rc": rc, "log": log, "config": config, "result_dir": result_dir,
            "manifest_rows": manifest_rows, "status_rows": _read_csv(result_dir / "qc_status.csv"),
            "metric_rows": _read_csv(result_dir / "metrics.csv"), "included": included,
            "h5ads": h5ads}


# The tables every CellQC run writes once for all its libraries: several species' runs are joined
# row by row, with the species named. Every other shared file (the report, its slides) keeps one
# copy per species, named after it; a library's own files carry its sample id already.
_COMBINED_TABLES = {"metrics.csv": ",", "manifest.tsv": "\t", "qc_status.csv": ","}


def _combine_results(runs: list[dict[str, Any]], dest: Path) -> Path:
    """Several species' CellQC ``result/`` directories as one flat directory in ``dest``."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    tables: dict[str, list[dict[str, str]]] = {name: [] for name in _COMBINED_TABLES}
    for done in runs:
        samples = done["included"] or [lib["sample"] for lib in done["libs"]]
        for src in sorted(p for p in done["result_dir"].iterdir() if p.is_file()
                          and not p.name.startswith(".")):
            if src.name in _COMBINED_TABLES:
                tables[src.name] += [{**row, "species": done["species"]}
                                     for row in _read_csv(src, _COMBINED_TABLES[src.name])]
            elif any(src.name.startswith(f"{s}.") or src.name.startswith(f"{s}_") for s in samples):
                _link_or_copy(src, dest / src.name)
            else:
                _link_or_copy(src, dest / f"{src.stem}_{done['species']}{src.suffix}")
    for name, rows in tables.items():
        if not rows:
            continue
        fields = list(dict.fromkeys(k for row in rows for k in row))
        with (dest / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, delimiter=_COMBINED_TABLES[name], restval="")
            w.writeheader()
            w.writerows(rows)
    return dest


def make_tool() -> HarnessTool:
    """The ``run_cellqc`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        _TOOL,
        "CellQC quality control for FOLDERS of 10x Cell Ranger outputs (one or more libraries with "
        "raw AND filtered matrices, in every folder bound to the run): SoupX ambient-RNA "
        "correction, count/feature/mitochondrial filtering, DoubletFinder doublet removal "
        "(scDblFinder as a second opinion), and the nuclear fraction from the BAM. Merges the "
        "libraries of all the bound folders in one call (obs 'sampleid') and writes the "
        "normalized checkpoint the later tools read. Use it INSTEAD of run_scanpy_qc when the data "
        "are Cell Ranger outputs; do not run run_doublet_detection after it.",
        _schema(_TOOL, samples={
            "type": "array", "items": {"type": "string"},
            "description": ("only these libraries, by sample id as the data profile lists them "
                            "(the dataset's name for a folder that is one library, else its "
                            "subfolder's name); empty = all")}),
        run_cellqc,
        reads_private_data=True, category="qc", requires=("scanpy",),
    )
