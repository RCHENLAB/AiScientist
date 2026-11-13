"""Bounded publication metadata for both report writers; never dereference result paths."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re


def delivery_evidence(art: Path) -> str:
    """Describe only this run's gateway-stamped pointers, not fresh data verification."""
    run_id, owner = art.parent.name, art.parent.parent.name

    def short_text(value: object, limit: int = 240) -> str:
        raw = str(value).encode("utf-8")
        return raw[:limit].decode("utf-8", errors="ignore") + ("…" if len(raw) > limit else "")

    def read(name: str) -> dict:
        path = art / "data" / name
        try:
            if art.is_symlink() or (art / "data").is_symlink() or path.is_symlink():
                return {}
            if not path.is_file() or path.stat().st_size > 1_000_000:
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("run_id") != run_id or value.get("owner") != owner:
                return {}
            return value
        except (OSError, ValueError):
            return {}

    def name_ok(name: object) -> bool:
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 512 or "\\" in name:
            return False
        path = PurePosixPath(name)
        return not path.is_absolute() and all(p not in {"", ".", ".."} for p in name.split("/"))

    primary = read("result_dataset.json")
    records = primary.get("datasets") or ([primary] if primary else [])
    datasets = []
    for value in records[:20] if isinstance(records, list) else []:
        if not isinstance(value, dict):
            continue
        species = str(value.get("species") or "")
        if primary.get("datasets") and re.fullmatch(r"[A-Za-z0-9_-]+", species):
            child_name = f"result_dataset.{species}.json"
            child = art / "data" / child_name
            if child.exists() or child.is_symlink():
                value = read(child_name)
                if not value or value.get("species") != species:
                    continue
        if value.get("run_id") != run_id or value.get("owner") != owner or not name_ok(value.get("name")):
            continue
        record = {"name": value["name"], "species": short_text(species, 64)}
        for key in ("n_cells", "n_genes", "size_bytes"):
            number = value.get(key)
            if isinstance(number, int) and not isinstance(number, bool) and 0 <= number < 10**18:
                record[key] = number
        for key in ("counts_layer", "cells_table"):
            if isinstance(value.get(key), str):
                record[key] = short_text(value[key])
        datasets.append(record)
        datasets[-1]["publication"] = (
            "Saved to my data (recorded by the gateway)" if value.get("saved_to")
            else "Kept beside this run (not recorded as saved to my data)")
        datasets[-1]["recorded_location"] = short_text(value.get("location"), 32)

    cellqc = read("cellqc_result.json")
    output = {"processed_datasets": datasets, "processed_dataset_count": len(datasets),
              "processed_dataset_preview_omitted": 0,
              "additional_dataset_records_not_examined": max(0, len(records) - 20) if isinstance(records, list) else 0}
    if cellqc:
        files = cellqc.get("files")
        if isinstance(files, list) and files and len(files) <= 10000 and all(
            isinstance(row, dict) and name_ok(row.get("name"))
            and isinstance(row.get("size"), int) and row["size"] >= 0 for row in files
        ):
            names = {row["name"] for row in files}
            matrices = cellqc.get("matrices") or []
            omitted = cellqc.get("not_in_bundle") or []
            matrices = matrices if isinstance(matrices, list) else []
            omitted = omitted if isinstance(omitted, list) else []
            valid_matrices = [name for name in matrices if isinstance(name, str) and name in names]
            valid_omitted = [name for name in omitted if isinstance(name, str) and name in names]
            output["complete_cellqc_result"] = {
                "name": short_text(cellqc.get("name")), "recorded_file_count": len(files),
                "recorded_size_bytes": sum(row["size"] for row in files),
                "recorded_matrices": valid_matrices[:8],
                "recorded_matrix_count": len(valid_matrices),
                "recorded_matrix_preview_omitted": max(0, len(valid_matrices) - 8),
                "publication": "Saved to my data (recorded by the gateway)" if cellqc.get("saved_to")
                    else "Kept beside this run (not recorded as saved to my data)",
                "download_channel": "CellQC results (.zip)",
                "complete_zip_contract": "The dedicated CellQC ZIP serves every file in this complete inventory, including its per-library matrices.",
                "ordinary_results_zip": "Results (.zip) is a different report/artifact bundle; its not_in_bundle list names files omitted only from that ordinary bundle.",
                "omitted_from_ordinary_bundle": valid_omitted[:8],
                "ordinary_bundle_excluded_count": len(valid_omitted),
                "ordinary_bundle_excluded_preview_omitted": max(0, len(valid_omitted) - 8),
            }
    if not datasets and "complete_cellqc_result" not in output:
        return ""
    prefix = "Current gateway publication records (read-only metadata, not a new file-content or availability check):\n"
    suffix = ("\nThe per-species processed datasets, temporary adata_*.h5ad work checkpoints, and per-library "
            "matrices inside the complete CellQC result are separate products. Missing a temporary checkpoint "
            "from a ZIP does not show that a published processed dataset is missing. Do not infer current "
            "checkpoint existence or deletion from these publication records. Saved state is recorded, "
            "not freshly verified by this report. Complete unabridged pointers remain under data/; string and list previews are bounded.\n\n")
    lists = [(output, "processed_datasets", "processed_dataset_preview_omitted")]
    if "complete_cellqc_result" in output:
        qc = output["complete_cellqc_result"]
        lists.extend([(qc, "recorded_matrices", "recorded_matrix_preview_omitted"),
                      (qc, "omitted_from_ordinary_bundle", "ordinary_bundle_excluded_preview_omitted")])
    while True:
        text = prefix + json.dumps(output, ensure_ascii=False) + suffix
        if len(text.encode("utf-8")) <= 8000:
            return text
        choices = [(parent, key, omitted) for parent, key, omitted in lists if parent[key]]
        if not choices:  # scalar fields are bounded above; fail closed if the contract changes
            return ""
        parent, key, omitted = max(choices, key=lambda item: len(json.dumps(item[0][item[1]], ensure_ascii=False).encode("utf-8")))
        parent[key].pop()
        parent[omitted] += 1
