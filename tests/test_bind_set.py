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

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.agents.research_lab import CriticVerdict, LabResult, LabRound  # noqa: E402


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


def test_bind_set_is_capped():
    req = gw_app.LabRequest(connection_id="c", question="q",
                            datasets=[{"path": f"/u/f{i}.csv"} for i in range(30)])
    assert len(gw_app._select_bound_datasets(req)) == gw_app._MAX_BOUND_DATASETS


# --- per-file staging records ------------------------------------------------------------------


class _FakeExec:
    def __init__(self):
        self.gets: list[tuple[str, str]] = []

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
