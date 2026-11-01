"""The multi-file BIND-SET (feature ②): a run may bind a SET of data files (a VCF + a BED panel + a
2nd VCF), not just one — while the legacy single ``dataset_path`` and the separate TEXT ``case_note``
keep working exactly as before.

These cover the request → staging-record → run_state → resume plumbing at the gateway layer:
``_select_bound_datasets`` (the reconcile point), ``_stage_secondary_dataset`` /
``_primary_dataset_record`` (per-file staging records), and the run_state round-trip. The Phase-B
content routing lives in ``test_content_routing.py``; the run-start auto-describe hook has its own
coverage in ``test_gateway_lab.py``.
"""

from __future__ import annotations

import json
import types

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.agents.research_lab import CriticVerdict, LabResult, LabRound  # noqa: E402


# --- _select_bound_datasets: the one place legacy + bind-set are reconciled -------------------


def test_legacy_single_dataset_path_is_a_one_element_set():
    """Back-compat contract: an old client sending only ``dataset_path`` yields exactly that one file
    as the primary — nothing else changes."""
    req = gw_app.LabRequest(connection_id="c", question="q", dataset_path="/dfs/u/case.vcf.gz")
    bound = gw_app._select_bound_datasets(req)
    assert bound == [{"path": "/dfs/u/case.vcf.gz", "name": "case.vcf.gz", "role": None}]


def test_no_data_is_empty_set():
    assert gw_app._select_bound_datasets(gw_app.LabRequest(connection_id="c", question="q")) == []


def test_multi_file_ranks_the_recognized_primary_first():
    """A VCF + a BED panel + a text note, sent in a "wrong" order: the recognized callset outranks the
    .txt (which outranks the unrecognized .bed), so the VCF is the primary that drives the tools."""
    req = gw_app.LabRequest(
        connection_id="c", question="q", dataset_path="/u/notes.txt",
        datasets=[{"path": "/u/notes.txt", "name": "notes.txt"},
                  {"path": "/u/panel.bed", "role": "gene_panel"},
                  {"path": "/u/case.vcf.gz"}])
    bound = gw_app._select_bound_datasets(req)
    assert [b["name"] for b in bound] == ["case.vcf.gz", "notes.txt", "panel.bed"]
    assert bound[0]["path"] == "/u/case.vcf.gz"          # primary
    assert bound[2]["role"] == "gene_panel"              # role preserved on the secondary


def test_datasets_list_overrides_and_dedupes_and_drops_pathless():
    # ``datasets`` present → it OVERRIDES the legacy ``dataset_path``; a duplicate path and a
    # pathless/blank entry are dropped. (Pydantic's ``list[dict]`` already rejects a non-object entry
    # with a 422 at the request boundary, so ``_select_bound_datasets`` only has to drop bad-value dicts.)
    req = gw_app.LabRequest(
        connection_id="c", question="q", dataset_path="/u/ignored_when_list_present.vcf",
        datasets=[{"path": "/u/a.vcf.gz"}, {"path": "/u/a.vcf.gz"},   # duplicate dropped
                  {"name": "no path"}, {"path": "  "}])                # pathless / blank dropped
    bound = gw_app._select_bound_datasets(req)
    assert [b["path"] for b in bound] == ["/u/a.vcf.gz"]


def test_oversized_bind_set_is_refused():
    req = gw_app.LabRequest(connection_id="c", question="q",
                            datasets=[{"path": f"/u/f{i}.csv"} for i in range(30)])
    with pytest.raises(gw_app.HTTPException, match="At most 12 datasets"):
        gw_app._select_bound_datasets(req)


# --- per-file staging records ------------------------------------------------------------------


class _FakeExec:
    def __init__(self):
        self.gets: list[tuple[str, str]] = []

    def exec(self, cmd: str, timeout: float = 60.0):
        # _ensure_local_dataset now checks the source exists (`test -s … && echo OK`) before
        # staging; a fake remote FS answers "present" for everything.
        return types.SimpleNamespace(ok=True, out="OK", stderr="")

    def get_file(self, remote: str, local: str) -> None:
        self.gets.append((remote, local))
        with open(local, "wb") as fh:
            fh.write(b"staged")


class _FakeConn:
    def __init__(self, executor=None, lab_storage="/dfs/ruic20_lab", shared_root=""):
        self.executor = executor
        self.settings = types.SimpleNamespace(
            lab_storage=lab_storage, shared_root=shared_root or f"{lab_storage}/AiScientist")


def test_stage_secondary_local_file_is_used_in_place(tmp_path):
    conn = _FakeConn(executor=None)
    rec = gw_app._stage_secondary_dataset(conn, "/local/panel.bed", "panel.bed", "gene_panel",
                                          tmp_path / "staged")
    assert rec == {"path": "/local/panel.bed", "name": "panel.bed", "role": "gene_panel",
                   "hpc_primary": None, "remote": False, "primary": False}


