"""Publication evidence separates saved species datasets and the two ZIP products."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from aiscientist.gateway import app as gw
from aiscientist.reporting.delivery import delivery_evidence


@pytest.fixture
def published(tmp_path):
    art = tmp_path / "tester" / "a1608e696d7b" / "artifacts"
    data = art / "data"
    data.mkdir(parents=True)
    def dataset(species, cells, genes):
        return {"owner": "tester", "run_id": "a1608e696d7b", "species": species,
            "name": f"sample.{species}.processed.20261113-0153.a1608e696d7b.h5ad",
            "n_cells": cells, "n_genes": genes, "counts_layer": "SoupX-corrected integer UMI counts",
            "location": "gateway", "saved_to": None}
    mouse, human = dataset("mouse", 29878, 33696), dataset("human", 4396, 38606)
    primary = {**mouse, "datasets": copy.deepcopy([mouse, human])}
    (data / "result_dataset.json").write_text(json.dumps(primary))
    for row in (mouse, human):
        row["saved_to"] = "/private/datasets/" + row["name"]
        (data / f"result_dataset.{row['species']}.json").write_text(json.dumps(row))
    files = [{"name": "Sample1.h5ad", "size": 300}, {"name": "Sample2.h5ad", "size": 200},
             {"name": "Sample3.h5ad", "size": 100}, {"name": "metrics.csv", "size": 40}]
    qc = {"owner": "tester", "run_id": "a1608e696d7b", "name": "sample.cellqc",
          "files": files, "matrices": [x["name"] for x in files[:3]],
          "not_in_bundle": [x["name"] for x in files[:3]], "saved_to": "/private/datasets/cellqc"}
    (data / "cellqc_result.json").write_text(json.dumps(qc))
    return art


def test_current_species_pointers_and_complete_zip_contract_are_distinct(published):
    before = {p: p.read_bytes() for p in (published / "data").iterdir()}
    text = delivery_evidence(published)
    assert "human.processed" in text and "mouse.processed" in text
    assert "4396" in text and "29878" in text
    assert text.count("Saved to my data (recorded by the gateway)") == 3
    assert "CellQC results (.zip)" in text and "Results (.zip) is a different" in text
    assert '"recorded_matrices": ["Sample1.h5ad", "Sample2.h5ad", "Sample3.h5ad"]' in text
    assert '"recorded_file_count": 4' in text and '"recorded_size_bytes": 640' in text
    assert "does not show that a published processed dataset is missing" in text
    assert "not a new file-content or availability check" in text
    assert "/private/" not in text and "deleted when" not in text
    assert all(path.read_bytes() == value for path, value in before.items())


@pytest.mark.parametrize("builder", ["main", "technical"])
def test_both_real_writer_prompts_receive_all_current_publication_records(published, builder):
    seen = []
    def complete(messages, **kwargs):
        seen.append(messages)
        return "# Report\n\nCurrent publication evidence was provided."
    result = SimpleNamespace(question="QC", rounds=[], agenda=[], final_answer="Scientific result")
    if builder == "main":
        gw._build_report(result.final_answer, published, complete, result.question, result)
    else:
        gw._build_technical_report(result, published, complete, result.question, is_revision=True)
    assert len(seen) == 1
    prompt = seen[0][1]["content"]
    assert "human.processed" in prompt and "mouse.processed" in prompt
    assert "Saved to my data" in prompt and "CellQC results (.zip)" in prompt
    assert "deleted when the run is published" not in prompt


@pytest.mark.parametrize("fault", ["owner", "run_id", "symlink"])
def test_invalid_current_species_pointer_does_not_fall_back_to_stale_parent(published, fault):
    path = published / "data/result_dataset.human.json"
    if fault == "symlink":
        other = published.parent / "outside.json"
        path.rename(other)
        path.symlink_to(other)
    else:
        value = json.loads(path.read_text())
        value[fault] = "another"
        path.write_text(json.dumps(value))
    text = delivery_evidence(published)
    assert "human.processed" not in text and "mouse.processed" in text


def test_unstamped_or_missing_records_do_not_claim_published_products(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "result_dataset.json").write_text(json.dumps({"name": "unverified.h5ad", "saved_to": "/outside"}))
    (data / "cellqc_result.json").write_text(json.dumps({"not_in_bundle": ["missing.h5ad"]}))
    assert delivery_evidence(tmp_path) == ""


def test_technical_writer_failure_keeps_delivery_evidence(published):
    result = SimpleNamespace(question="QC", rounds=[], agenda=[], final_answer="Scientific result")
    def fail(*args, **kwargs):
        raise RuntimeError("writer unavailable")
    report = gw._build_technical_report(result, published, fail, result.question, is_revision=True)
    assert "human.processed" in report and "mouse.processed" in report
    assert "CellQC results (.zip)" in report


def test_large_valid_matrix_inventory_is_bounded_without_losing_totals(published):
    path = published / "data/cellqc_result.json"
    value = json.loads(path.read_text())
    names = [f"sample_{i:05d}_" + "x" * 20 + ".h5ad" for i in range(5000)]
    value.update(files=[{"name": n, "size": 1} for n in names], matrices=names, not_in_bundle=names)
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    assert len(before) < 1_000_000
    text = delivery_evidence(published)
    assert len(text.encode()) <= 8000
    qc = json.loads(text.splitlines()[1])["complete_cellqc_result"]
    assert qc["recorded_file_count"] == qc["recorded_matrix_count"] == 5000
    assert qc["ordinary_bundle_excluded_count"] == 5000
    assert len(qc["recorded_matrices"]) <= 8
    assert len(qc["recorded_matrices"]) + qc["recorded_matrix_preview_omitted"] == 5000
    assert len(qc["omitted_from_ordinary_bundle"]) + qc["ordinary_bundle_excluded_preview_omitted"] == 5000
    assert path.read_bytes() == before


def test_same_run_child_cannot_change_its_species(published):
    path = published / "data/result_dataset.human.json"
    value = json.loads(path.read_text())
    value["species"] = "mouse"
    path.write_text(json.dumps(value))
    assert "human.processed" not in delivery_evidence(published)


def test_large_unicode_metadata_cannot_exceed_context_byte_bound(published):
    path = published / "data/result_dataset.mouse.json"
    value = json.loads(path.read_text())
    value["counts_layer"] = "科学" * 10000
    value["location"] = "location" * 10000
    path.write_text(json.dumps(value, ensure_ascii=False))
    text = delivery_evidence(published)
    assert len(text.encode()) <= 8000
    assert "previews are bounded" in text and "…" in text
