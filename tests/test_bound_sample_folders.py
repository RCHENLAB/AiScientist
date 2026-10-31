"""Several Cell Ranger folders bound to one run (RCHENLAB/AiScientist issue 22).

Two folders attached to one chat, ``Sample3_GSM5676874/`` and ``Sample1_WT/``: the run log read
"Cell Ranger delivery: 1 library (Sample3_GSM5676874)", "Bound extra file: Sample1_WT/" and
"Sample1_WT/: A unknown file (~4.1 KB)", and the plan scoped CellQC to Sample3. Sample1_WT was whole
on the server (21 GB) and its session copy on HPC3 was complete: the gateway read only the PRIMARY
folder as a Cell Ranger delivery and took every other bound path for one file (4 KB is a directory's
own size), and run_cellqc read only the primary's folder. Sample3 had also come from its HPC3
original, which the chat still named after the dataset was moved to the server.

Now every bound folder is a sample folder: one layout covers the libraries of all of them, each
named after its dataset (a folder kept on the server is ``…/<uuid>/original``, so both were
"original"), run_cellqc reads them all, a dataset that cannot be used is named to the planner with
the reason, and an HPC3 original is read from its copy on the server.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from aiscientist.tools._lib.cellranger import (cellranger_layout_hint, describe_cellranger_layout,
                                               merge_cellranger_layouts)
from test_kept_storage import LAB, SHARED, FakeHpc, _live, kept  # noqa: F401 - kept is a fixture
from test_run_cellqc import _web_summary, _write_10x_h5, fake_cellqc  # noqa: F401 - a fixture
from test_tus_upload import env  # noqa: F401 - a fixture kept needs

_LIBRARY = ["raw_feature_bc_matrix.h5", "filtered_feature_bc_matrix.h5"]


def _library(folder: Path, *, matrix: bool = True, genome: str = "GRCm39") -> Path:
    """A Cell Ranger library AS the folder, the way a 10x Cloud download is uploaded."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "raw_feature_bc_matrix.h5").write_bytes(b"")
    if matrix:
        _write_10x_h5(folder / "filtered_feature_bc_matrix.h5", genome=genome)
    else:
        (folder / "filtered_feature_bc_matrix.h5").write_bytes(b"")
    (folder / "metrics_summary.csv").write_text("Estimated Number of Cells\n4\n")
    (folder / "web_summary.html").write_text(_web_summary("Single Cell 3' v3 (polyA)", "GRCm39-2024-A"))
    return folder


# --- one layout over every bound folder --------------------------------------------------------------


def test_two_folders_kept_on_the_server_are_two_samples_named_after_their_datasets():
    a = describe_cellranger_layout(_LIBRARY, f"/data/users/u/datasets/{uuid4()}/original")
    b = describe_cellranger_layout(_LIBRARY, f"/data/users/u/datasets/{uuid4()}/original")
    assert a["libraries"][0]["sample"] == b["libraries"][0]["sample"] == "original"  # all a path says
    merged = merge_cellranger_layouts([("Sample3_GSM5676874", a), ("Sample1_WT", b), ("notes", None)])
    assert [lib["sample"] for lib in merged["libraries"]] == ["Sample3_GSM5676874", "Sample1_WT"]
    assert [lib["dataset"] for lib in merged["libraries"]] == ["Sample3_GSM5676874", "Sample1_WT"]
    assert merged["n_libraries"] == 2 and merged["datasets"] == ["Sample3_GSM5676874", "Sample1_WT"]
    hint = cellranger_layout_hint(merged)
    assert "2 libraries (Sample3_GSM5676874, Sample1_WT) in 2 attached folders" in hint
    assert "plan ONE `run_cellqc` over every library" in hint


def test_a_cohort_folder_keeps_its_subfolders_names_and_a_repeated_name_is_numbered():
    cohort = describe_cellranger_layout(
        [f"S1/{n}" for n in _LIBRARY] + [f"S2/outs/{n}" for n in _LIBRARY], "/up/cohort")
    alone = describe_cellranger_layout(_LIBRARY, "/up/x/original")
    other = describe_cellranger_layout(_LIBRARY, "/up/y/original")
    merged = merge_cellranger_layouts([("cohort", cohort), ("S1", alone), ("WT sample #2/", other)])
    assert [lib["sample"] for lib in merged["libraries"]] == ["S1", "S2", "S1_2", "WT_sample_2"]
    # One folder and no dataset name: the directory's names, as before.
    assert merge_cellranger_layouts([("", alone)])["libraries"][0]["sample"] == "original"
    assert merge_cellranger_layouts([("a", None)]) is None
    assert "attached folders" not in cellranger_layout_hint(merge_cellranger_layouts([("S1", alone)]))


