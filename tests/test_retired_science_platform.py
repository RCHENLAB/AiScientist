"""Retired science is unavailable; metadata and user result I/O remain functional."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiscientist.tools import api, worker_cli
from aiscientist.tools._lib import dataset_preflight as preflight


def test_text_preflight_is_metadata_only_and_preserves_input(tmp_path):
    path = tmp_path / "counts.tsv"
    path.write_text("cell_id\tcondition\tMT-A\tGeneB\na\tcontrol\t1\t2\nb\ttreated\t30\t40\n")
    original = path.read_bytes()
    result = api.run_dataset_smoke_analysis(path, tmp_path / "out")["result"]
    assert result["cells"] == result["sampled_rows"] == 2
    assert result["columns"] == ["cell_id", "condition", "MT-A", "GeneB"]
    assert result["metadata_columns"]["condition"]["values"] == ["control", "treated"]
    assert result["metadata_only"] and not result["truncated"]
    assert not ({"condition_effects", "qc_preview", "mean_mito_fraction", "mean_total_counts", "genes"} & result.keys())
    assert "Largest Condition Effects" not in (tmp_path / "out/dataset_summary.md").read_text()
    assert path.read_bytes() == original


def test_large_table_does_not_claim_complete_cell_count(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "TEXT_MAX_ROWS", 3)
    path = tmp_path / "large.csv"
    path.write_text("cell_id,condition,gene\n" + "".join(f"c{i},A,{i}\n" for i in range(8)))
    result = preflight.run_dataset_preflight(path, tmp_path / "out")["result"]
    assert result["sampled_rows"] == 3 and result["cells"] is None
    assert result["rows"] is None and result["truncated"]
    assert result["metadata_columns"]["condition"]["sampled"]


def test_byte_budget_bounds_input_read_and_preserves_uncertainty(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "TEXT_MAX_BYTES", 40)
    path = tmp_path / "large.csv"
    path.write_text("cell_id,gene\n" + "c,100\n" * 10000)
    result = preflight.run_dataset_preflight(path, tmp_path / "out")["result"]
    assert result["bytes_read"] == 40 and result["truncated"] and result["rows"] is None


def test_empty_and_quoted_tables_have_honest_counts(tmp_path):
    path = tmp_path / "query.csv"
    path.write_text('cell_id,condition\n"c\n1","A,B"\n')
    result = preflight.run_dataset_preflight(path, tmp_path / "out")["result"]
    assert result["rows"] == 1 and result["metadata_columns"]["condition"]["values"] == ["A,B"]
    path.write_text("")
    result = preflight.run_dataset_preflight(path, tmp_path / "empty")["result"]
    assert result["rows"] == 0 and not result["truncated"]


def test_platform_default_catalog_cannot_compute_retired_smoke_science():
    from aiscientist.agents.research_harness import default_catalog

    assert {tool.name for tool in default_catalog()} == {"finish"}
    assert not hasattr(api, "build_de_marker_execution")
    assert not hasattr(preflight, "run_single_cell_smoke")
    assert not hasattr(preflight, "run_spatial_smoke")


@pytest.mark.parametrize("module", ["aiscientist.tools.worker_cli", "aiscientist.tools.scrna_cli"])
def test_actual_cli_metadata_and_error_marker(tmp_path, module):
    path = tmp_path / "input.tsv"
    path.write_text("cell_id\tcondition\na\tA\n")
    command = [sys.executable, "-m", module, "--workspace", str(tmp_path / "run")]
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run(command + ["--tool", "preflight", "--dataset", str(path)],
                            text=True, capture_output=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.split(worker_cli.RESULT_MARKER)[-1])
    assert payload["result"]["metadata_only"]
    rejected = subprocess.run(command + ["--tool", "run_scanpy_qc"], text=True,
                              capture_output=True, timeout=30, env=env)
    assert rejected.returncode != 0
    assert json.loads(rejected.stdout.split(worker_cli.RESULT_MARKER)[-1])["status"] == "error"


def test_real_checkpoint_export_adopt_preserves_counts_and_provenance(tmp_path):
    ad = pytest.importorskip("anndata")
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    sp = pytest.importorskip("scipy.sparse")
    workspace = tmp_path / "run"
    (workspace / "work").mkdir(parents=True)
    matrix = sp.csr_matrix(np.array([[1, 0, 3], [0, 2, 1], [4, 3, 0]], dtype=np.float32))
    data = ad.AnnData(matrix.copy(), obs=pd.DataFrame(index=["a", "b", "c"]),
                     var=pd.DataFrame(index=["g1", "g2", "g3"]))
    data.layers["counts"] = matrix.copy()
    data.obs["cell_type"] = pd.Categorical(["Rod", "Cone", "Rod"])
    data.write_h5ad(workspace / "work/adata_qc.h5ad")
    source = workspace / "work/adata_qc.h5ad"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = worker_cli.run_tool(worker_cli.EXPORT_RESULT_DATASET, str(workspace), None,
                                  {"name": "query.processed.h5ad", "meta": {"run_id": "platform-test"}})
    assert result["status"] == "ok", result
    exported = workspace / "result/query.processed.h5ad"
    output = ad.read_h5ad(exported)
    np.testing.assert_array_equal(output.layers["counts"].toarray(), matrix.toarray())
    assert output.obs["cell_type"].astype(str).tolist() == ["Rod", "Cone", "Rod"]
    assert output.uns["aiscientist"]["run_id"] == "platform-test"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    next_run = tmp_path / "continued"
    adopted = worker_cli.run_tool(worker_cli.ADOPT_RESULT_DATASET, str(next_run), str(exported), {})
    assert adopted["status"] == "ok", adopted
    restored = ad.read_h5ad(next_run / "work/adata_qc.h5ad")
    np.testing.assert_array_equal(restored.layers["counts"].toarray(), matrix.toarray())
    assert restored.obs_names.tolist() == ["a", "b", "c"]


def test_generic_codeact_still_executes_in_isolated_auth_free_subprocess(tmp_path):
    from aiscientist.agents.sandbox import CodeSandbox

    sandbox = CodeSandbox(work_dir=str(tmp_path))
    result = sandbox("import os\nfrom pathlib import Path\np = Path(os.environ['AISCIENTIST_WORK'])/'sum.txt'\np.write_text(str(sum([2, 3, 5])))\nprint(p.read_text())")
    assert result["status"] == "ok", result
    assert "10" in result["stdout"]


def test_reference_panel_asset_is_unchanged_and_default_arguments_match():
    from aiscientist.agents import reference_panels, variant_settings

    path = Path(reference_panels.__file__).parent / "reference_data/ird_retnet.txt"
    expected = os.environ.get("AISCI_TEST_IRD_PANEL_SHA", "")
    if expected:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    genes = reference_panels.load_gene_panel("ird")
    assert api.load_gene_panel("retnet") == genes
    assert variant_settings.default_arguments({"VARIANT_DEFAULT_GENE_PANEL": "ird"})["genes"] == genes
    assert "genes" not in variant_settings.default_arguments({"VARIANT_DEFAULT_GENE_PANEL": "ird"}, {"genes": []})


def test_explicit_empty_native_gene_set_setting_disables_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", str(tmp_path))
    monkeypatch.setenv("SCRNA_GENESETS_DIR", "")
    assert api.genesets_dir() is None
    monkeypatch.setenv("SCRNA_GENESETS_DIR", str(tmp_path / "native"))
    assert api.genesets_dir() == tmp_path / "native"
