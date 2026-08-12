#!/usr/bin/env python3
"""Convert a real .h5ad single-cell dataset into the cells-as-rows CSV that the
AiScientist pipeline runs FULL QC + DE on.

Why: the pipeline's .h5ad path only does a metadata *preflight* (shape/structure);
the numeric QC + per-gene differential-expression compute (run_single_cell_smoke)
runs on the CSV format — cells as rows, a `condition` column, gene columns. This
script gives you REAL expression values from REAL cells in that format, so you can
exercise the full numeric path on a realistic dataset.

The `condition` split is SYNTHETIC (cells split into two groups by total counts)
and labelled as such — it is a pipeline stress-test, not a real biological contrast.

Deps: h5py + numpy only (already in the app venv) — no scanpy/anndata needed.

Usage (on the eye server, with the app venv):
    /data/BioAgent/env/bin/python h5ad_to_csv.py INPUT.h5ad OUTPUT.csv \
        [--cells 400] [--genes 40] [--seed 0]
"""
from __future__ import annotations

import argparse
import csv

import h5py
import numpy as np


def _decode(values) -> list[str]:
    out = []
    for v in values:
        out.append(v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v))
    return out


def _read_index(node) -> list[str]:
    """Read obs/var names across AnnData formats.

    - Newer AnnData: obs/var is a *Group* with an `_index` attr (or an `index`
      dataset).
    - Older AnnData (e.g. the classic pbmc3k_raw.h5ad): obs/var is a *structured*
      (compound-dtype) Dataset; the names live in its `index` field.
    """
    if isinstance(node, h5py.Group):
        if "_index" in node.attrs:
            key = node.attrs["_index"]
            key = key.decode() if isinstance(key, bytes) else key
            if key in node:
                return _decode(node[key][:])
        for key in ("_index", "index"):
            if key in node and isinstance(node[key], h5py.Dataset):
                return _decode(node[key][:])
        for key in node:
            sub = node[key]
            if isinstance(sub, h5py.Dataset) and sub.ndim == 1:
                return _decode(sub[:])
        raise SystemExit(f"could not locate an index in group {node.name!r}")

    # Old format: a structured/compound Dataset — pull the index field.
    arr = node[:]
    fields = arr.dtype.names
    if fields:
        field = "index" if "index" in fields else fields[0]
        return _decode(arr[field])
    return _decode(arr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="input .h5ad")
    ap.add_argument("output", help="output .csv (cells-as-rows)")
    ap.add_argument("--cells", type=int, default=400, help="number of cells to sample (default 400)")
    ap.add_argument("--genes", type=int, default=40, help="number of top-expressed genes to keep (default 40)")
    ap.add_argument("--seed", type=int, default=0, help="random seed for cell sampling")
    args = ap.parse_args(argv)

    with h5py.File(args.input, "r") as h:
        if "X" not in h:
            raise SystemExit("no X matrix in this .h5ad")
        var_names = np.array(_read_index(h["var"]))
        obs_names = np.array(_read_index(h["obs"]))
        x = h["X"]

        if isinstance(x, h5py.Group):  # sparse (CSR is the AnnData default)
            enc = x.attrs.get("encoding-type", x.attrs.get("h5sparse_format", b"csr"))
            enc = enc.decode() if isinstance(enc, bytes) else enc
            if "csc" in str(enc).lower():
                raise SystemExit("CSC matrices aren't supported by this helper; re-save as CSR or dense.")
            data = x["data"][:]
            indices = x["indices"][:]
            indptr = x["indptr"][:]
            # shape attr name varies across AnnData versions; infer if absent.
            shape_attr = x.attrs.get("shape", x.attrs.get("h5sparse_shape"))
            if shape_attr is not None:
                n_cells, n_genes = int(shape_attr[0]), int(shape_attr[1])
            else:
                n_cells = len(indptr) - 1
                n_genes = int(indices.max()) + 1 if indices.size else 0
            # per-gene total (for top-gene pick) and per-cell total (for the split)
            gene_total = np.zeros(n_genes, dtype=np.float64)
            np.add.at(gene_total, indices, data)
            cell_total = np.add.reduceat(data, indptr[:-1]) if data.size else np.zeros(n_cells)
            # reduceat mishandles empty rows; recompute cleanly
            cell_total = np.array([data[indptr[i]:indptr[i + 1]].sum() for i in range(n_cells)])
            sparse = True
        else:  # dense
            mat = np.asarray(x[:], dtype=np.float64)
            n_cells, n_genes = mat.shape
            gene_total = mat.sum(axis=0)
            cell_total = mat.sum(axis=1)
            sparse = False

    n_genes_keep = min(args.genes, n_genes)
    sel_genes = np.argsort(gene_total)[::-1][:n_genes_keep]
    n_cells_keep = min(args.cells, n_cells)
    rng = np.random.default_rng(args.seed)
    sel_cells = np.sort(rng.choice(n_cells, size=n_cells_keep, replace=False))

    # synthetic condition: split the SAMPLED cells at their median total count
    sampled_total = cell_total[sel_cells]
    median = float(np.median(sampled_total))

    if sparse:
        gene_pos = {int(g): j for j, g in enumerate(sel_genes)}
        sub = np.zeros((n_cells_keep, n_genes_keep), dtype=np.float64)
        for r, i in enumerate(sel_cells):
            s, e = int(indptr[i]), int(indptr[i + 1])
            for col, val in zip(indices[s:e], data[s:e]):
                j = gene_pos.get(int(col))
                if j is not None:
                    sub[r, j] = val
    else:
        sub = mat[np.ix_(sel_cells, sel_genes)]

    gene_headers = _dedupe(_decode(var_names[sel_genes]))
    with open(args.output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell_id", "condition", *gene_headers])
        for r, i in enumerate(sel_cells):
            cond = "high_count" if sampled_total[r] >= median else "low_count"
            cid = obs_names[i] if i < len(obs_names) else f"cell{i}"
            # integerize counts for a clean count-matrix look
            row = [int(round(v)) for v in sub[r]]
            w.writerow([cid, cond, *row])

    print(f"wrote {args.output}: {n_cells_keep} cells x {n_genes_keep} genes "
          f"(condition = synthetic high/low total-count split at median {median:.0f})")
    return 0


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}.{seen[n]}")
        else:
            seen[n] = 0
            out.append(n)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