def test_parent_and_child_bindings_qc_each_real_library_once(tmp_path):
    parent = tmp_path / "CellQC_testdata"
    paths = [f"{sample}/{n}" for sample in ("Sample1_WT", "Sample2_Ddx41", "Sample3")
             for n in _LIBRARY]
    cohort = describe_cellranger_layout(paths, parent.as_posix())
    child = describe_cellranger_layout(_LIBRARY, (parent / "Sample1_WT").as_posix())
    for parts in ([("CellQC_testdata", cohort), ("Sample1_WT", child)],
                  [("Sample1_WT", child), ("CellQC_testdata", cohort)]):
        merged = merge_cellranger_layouts(parts)
        assert merged["n_libraries"] == 3
        assert sorted(lib["sample"] for lib in merged["libraries"]) == [
            "Sample1_WT", "Sample2_Ddx41", "Sample3"]
        assert len(merged["notes"]) == 1
        assert "Sample1_WT was bound twice" in merged["notes"][0]
        assert "QC'd once" in cellranger_layout_hint(merged)


def test_library_deduplication_resolves_directory_aliases(tmp_path):
    root = tmp_path / "sample"
    root.mkdir()
    a = describe_cellranger_layout(_LIBRARY, root.as_posix())
    b = describe_cellranger_layout(_LIBRARY, (root / ".." / "sample").as_posix())
    merged = merge_cellranger_layouts([("first", a), ("alias", b)])
    assert merged["n_libraries"] == 1
    assert "QC'd once" in merged["notes"][0]


def test_multispecies_plan_hint_keeps_surviving_barcodes_in_separate_objects():
    layout = describe_cellranger_layout(
        [f"{s}/{n}" for s in ("mouse", "human") for n in _LIBRARY], "/cohort")
    for lib in layout["libraries"]:
        genome = "GRCm39" if lib["sample"] == "mouse" else "GRCh38"
        lib["matrix"] = {"genomes": {genome: 3}, "dtype": "int32", "n_barcodes": 4,
                         "n_features": 3, "format": "10x_h5"}
    hint = cellranger_layout_hint(layout)
    assert "separate objects, never into one object across species" in hint
    assert "State this split in the plan" in hint


# --- the run start: what the planner is told --------------------------------------------------------


def test_two_bound_folders_reach_the_plan_as_two_cell_ranger_samples(kept, tmp_path, monkeypatch):  # noqa: F811 - a fixture imported above
    """Through _run_lab, up to the lab it builds: what the planner and the tools are handed."""
    pytest.importorskip("h5py")
    _client, _conn, gw = kept
    from aiscientist.agents import research_lab as rl
    from aiscientist.agents.research_lab import LabResult
    from aiscientist.gateway import dataset_storage
    from aiscientist.gateway.settings import HPCSettings

    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    sample3 = _library(dataset_storage.dataset_dir(str(uuid4()), "tester") / "original")
    sample1 = _library(dataset_storage.dataset_dir(str(uuid4()), "tester") / "original")
    seen: dict = {}

    class _Lab:
        def __init__(self, ctx, *a, **k):
            seen["decisions"] = ctx.decisions
            self.guidance = None

        def run(self, question, on_event=None, plan_review=None, should_cancel=None,
                pull_injections=None, resume=None, decision_review=None, **_kw):
            if on_event:
                on_event({"type": "plan_cancelled"})
            return LabResult(question, ["QC"], [], False, 0, "cancelled during review")

    monkeypatch.setattr(rl, "ResearchLab", _Lab)
    monkeypatch.setattr(gw, "_build_report", lambda *a, **k: None)

    def _no_model(*a, **k):
        raise ConnectionError("no model in this test")
    monkeypatch.setattr(gw.vllm_client, "complete", _no_model)

    conn = gw.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace, conn.status, conn.executor, conn.alloc = tmp_path / "ws", "ready", object(), object()
    gw.CONNECTIONS[conn.id] = conn
    published: list = []
    monkeypatch.setattr(conn, "_publish", lambda payload: published.append(dict(payload)))
    try:
        req = gw.LabRequest(connection_id=conn.id, question="Run QC on these two samples",
                            conversation_id="conv-1", plan_mode=True,
                            datasets=[{"path": str(sample3), "name": "Sample3_GSM5676874/"},
                                      {"path": str(sample1), "name": "Sample1_WT/"}])
        conn.begin_run("conv-1")
        asyncio.run(gw._run_lab(conn, req))
    finally:
        gw.CONNECTIONS.pop(conn.id, None)

    decisions = seen["decisions"]
    layout = decisions["cellranger_layout"]
    assert [lib["sample"] for lib in layout["libraries"]] == ["Sample3_GSM5676874", "Sample1_WT"]
    assert decisions["input_folders"] == [
        {"name": "Sample3_GSM5676874", "path": str(sample3), "hpc": ""},
        {"name": "Sample1_WT", "path": str(sample1), "hpc": ""}]
    assert "2 libraries (Sample3_GSM5676874, Sample1_WT)" in decisions["dataset_result"]["input_layout_hint"]
    second = decisions["datasets"][1]
    assert second["kind"] == "folder" and second["matrix"].endswith("filtered_feature_bc_matrix.h5")
    assert "10x Cell Ranger folder: 1 library, Sample1_WT" in second["description"]["one_line_summary"]
    assert decisions["input_name"] == "Sample3_GSM5676874_Sample1_WT"     # what its results are named
    assert "unusable_inputs" not in decisions
    said = json.dumps(published)
    assert "run failed" not in said
    assert "Bound extra folder: Sample1_WT/" in said and "Loaded folder: Sample3_GSM5676874/" in said
    assert "2 libraries (Sample3_GSM5676874, Sample1_WT)" in said
    assert "unknown file" not in said and "original/" not in said