def test_stage_secondary_remote_file_is_staged_back_and_remembers_dfs3b(tmp_path):
    ex = _FakeExec()
    conn = _FakeConn(executor=ex, lab_storage="/dfs/ruic20_lab")
    remote = "/dfs/ruic20_lab/u/uploads/second.vcf.gz"
    rec = gw_app._stage_secondary_dataset(conn, remote, "second.vcf.gz", None, tmp_path / "staged")
    assert rec["remote"] is True and rec["hpc_primary"] == remote and rec["primary"] is False
    assert rec["path"].endswith("second.vcf.gz")
    assert ex.gets == [(remote, rec["path"])]            # a single bounded stage-back


def test_primary_record_reflects_the_staged_decisions():
    primary = {"path": "/u/case.vcf.gz", "name": "case.vcf.gz", "role": "proband"}
    # remote-staged: dataset_path is the local copy, hpc_primary the dfs3b path
    rec = gw_app._primary_dataset_record(
        primary, {"dataset_path": "/ws/staged/case.vcf.gz", "hpc_primary": "/dfs/u/case.vcf.gz"})
    assert rec["primary"] is True and rec["remote"] is True
    assert rec["path"] == "/ws/staged/case.vcf.gz" and rec["hpc_primary"] == "/dfs/u/case.vcf.gz"
    assert rec["role"] == "proband"
    # local: no hpc_primary → not remote
    rec2 = gw_app._primary_dataset_record(primary, {"dataset_path": "/u/case.vcf.gz"})
    assert rec2["remote"] is False and rec2["hpc_primary"] is None


# --- run_state round-trip: the whole set survives persist + resume -----------------------------


def _result():
    rounds = [LabRound(1, 1, "Annotate", "S", {"final_answer": "x"},
                       CriticVerdict("accept", 0.9, "ok"))]
    return LabResult("diagnose", ["Annotate"], rounds, True, 1, "final")


def test_write_run_state_persists_the_bind_set(tmp_path):
    ds = [{"path": "/ws/staged/case.vcf.gz", "name": "case.vcf.gz", "role": None,
           "hpc_primary": "/dfs/u/case.vcf.gz", "remote": True, "primary": True},
          {"path": "/ws/staged/panel.bed", "name": "panel.bed", "role": "gene_panel",
           "hpc_primary": None, "remote": False, "primary": False}]
    gw_app._write_run_state(tmp_path, _result(), None,
                            {"dataset_path": "/ws/staged/case.vcf.gz", "datasets": ds})
    state = json.loads((tmp_path / "process" / "run_state.json").read_text())
    assert state["dataset_path"] == "/ws/staged/case.vcf.gz"   # legacy primary still written
    assert [d["name"] for d in state["datasets"]] == ["case.vcf.gz", "panel.bed"]


def test_write_run_state_omits_the_set_for_a_legacy_single_file(tmp_path):
    gw_app._write_run_state(tmp_path, _result(), None, {"dataset_path": "/u/a.h5ad"})
    state = json.loads((tmp_path / "process" / "run_state.json").read_text())
    assert "datasets" not in state             # additive: a single-file run writes no set
    assert state["dataset_path"] == "/u/a.h5ad"


def test_cont_req_carries_the_bind_set_forward_on_resume():
    """A LabRequest rebuilt for a resume carries both the legacy primary AND the whole set, so the
    resumed run re-enters with every bound file."""
    ds = [{"path": "/ws/staged/case.vcf.gz", "name": "case.vcf.gz"},
          {"path": "/ws/staged/panel.bed", "name": "panel.bed", "role": "gene_panel"}]
    req = gw_app.LabRequest(connection_id="c", question="q",
                            dataset_path="/ws/staged/case.vcf.gz", datasets=ds)
    bound = gw_app._select_bound_datasets(req)
    assert bound[0]["path"] == "/ws/staged/case.vcf.gz"      # primary preserved
    assert [b["name"] for b in bound] == ["case.vcf.gz", "panel.bed"]


# --- roles: a REFERENCE file (e.g. a cell-type atlas) never becomes the primary ----------------


def test_a_reference_file_never_becomes_the_primary():
    """Format ranking alone puts an .h5ad atlas ahead of a .csv query — the study would then be
    profiled, QC'd and tested on the atlas. A file marked "reference" ranks after every other."""
    req = gw_app.LabRequest(
        connection_id="c", question="q",
        datasets=[{"path": "/u/atlas.h5ad", "role": "reference"}, {"path": "/u/query.csv"}])
    bound = gw_app._select_bound_datasets(req)
    assert [b["name"] for b in bound] == ["query.csv", "atlas.h5ad"]
    assert bound[1]["role"] == "reference"


def test_an_explicit_primary_role_pins_the_primary():
    req = gw_app.LabRequest(
        connection_id="c", question="q",
        datasets=[{"path": "/u/a.h5ad"}, {"path": "/u/b.h5ad", "role": "Primary"}])
    assert gw_app._select_bound_datasets(req)[0]["path"] == "/u/b.h5ad"


