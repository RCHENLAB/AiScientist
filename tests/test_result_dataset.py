"""A run hands on a processed dataset, and the user can move it to their own folder.

Run f3b8268c4fd4 delivered a 39 MB bundle of report, figures and tables for a 16,750-cell library
and no dataset: the checkpoints were deleted once the report existed. Now the QC checkpoint (all
genes, raw counts) with the later steps' clusters, labels and embeddings is exported as
``<input>.processed.<time>.<run>.h5ad`` beside the run (not in artifacts, which HPC3 copies back
after every step), a pointer and a per-cell table go into artifacts, and "save to my data" moves the
file to the user's personal folder and lists it among their datasets.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
import types
from pathlib import Path

import pytest

from aiscientist.tools._lib.result_dataset import result_name


# --- naming ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("path,stem", [
    ("/x/CellQC_testdata/Sample1_WT/filtered_feature_bc_matrix.h5", "Sample1_WT"),
    ("/x/Sample2_Ddx41/outs/filtered_feature_bc_matrix.h5", "Sample2_Ddx41"),
    ("/u/yijus12/Ddx41_DEG.h5ad", "Ddx41_DEG"),
    # a run that started from an earlier run's result is named after the original input
    ("/t/run1/result/Sample1_WT.processed.20261007-2215.f3b8268c4fd4.h5ad", "Sample1_WT"),
    ("", "dataset"),
])
def test_the_name_says_what_it_came_from_that_it_is_processed_when_and_which_run(path, stem):
    name = result_name(path, "f3b8268c4fd4", dt.datetime(2026, 10, 7, 22, 15))
    assert name == f"{stem}.processed.20261007-2215.f3b8268c4fd4.h5ad"


# --- the export -------------------------------------------------------------------------------------

@pytest.fixture
def libs():
    """anndata & co. — absent from the offline CI subset, which must still run the other tests."""
    return types.SimpleNamespace(ad=pytest.importorskip("anndata"), np=pytest.importorskip("numpy"),
                                 pd=pytest.importorskip("pandas"), sp=pytest.importorskip("scipy.sparse"))


def _workspace(tmp_path: Path, libs, *, annotated: bool = True) -> Path:
    """QC (all 50 genes, counts layer) -> clustered (20 HVGs scaled, .raw full) -> annotated."""
    ad, np, pd, sp = libs.ad, libs.np, libs.pd, libs.sp
    rng = np.random.default_rng(0)
    ws = tmp_path / "run"
    (ws / "work").mkdir(parents=True)
    counts = sp.csr_matrix(rng.poisson(2, (100, 50)).astype(np.float32))
    qc = ad.AnnData(sp.csr_matrix(np.log1p(counts.toarray())))
    qc.var_names = [f"Gene{i}" for i in range(50)]
    qc.obs_names = [f"AAAC{i:04d}-1" for i in range(100)]
    qc.layers["counts"] = counts
    qc.obs["n_genes_by_counts"] = rng.integers(200, 900, 100)
    qc.obs["predicted_doublet"] = pd.Categorical(["singlet"] * 100)
    qc.raw = qc
    qc.write_h5ad(ws / "work" / "adata_qc.h5ad")

    cl = qc[:, :20].copy()
    cl.X = np.asarray(cl.X.todense())                       # scaled dense HVGs, as run_clustering
    cl.obs["leiden"] = pd.Categorical([str(i % 3) for i in range(100)])
    cl.obsm["X_pca"] = rng.normal(size=(100, 5)).astype(np.float32)
    cl.obsm["X_umap"] = rng.normal(size=(100, 2)).astype(np.float32)
    cl.write_h5ad(ws / "work" / "adata_clustered.h5ad")
    if annotated:
        an = cl.copy()
        an.obs["cell_type"] = an.obs["leiden"].map({"0": "Rod", "1": "Cone", "2": "MG"}).astype(str)
        an.write_h5ad(ws / "work" / "adata_annotated.h5ad")
    return ws


def test_the_export_keeps_all_genes_and_counts_and_adds_every_later_steps_results(tmp_path, libs):
    ad, pd = libs.ad, libs.pd
    from aiscientist.tools._lib.result_dataset import export
    ws = _workspace(tmp_path, libs)
    name = "Sample1_WT.processed.20261007-2215.f3b8268c4fd4.h5ad"
    out = export(ws, name, {"run_id": "f3b8268c4fd4", "question": "q", "location": "hpc3"})
    assert out["status"] == "ok"

    res = ad.read_h5ad(ws / "result" / name)
    assert res.shape == (100, 50)                           # every QC'd gene, not the 20 HVGs
    assert "counts" in res.layers and res.raw is None
    assert {"leiden", "cell_type", "n_genes_by_counts"} <= set(res.obs.columns)
    assert {"X_pca", "X_umap"} <= set(res.obsm.keys())
    assert res.uns["aiscientist"]["processed"] and res.uns["aiscientist"]["run_id"] == "f3b8268c4fd4"

    cells = pd.read_csv(ws / "artifacts" / "tables" / "cells.csv")
    assert len(cells) == 100 and {"barcode", "cell_type", "UMAP_1", "UMAP_2"} <= set(cells.columns)
    pointer = json.loads((ws / "artifacts" / "data" / "result_dataset.json").read_text())
    assert pointer["path"] == str(ws / "result" / name) and pointer["n_genes"] == 50
    assert pointer["saved_to"] is None and pointer["location"] == "hpc3"
    assert (ws / "result" / (name + ".json")).is_file()     # the record travels with the file
    assert not (ws / "artifacts" / "result").exists()       # the matrix is NOT in artifacts


def test_cell_types_come_from_the_label_table_when_no_checkpoint_has_them(tmp_path, libs):
    ad, pd = libs.ad, libs.pd
    from aiscientist.tools._lib.result_dataset import export
    ws = _workspace(tmp_path, libs, annotated=False)
    (ws / "artifacts" / "tables").mkdir(parents=True)
    pd.DataFrame({"cluster": ["0", "1", "2"], "cell_type": ["Rod", "Cone", "MG"]}).to_csv(
        ws / "artifacts" / "tables" / "cluster_cell_types.csv", index=False)
    name = "x.processed.20261007-2215.r1.h5ad"
    assert export(ws, name, {})["status"] == "ok"
    res = ad.read_h5ad(ws / "result" / name)
    assert set(res.obs["cell_type"]) == {"Rod", "Cone", "MG"}


def test_nothing_to_export_is_skipped_and_a_bad_name_is_refused(tmp_path, libs):
    from aiscientist.tools._lib.result_dataset import export
    (tmp_path / "empty" / "work").mkdir(parents=True)
    assert export(tmp_path / "empty", "a.processed.20261007-2215.r.h5ad", {})["status"] == "skipped"
    ws = _workspace(tmp_path, libs)
    assert export(ws, "../../evil.h5ad", {})["status"] == "error"


def test_the_hpc3_job_entry_point_runs_the_export(tmp_path, libs):
    from aiscientist.tools.api import EXPORT_RESULT_DATASET, run_analysis_tool
    ws = _workspace(tmp_path, libs)
    out = run_analysis_tool(EXPORT_RESULT_DATASET, str(ws), None,
                            {"name": "s.processed.20261007-2215.r2.h5ad", "meta": {"run_id": "r2"}})
    assert out["status"] == "ok" and out["result_dataset"]["n_cells"] == 100


# --- save to my data ------------------------------------------------------------------------------

@pytest.fixture
def gw():
    return pytest.importorskip("aiscientist.gateway.app")


def _client(gw_app):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def _seed_local_result(root: Path, owner: str, run_id: str) -> str:
    name = f"Sample1_WT.processed.20261007-2215.{run_id}.h5ad"
    res = root / owner / run_id / "result"
    res.mkdir(parents=True)
    (res / name).write_bytes(b"h5ad bytes")
    (res / (name + ".json")).write_text("{}")
    data = root / owner / run_id / "artifacts" / "data"
    data.mkdir(parents=True)
    (data / "result_dataset.json").write_text(json.dumps({
        "name": name, "path": str(res / name), "size_bytes": 10, "n_cells": 100, "n_genes": 50,
        "created_at": "2026-10-07T22:15:00+00:00", "location": "gateway", "kept_days": 7,
        "saved_to": None}))
    return name


def test_save_moves_a_local_result_into_the_users_data_and_lists_it(tmp_path, monkeypatch, gw):
    gw_app = gw
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: types.SimpleNamespace(username="yijus12", id=5))
    recorded: list = []
    monkeypatch.setattr(gw_app.auth_routes, "dataset_path_recorded", lambda uid, path: False)
    monkeypatch.setattr(gw_app.auth_routes, "record_dataset",
                        lambda *a, **kw: recorded.append(a) or 42)
    name = _seed_local_result(tmp_path, "yijus12", "abc123")

    listed = _client(gw_app).get("/api/result-datasets").json()["result_datasets"]
    assert [(r["run_id"], r["name"], r["saved_to"]) for r in listed] == [("abc123", name, None)]

    r = _client(gw_app).post("/api/result-datasets/abc123/save")
    assert r.status_code == 200 and r.json()["dataset_id"] == 42
    dest = tmp_path / "yijus12" / "uploads" / "processed" / name
    assert dest.read_bytes() == b"h5ad bytes" and (dest.parent / (name + ".json")).is_file()
    assert not (tmp_path / "yijus12" / "abc123" / "result" / name).exists()   # moved, not copied
    assert recorded == [(5, name, str(dest), 10, "h5ad")]
    pointer = json.loads((tmp_path / "yijus12" / "abc123" / "artifacts" / "data" / "result_dataset.json").read_text())
    assert pointer["saved_to"] == str(dest)
    # a second click reports it saved instead of failing
    assert _client(gw_app).post("/api/result-datasets/abc123/save").json()["already"] is True


def test_save_is_confined_to_the_callers_own_runs(tmp_path, monkeypatch, gw):
    gw_app = gw
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    _seed_local_result(tmp_path, "victim", "run9")
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: types.SimpleNamespace(username="attacker", id=7))
    assert _client(gw_app).post("/api/result-datasets/run9/save").status_code == 404
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: None)
    assert _client(gw_app).post("/api/result-datasets/run9/save").status_code == 401


class _FakeExec:
    def __init__(self, devices: str):
        self.devices, self.calls = devices, []

    def exec(self, cmd):
        self.calls.append(cmd)
        out = self.devices if cmd.startswith("mkdir -p") else ""
        return types.SimpleNamespace(ok=True, stdout=out, stderr="")


def _conn(devices: str):
    return types.SimpleNamespace(executor=_FakeExec(devices),
                                 settings=types.SimpleNamespace(cpu_partition="standard", cpu_account="ruic20_lab"))


def test_a_move_on_one_filesystem_is_a_rename_and_across_filesystems_a_slurm_job(gw):
    gw_app = gw
    same = _conn("2049 2049")
    ok, _ = gw_app._move_on_hpc(same, "/dfs3b/a/Temp/u/analysis/r/result/x.h5ad", "/dfs3b/lab/u/AiScientist_results/x.h5ad")
    assert ok and same.executor.calls[1].startswith("mv ")
    other = _conn("2049 2050")
    ok, _ = gw_app._move_on_hpc(other, "/dfs3b/a/x.h5ad", "/other/u/x.h5ad")
    assert ok and other.executor.calls[1].startswith("srun --partition=standard --account=ruic20_lab")


def test_an_unsaved_local_result_expires_with_the_checkpoints(tmp_path, gw):
    gw_app = gw
    import os
    import time
    old = tmp_path / "u" / "r1" / "result"
    old.mkdir(parents=True)
    f = old / "x.processed.20261001-0000.r1.h5ad"
    f.write_bytes(b"x")
    past = time.time() - 30 * 86400
    os.utime(f, (past, past))
    assert gw_app._expire_old_checkpoints(tmp_path, 7) == 1 and not old.exists()


def test_a_stopped_run_still_exports_its_dataset(tmp_path):
    # Run 7f2955e8e: after one Stop the write-up ran, but the export job inherited the Stop flag
    # and was cancelled ("Run cancelled by the user"). The flag is lifted for the export only.
    import asyncio
    from types import SimpleNamespace
    from aiscientist.gateway.app import _export_result_dataset

    seen = {}

    class Executor:
        should_cancel = staticmethod(lambda: True)

        def run_tool(self, name, args, ctx):
            seen["cancel_during"] = self.should_cancel
            return {"status": "skipped", "reason": "test"}

    ex = Executor()
    conn = SimpleNamespace(settings=SimpleNamespace(temp_ttl_days=3, checkpoint_ttl_days=7,
                                                     session_cleanup=True),
                           workspace=tmp_path, owner="u")
    asyncio.run(_export_result_dataset(conn, "r1", {"dataset_path": "/x/a.h5ad"}, ex, "q",
                                       lambda *a, **k: None))
    assert seen["cancel_during"] is None
    assert ex.should_cancel() is True                       # restored afterwards


def _export_conn(tmp_path):
    from types import SimpleNamespace
    return SimpleNamespace(settings=SimpleNamespace(temp_ttl_days=3, checkpoint_ttl_days=7,
                                                    session_cleanup=True),
                           workspace=tmp_path, owner="u")


def test_a_run_with_no_checkpoint_submits_no_export_job(tmp_path):
    # Run 0c070eb6307d (RCHENLAB/AiScientist issue 152, case A3): a gene-table lookup answered in a
    # minute, then waited 11 min 38 s in the HPC3 queue for the export job, which found nothing.
    import asyncio
    from aiscientist.gateway.app import _export_result_dataset

    said, jobs = [], []

    class Executor:
        should_cancel = None

        def wrote_checkpoint(self):
            return False

        def run_tool(self, name, args, ctx):
            jobs.append(name)
            return {"status": "skipped", "reason": "test"}

    out = asyncio.run(_export_result_dataset(_export_conn(tmp_path), "r1", {}, Executor(), "q",
                                             lambda *a, **k: said.append(a)))
    assert out is None and jobs == []
    assert said == [("info", "lab", "No processed dataset to export: the run wrote no analysis "
                                    "checkpoint (work/adata_*.h5ad) to export")]


@pytest.mark.parametrize("probe", [True, None])
def test_a_checkpoint_or_an_unanswered_probe_still_submits_the_export_job(tmp_path, probe):
    import asyncio
    from aiscientist.gateway.app import _export_result_dataset

    jobs = []

    class Executor:
        should_cancel = None

        def wrote_checkpoint(self):
            return probe

        def run_tool(self, name, args, ctx):
            jobs.append(name)
            return {"status": "skipped", "reason": "test"}

    asyncio.run(_export_result_dataset(_export_conn(tmp_path), "r1", {}, Executor(), "q",
                                       lambda *a, **k: None))
    assert len(jobs) == 1


@pytest.mark.parametrize("probe, response, outcome, reason", [
    (False, None, "skipped", "no_checkpoint"),
    (None, {"status": "skipped", "reason": "no output"}, "skipped", "exporter_skipped"),
    (True, {"status": "ok", "result_dataset": {"name": "delivered"}}, "success", None),
    (True, {"status": "ok"}, "failed", "missing_result"),
    (True, {"status": "error", "error": "/private/path"}, "failed", None),
    (True, OSError("private diagnostic"), "failed", None),
    (True, {"status": "cancelled"}, "cancelled", None),
    (True, {"status": "error", "queue_timeout": True}, "timeout", None),
])
def test_export_records_one_observation_and_preserves_its_return_value(tmp_path, probe, response, outcome, reason):
    from aiscientist.gateway import app, run_monitor

    path = tmp_path / "timing.jsonl"
    monitor = run_monitor.RunMonitor(run_id="r1")
    monitor.bind_timing(path)
    during_probe = []

    class Executor:
        def wrote_checkpoint(self):
            during_probe.extend(run_monitor.read_timing(path)["spans"])
            return probe

        def run_tool(self, *args):
            assert probe is not False
            if isinstance(response, Exception):
                raise response
            return response

    with run_monitor.bind_monitor(monitor):
        output = asyncio.run(app._export_result_dataset(_export_conn(tmp_path), "r1", {}, Executor(),
                                                       "private question", lambda *args: None))
    if isinstance(response, dict) and response.get("status") == "ok":
        assert output == response.get("result_dataset", {})
    else:
        assert output is None
    in_flight, = during_probe
    assert in_flight["stage"] == "result_export" and in_flight["partial"]
    span, = run_monitor.read_timing(path)["spans"]
    assert (span["stage"], span["category"], span["outcome"]) == ("result_export", "observation", outcome)
    assert span["started_at"] and span["ended_at"] and span["duration_seconds"] >= 0 and not span["partial"]
    assert span["metadata"]["backend"] == "hpc3"
    assert span["metadata"].get("reason_code") == reason
    assert "private" not in path.read_text()


def test_cancelling_export_while_waiting_for_probe_keeps_partial_history(tmp_path):
    from aiscientist.gateway import app, run_monitor

    path = tmp_path / "timing.jsonl"
    monitor = run_monitor.RunMonitor(run_id="r1")
    monitor.bind_timing(path)
    release = threading.Event()

    async def exercise():
        loop = asyncio.get_running_loop()
        probing = asyncio.Event()

        def probe():
            loop.call_soon_threadsafe(probing.set)
            release.wait(timeout=10)
            return False

        with run_monitor.bind_monitor(monitor):
            task = asyncio.create_task(app._export_result_dataset(
                _export_conn(tmp_path), "r1", {}, types.SimpleNamespace(wrote_checkpoint=probe),
                "q", lambda *args: None))
            try:
                await asyncio.wait_for(probing.wait(), timeout=5)
                monitor.end("cancelled")
                span, = run_monitor.read_timing(path)["spans"]
                assert span["partial"] and span["outcome"] == "unknown"
                assert span["duration_seconds"] is None and span["ended_at"] is None
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()

    asyncio.run(exercise())
    span, = run_monitor.read_timing(path)["spans"]
    assert span["outcome"] == "cancelled" and not span["partial"] and span["ended_at"]


class _Remote:
    def __init__(self, stdout="", status=0, fail=False):
        self.stdout, self.status, self.fail, self.commands = stdout, status, fail, []

    def exec(self, command, timeout=60.0):
        from aiscientist.gateway.executor import ExecResult
        self.commands.append(command)
        if self.fail:
            raise OSError("ssh dropped")
        return ExecResult(command=command, exit_status=self.status, stdout=self.stdout, stderr="")


@pytest.mark.parametrize("remote, expected", [
    (_Remote("checked\n"), False),
    (_Remote("found\nchecked\n"), True),
    (_Remote("", status=255), None),          # the command did not run
    (_Remote("garbage"), None),               # not the probe's own answer
    (_Remote(fail=True), None),               # the link failed
])
def test_the_checkpoint_probe_reads_the_run_workspace_over_ssh(remote, expected):
    from aiscientist.gateway.slurm_analysis import SlurmAnalysisExecutor
    ex = SlurmAnalysisExecutor(remote=remote, container_image="img", remote_workspace="/dfs3b/u/run 1/",
                               scratch_dir="/dfs3b/u/s")
    assert ex.wrote_checkpoint() is expected
    assert "'/dfs3b/u/run 1/work'/adata_*.h5ad" in remote.commands[0]
    assert "sbatch" not in remote.commands[0]


def test_the_checkpoint_probe_needs_a_remote_workspace():
    from aiscientist.gateway.slurm_analysis import SlurmAnalysisExecutor
    assert SlurmAnalysisExecutor(remote=None, container_image="img", scratch_dir="/s").wrote_checkpoint() is None