def test_the_planner_is_told_which_attached_datasets_it_cannot_use(tmp_path):
    from aiscientist.agents.research_harness import HarnessContext
    from aiscientist.agents.research_lab import LabConfig, ResearchLab
    from aiscientist.gateway.app import _cellranger_layout_local, _record_input_layout

    a, b = _library(tmp_path / "a" / "original"), _library(tmp_path / "b" / "original")
    layout = merge_cellranger_layouts([("Sample3_GSM5676874", _cellranger_layout_local(a)),
                                       ("Sample1_WT", _cellranger_layout_local(b))])
    decisions = {"dataset_result": {"dataset_kind": "single_cell_other"},
                 "datasets": [{"name": "filtered_feature_bc_matrix.h5", "primary": True},
                              {"name": "Sample1_WT/", "kind": "folder", "primary": False},
                              {"name": "notes/", "kind": "folder", "primary": False}],
                 "unusable_inputs": [{"name": "notes", "reason": "it holds no Cell Ranger library"}]}
    _record_input_layout(decisions, layout)
    lab = ResearchLab(HarnessContext(decisions=decisions, tunnel_port=1, model="m"), LabConfig(),
                      complete_fn=lambda m: "x")
    ctx = lab._dataset_context()
    assert "Sample1_WT/ — SAMPLE FOLDER" in ctx and "notes/ — extra folder" in ctx
    assert "`run_cellqc` reads the Cell Ranger libraries of every attached folder" in ctx
    assert "ATTACHED BUT NOT USABLE" in ctx and "  - notes: it holds no Cell Ranger library" in ctx


def test_a_bound_folder_without_a_library_is_named_with_the_reason(tmp_path):
    from aiscientist.gateway import app as gw

    class _Conn:
        executor = None
        settings = None

    a = _library(tmp_path / "a" / "original")
    other = tmp_path / "b" / "original"
    other.mkdir(parents=True)
    (other / "README.md").write_text("x")
    folders = [{"name": "Sample3_GSM5676874", "path": str(a), "hpc": ""},
               {"name": "notes", "path": str(other), "hpc": ""}]
    layout, empty = gw._input_folders_layout(_Conn(), folders, {})
    assert layout["n_libraries"] == 1 and [f["name"] for f in empty] == ["notes"]
    rec = gw._stage_secondary_dataset(_Conn(), str(other), "notes/", None, tmp_path / "staged")
    assert rec["kind"] == "folder" and rec["matrix"] == "" and rec["path"] == str(other)
    records = [{"primary": True}, rec]
    gw._folder_gists(records, folders, layout)
    assert rec["folder_gist"].startswith("a folder with no recognised data file")