def test_only_references_still_yield_a_primary():
    req = gw_app.LabRequest(connection_id="c", question="q",
                            datasets=[{"path": "/u/x.csv", "role": "reference"},
                                      {"path": "/u/y.h5ad", "role": "reference"}])
    assert gw_app._select_bound_datasets(req)[0]["path"] == "/u/y.h5ad"


# --- a dfs3b secondary is read in place when run_code runs on HPC3 -----------------------------


def test_stage_secondary_remote_is_not_copied_back_when_read_in_place(tmp_path):
    ex = _FakeExec()
    conn = _FakeConn(executor=ex, lab_storage="/dfs/ruic20_lab")
    remote = "/dfs/ruic20_lab/u/uploads/atlas.h5ad"
    rec = gw_app._stage_secondary_dataset(conn, remote, "atlas.h5ad", "reference",
                                          tmp_path / "staged", stage_local=False)
    assert ex.gets == []                                  # no multi-GB pull to the gateway
    assert rec == {"path": remote, "name": "atlas.h5ad", "role": "reference",
                   "hpc_primary": remote, "remote": True, "primary": False}


class _PutExec(_FakeExec):
    def __init__(self):
        super().__init__()
        self.puts: list[tuple[str, str]] = []
        self.cmds: list[str] = []

    def exec(self, cmd: str, timeout: float = 60.0):
        self.cmds.append(cmd)
        return super().exec(cmd, timeout)

    def put_file(self, local: str, remote: str) -> None:
        self.puts.append((local, remote))


def test_run_code_inputs_on_hpc_use_dfs3b_in_place_and_upload_local_files(tmp_path):
    local = tmp_path / "panel.bed"
    local.write_text("chr1\t1\t2\n")
    decisions = {"datasets": [
        {"path": "/dfs/u/q.h5ad", "name": "q.h5ad", "hpc_primary": "/dfs/u/q.h5ad", "primary": True},
        {"path": "/dfs/u/atlas.h5ad", "name": "atlas.h5ad", "role": "reference",
         "hpc_primary": "/dfs/u/atlas.h5ad", "remote": True, "primary": False},
        {"path": str(local), "name": "panel.bed", "role": "gene_panel", "hpc_primary": None,
         "remote": False, "primary": False},
        {"path": "/gone/x.vcf", "name": "x.vcf", "primary": False, "error": "staging failed"},
    ]}
    ex = _PutExec()
    inputs, warnings = gw_app._remote_inputs(_FakeConn(executor=ex), decisions, "/dfs/tmp/run1")
    assert inputs == [
        {"name": "atlas.h5ad", "role": "reference", "path": "/dfs/u/atlas.h5ad"},
        {"name": "panel.bed", "role": "gene_panel", "path": "/dfs/tmp/run1/inputs/panel.bed"}]
    assert ex.puts == [(str(local), "/dfs/tmp/run1/inputs/panel.bed")]
    assert warnings == []


def test_local_sandbox_inputs_are_only_files_readable_here(tmp_path):
    here = tmp_path / "atlas.csv"
    here.write_text("x")
    decisions = {"datasets": [
        {"path": str(tmp_path / "q.h5ad"), "name": "q.h5ad", "primary": True},
        {"path": str(here), "name": "atlas.csv", "role": "reference", "primary": False},
        {"path": "/dfs/u/other.h5ad", "name": "other.h5ad", "primary": False}]}
    assert gw_app._local_inputs(decisions) == [
        {"name": "atlas.csv", "role": "reference", "path": str(here)}]


def test_one_failed_upload_is_a_warning_not_a_lost_executor(tmp_path):
    local = tmp_path / "panel.bed"
    local.write_text("x")

    class _Broken(_PutExec):
        def put_file(self, local, remote):
            raise OSError("sftp closed")

    decisions = {"datasets": [{"path": str(local), "name": "panel.bed", "primary": False}]}
    inputs, warnings = gw_app._remote_inputs(_FakeConn(executor=_Broken()), decisions, "/dfs/tmp/r")
    assert inputs == [] and "upload to HPC3 failed" in warnings[0]


def test_plan_inputs_show_actual_uses_and_unusable_reasons():
    data = gw_app._plan_dataset_inputs({
        "datasets": [{"name": "main", "primary": True}, {"name": "second/", "kind": "folder"},
                     {"name": "atlas", "role": "reference"}, {"name": "panel.bed"}],
        "cellranger_layout": {"datasets": ["main", "second"]},
        "unusable_inputs": [{"name": "missing", "reason": "its files were not found"}],
    })
    assert [d["use"] for d in data["datasets"]] == ["main", "sample", "reference", "extra input"]
    assert data["unusable_inputs"] == [{"name": "missing", "reason": "its files were not found"}]
