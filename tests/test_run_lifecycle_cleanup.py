"""A finished run keeps its deliverable and nothing else, and deleting a chat takes the disk too.

Two changes Yijun asked for on 2026-08-21, after the console's run store reached 7.4 GB:

* report regeneration is gone, so the analysis checkpoints no longer have to survive for a TTL —
  the process files are released the moment the report exists;
* deleting a chat must delete its runs' bundles, and must not depend on the browser having loaded
  that chat's messages to know which runs those were.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The precondition is that gateway.app IMPORTS — it pulls fastapi and paramiko at module scope,
# and guarding on one of them only moves the failure to the other. Without a guard the import
# raises during COLLECTION, which pytest treats as fatal and aborts the whole session: unguarded
# modules took CI from 1,525 passing tests to "33 skipped, 3 errors" and kept main red from
# 2026-08-20 to 2026-09-08. CI installs the gateway extra so these actually RUN; the guard is
# what keeps a leaner environment skipping cleanly instead of taking every other test down.
pytest.importorskip("bioagent.gateway.app")

from bioagent.gateway import app as gw_app  # noqa: E402


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run123"
    for rel, size in (("work/adata_qc.h5ad", 4096), ("work/adata_clustered.h5ad", 8192),
                      ("staged/matrix.h5ad", 16384),
                      ("artifacts/report/report.md", 32), ("artifacts/figures/umap.png", 64),
                      ("artifacts/process/run_state.json", 128)):
        p = run / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return run


def test_the_deliverable_survives_and_the_machinery_does_not(tmp_path):
    run = _run_dir(tmp_path)
    freed = gw_app._drop_process_files(run)

    assert not (run / "work").exists(), "checkpoints are the bulk of the 7.4 GB"
    assert not (run / "staged").exists(), "a whole second copy of the input matrix"
    assert (run / "artifacts" / "report" / "report.md").exists()
    assert (run / "artifacts" / "figures" / "umap.png").exists()
    assert (run / "artifacts" / "process" / "run_state.json").exists(), \
        "small, and the only record of what each step actually did"
    assert freed == 4096 + 8192 + 16384


def test_it_is_idempotent_and_quiet_on_a_run_that_has_neither(tmp_path):
    run = tmp_path / "empty"
    (run / "artifacts").mkdir(parents=True)
    assert gw_app._drop_process_files(run) == 0
    assert gw_app._drop_process_files(run) == 0        # second pass finds nothing, raises nothing


def test_a_symlinked_work_dir_is_left_alone(tmp_path):
    """Path-guarded: never follow a link out of the run directory and delete someone else's data."""
    outside = tmp_path / "outside"
    (outside / "keep.h5ad").parent.mkdir(parents=True, exist_ok=True)
    (outside / "keep.h5ad").write_bytes(b"precious")
    run = tmp_path / "run456"
    run.mkdir()
    (run / "work").symlink_to(outside, target_is_directory=True)

    gw_app._drop_process_files(run)
    assert (outside / "keep.h5ad").exists()


# --- deleting a chat deletes its bundles -------------------------------------------------------

def test_delete_run_dir_is_confined_to_the_results_root(tmp_path, monkeypatch):
    from bioagent.gateway import auth_routes
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    victim = tmp_path / "someone_else" / "run999"
    victim.mkdir(parents=True)

    assert auth_routes._delete_run_dir("owner", "../../etc") is False
    assert auth_routes._delete_run_dir("..", "run999") is False
    assert victim.exists()


def test_delete_run_dir_removes_the_bundle(tmp_path, monkeypatch):
    from bioagent.gateway import auth_routes
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    mine = tmp_path / "testowner" / "run123" / "artifacts"
    mine.mkdir(parents=True)
    (mine / "report.md").write_text("x", encoding="utf-8")

    assert auth_routes._delete_run_dir("testowner", "run123") is True
    assert not (tmp_path / "testowner" / "run123").exists()
    assert auth_routes._delete_run_dir("testowner", "run123") is False   # already gone


def test_report_regeneration_is_gone():
    """It was removed deliberately; nothing may quietly reintroduce the endpoint or its worker."""
    assert not hasattr(gw_app, "_regenerate_report")
    assert not hasattr(gw_app, "_edit_report_body")
    assert not hasattr(gw_app, "RegenerateReportRequest")
    paths = {getattr(r, "path", None) for r in gw_app.app.routes}
    assert "/api/report/regenerate" not in paths
    assert "edit_report" not in gw_app._FOLLOWUP_ROUTER_SYS