def test_a_reference_folder_is_not_one_of_the_samples(tmp_path):
    from aiscientist.gateway import app as gw
    bound = [{"path": "/x/a", "name": "A/", "role": None}, {"path": "/x/atlas", "name": "atlas/", "role": "reference"},
             {"path": "/x/b", "name": "B/", "role": None}]
    records = [{"primary": True},
               {"path": "/x/atlas", "name": "atlas/", "role": "reference", "kind": "folder"},
               {"path": "/x/b", "name": "B/", "role": None, "kind": "folder", "hpc_primary": "/dfs/b"}]
    folders = gw._input_folder_list(bound, records, {"dataset_root": "/x/a"}, {})
    assert folders == [{"name": "A", "path": "/x/a", "hpc": ""}, {"name": "B", "path": "/x/b", "hpc": "/dfs/b"}]
    # A file primary keeps the bind-set as it always was: no sample folders.
    assert gw._input_folder_list(bound, records, {"dataset_path": "/x/a.h5ad"}, {}) == []


# --- HPC3: a session copy of each, and the HPC3 original of a moved dataset --------------------------


def test_each_folder_is_copied_to_hpc3_and_an_hpc3_original_is_read_from_the_server(kept, tmp_path, monkeypatch):  # noqa: F811 - a fixture imported above
    _client, _conn, gw = kept
    pytest.importorskip("sqlalchemy")
    from aiscientist.gateway import auth_routes, dataset_storage, db, models
    db.reset(f"sqlite:///{(tmp_path / 'issue22.db').as_posix()}")
    db.init_db()
    conn = _live(gw, tmp_path, monkeypatch)
    try:
        with db.session_scope() as session:
            user = models.User(username="tester", password_hash="x")
            session.add(user)
            session.commit()
            conn.app_user_id = user.id
        monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
        original = f"{SHARED}/uploads/tester/Sample3_GSM5676874"
        # Sample3 was uploaded to HPC3 and has since been moved here; Sample1_WT was uploaded here.
        moved_id = str(uuid4())
        sample3 = _library(dataset_storage.dataset_dir(moved_id, "tester") / "original", matrix=False)
        (sample3.parent / "source.json").write_text(json.dumps({"hpc3_path": original}))
        sample1 = _library(dataset_storage.dataset_dir(str(uuid4()), "tester") / "original", matrix=False)
        row = auth_routes.record_folder_dataset(conn.app_user_id, "Sample3_GSM5676874", original, 3)
        assert auth_routes.repoint_dataset(conn.app_user_id, row, original, str(sample3))
        auth_routes.record_folder_dataset(conn.app_user_id, "Sample1_WT", str(sample1), 3)
        conn.executor.files[f"{original}/filtered_feature_bc_matrix.h5"] = b"OLD"

        bound = [{"path": original, "name": "Sample3_GSM5676874/", "role": None},   # as the chat sent it
                 {"path": str(sample1), "name": "Sample1_WT/", "role": None}]
        bound, said = gw._current_bound_paths(conn, bound)
        assert [e["path"] for e in bound] == [str(sample3), str(sample1)]
        assert said and "Sample3_GSM5676874: read from your data on the server" in said[0]
        # Someone else's HPC3 path, or one nothing was moved from, is left as it is.
        assert gw._kept_copy_of(conn, f"{SHARED}/uploads/tester/Sample2") is None
        assert gw._kept_copy_of(conn, f"{original}/filtered_feature_bc_matrix.h5") == str(
            sample3 / "filtered_feature_bc_matrix.h5")

        admitted, mirrors = asyncio.run(gw._admit_bound_paths(conn, bound, lambda *a: None, lambda *a: None))
        assert [e["path"] for e in admitted] == [str(sample3), str(sample1)]
        copy3, copy1 = mirrors[str(sample3)][0], mirrors[str(sample1)][0]
        assert copy3 != copy1 and conn.executor.files[f"{copy1}/metrics_summary.csv"]

        decisions = {"dataset_root": str(sample3), "hpc_dataset_root": copy3}
        rec = gw._stage_secondary_dataset(conn, str(sample1), "Sample1_WT/", None, tmp_path / "staged")
        rec["hpc_primary"] = mirrors[str(sample1)][0]
        folders = gw._input_folder_list(admitted, [{"primary": True}, rec], decisions, mirrors)
        assert folders == [{"name": "Sample3_GSM5676874", "path": str(sample3), "hpc": copy3},
                           {"name": "Sample1_WT", "path": str(sample1), "hpc": copy1}]
        layout, empty = gw._input_folders_layout(conn, folders, {})
        assert [lib["sample"] for lib in layout["libraries"]] == ["Sample3_GSM5676874", "Sample1_WT"]
        assert empty == []
    finally:
        gw.CONNECTIONS.pop(conn.id, None)
        db.reset()


def test_a_dfs3b_folder_bound_beside_the_primary_is_a_folder_not_a_file(tmp_path):
    from aiscientist.gateway import app as gw

    class _Hpc(FakeHpc):
        def exec(self, cmd, timeout=60.0):
            if cmd.startswith("test -d "):
                path = cmd.split("test -d ", 1)[1].split(" &&", 1)[0].strip("'")
                out = "D" if path in self._dirs() else ""
                self.calls.append(cmd)
                import types
                return types.SimpleNamespace(ok=True, stdout=out, stderr="", out=out, exit_status=0)
            if cmd.startswith("find ") and "-printf" in cmd:
                root = cmd.split("find ", 1)[1].split(" -type", 1)[0].strip("'")
                out = "".join(f"{len(b)}\t{p}\n" for p, b in self.files.items() if p.startswith(root + "/"))
                import types
                return types.SimpleNamespace(ok=True, stdout=out, stderr="", out=out, exit_status=0)
            return super().exec(cmd, timeout)

    folder = f"{SHARED}/uploads/tester/Sample1_WT"
    hpc = _Hpc({f"{folder}/raw_feature_bc_matrix.h5": b"R" * 9, f"{folder}/filtered_feature_bc_matrix.h5": b"F",
                f"{folder}/metrics_summary.csv": b"m"})

    class _Conn:
        executor = hpc
        settings = type("S", (), {"lab_storage": LAB, "shared_root": SHARED})()

    rec = gw._stage_secondary_dataset(_Conn(), folder, "Sample1_WT/", None, tmp_path / "staged")
    assert rec["kind"] == "folder" and rec["remote"] is True and rec["hpc_primary"] == folder
    assert rec["matrix"] == f"{folder}/filtered_feature_bc_matrix.h5"
    assert not (tmp_path / "staged").exists()                 # a folder is never pulled back whole


# --- run_cellqc reads them all -----------------------------------------------------------------------


def test_cellqc_reads_every_bound_folder_in_one_run(tmp_path, fake_cellqc):  # noqa: F811 - a fixture imported above
    pytest.importorskip("scanpy")
    from aiscientist.agents.research_harness import HarnessContext
    from aiscientist.tools.run_cellqc.tool import run_cellqc

    a = _library(tmp_path / "data" / "aaa" / "original", matrix=False)
    b = _library(tmp_path / "data" / "bbb" / "original", matrix=False)
    notes = tmp_path / "data" / "ccc" / "original"
    notes.mkdir(parents=True)
    (notes / "notes.txt").write_text("x")
    ws = tmp_path / "run"
    ctx = HarnessContext(workspace=ws, decisions={
        "dataset_root": str(a),
        "input_folders": [{"name": "Sample3_GSM5676874", "path": str(a)},
                          {"name": "Sample1_WT", "path": str(b)},
                          {"name": "notes", "path": str(notes)},
                          {"name": "gone", "path": str(tmp_path / "missing")}]})
    out = run_cellqc({}, ctx)
    assert out["status"] == "ok", out
    lines = (ws / "work" / "cellqc" / "samples.txt").read_text().splitlines()[1:]
    assert [ln.split("\t")[0] for ln in lines] == ["Sample3_GSM5676874", "Sample1_WT"]
    assert out["n_libraries"] == 2
    assert {lib["sample"] for lib in out["libraries"]} == {"Sample3_GSM5676874", "Sample1_WT"}
    text = "\n".join(out["warnings"])
    assert "notes/ holds no Cell Ranger library" in text and "gone/ could not be read" in text
    assert out["read_from"] == "the bound folders (Sample3_GSM5676874/, Sample1_WT/)"
    import anndata as ad
    adata = ad.read_h5ad(ws / "work" / "adata_qc.h5ad")
    assert set(adata.obs["sampleid"]) == {"Sample3_GSM5676874", "Sample1_WT"}


def test_results_are_named_after_the_datasets_not_their_folder():
    from aiscientist.tools.api import cellqc_result_name, result_dataset_name
    assert result_dataset_name("Sample1_WT", "f0cbca6c900e").startswith("Sample1_WT.processed.")
    assert cellqc_result_name("Sample3_GSM5676874_Sample1_WT", "r1").startswith(
        "Sample3_GSM5676874_Sample1_WT.cellqc.")
    # What the path alone gave for a dataset kept on the server (run f0cbca6c900e).
    assert result_dataset_name(f"/data/users/u/datasets/{uuid4()}/original/filtered_feature_bc_matrix.h5",
                               "r1").startswith("original.")


# --- the console: a moved dataset stays attached -----------------------------------------------------

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    if source[start - 6:start] == "async ":
        start -= 6
    depth, i = 0, source.index("{", source.index(")", start))
    while True:
        ch = source[i]
        depth += ch == "{"
        depth -= ch == "}"
        i += 1
        if depth == 0:
            return source[start:i] + "\n"


def test_the_console_follows_a_dataset_moved_from_hpc3_to_the_server():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    src = APP_JS.read_text(encoding="utf-8")
    code = "".join(_function(src, n) for n in ("loadDatasetChips", "repointEverywhere")) + r"""
const old = "/dfs3b/u/uploads/Sample3_GSM5676874", now = "/data/users/u/datasets/aaa/original";
const state = { datasets: [{ id: 33, name: "Sample3_GSM5676874/", path: old, kind: "folder" }],
                sessions: [{ id: "A" }, { id: "B" }] };
const chats = { A: { bound: [old, "/x/other.h5ad"], roles: { [old]: "primary" } }, B: { bound: [], roles: {} } };
const saved = [];
const chatOf = (sid) => chats[sid];
const saveComposer = (c) => saved.push(c === chats.A ? "A" : "B");
const isFolderKind = (k) => k === "folder";
const renderDatasetChips = () => {}, renderDataMenuRecent = () => {};
const fetch = async () => ({ ok: true, json: async () => ({ datasets: [
  { id: 33, name: "Sample3_GSM5676874", path: now, kind: "folder" },
  { id: 36, name: "Sample1_WT", path: "/data/users/u/datasets/bbb/original", kind: "folder" },
  { id: 37, name: "other.h5ad", path: "/x/other.h5ad", kind: "h5ad" }] }) });
loadDatasetChips().then(() => process.stdout.write(JSON.stringify({
  paths: state.datasets.map((d) => d.path), ids: state.datasets.map((d) => d.id),
  bound: chats.A.bound, roles: chats.A.roles, saved })));
"""
    done = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["paths"] == ["/data/users/u/datasets/aaa/original", "/data/users/u/datasets/bbb/original",
                            "/x/other.h5ad"]
    assert out["ids"] == [33, 36, 37]
    assert out["bound"] == ["/data/users/u/datasets/aaa/original", "/x/other.h5ad"]
    assert out["roles"] == {"/data/users/u/datasets/aaa/original": "primary"} and out["saved"] == ["A"]


# --- libraries of two species (the two folders of issue 22: a human and a mouse library) -------------


def test_the_planner_is_told_two_species_are_qcd_apart_in_one_step(tmp_path):
    pytest.importorskip("h5py")
    from aiscientist.gateway.app import _cellranger_layout_local
    a = _library(tmp_path / "a" / "original", genome="GRCh38")
    b = _library(tmp_path / "b" / "original")
    layout = merge_cellranger_layouts([("Sample3_GSM5676874", _cellranger_layout_local(a)),
                                       ("Sample1_WT", _cellranger_layout_local(b))])
    hint = cellranger_layout_hint(layout)
    assert "DIFFERENT SPECIES (human: Sample3_GSM5676874; mouse: Sample1_WT)" in hint
    assert "QCs each species in its own CellQC run" in hint
    assert "Later steps read the human libraries' checkpoint only" in hint
    assert "The reference genome makes the species" not in hint      # no single species to state


def test_the_verification_names_each_species_checkpoint():
    from aiscientist.reporting.verification import qc_section
    libs = [{"sample": "Sample3_GSM5676874", "cells_after_doublets": 11000},
            {"sample": "Sample1_WT", "cells_after_doublets": 15000}]
    result = {"status": "ok", "cells_before": 32240, "cells_after": 26000, "libraries": libs,
              "checkpoints": {"human": {"checkpoint": "adata_qc.h5ad", "cells": 11000},
                              "mouse": {"checkpoint": "adata_qc.mouse.h5ad", "cells": 15000}}}
    rounds = [{"scientist_result": {"steps": [{"tool": "run_cellqc", "result": result}]}}]
    lines, _ = qc_section(rounds)
    text = "\n".join(lines)
    assert ("The QC checkpoints hold 26,000 cells (human: 11,000 in adata_qc.h5ad; mouse: 15,000 in "
            "adata_qc.mouse.h5ad); the libraries' final counts sum to 26,000 (consistent)") in text
