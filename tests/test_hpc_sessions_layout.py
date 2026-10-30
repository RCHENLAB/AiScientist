"""HPC3 holds environments, images, models and one folder per session (RCHENLAB/AiScientist issue 28).

What this file pins down:

1. **The layout.** ``sessions/<session-uuid>/`` with ``app/``, ``inputs/``, ``runs/<run_id>/<kind>``
   and ``runs/<run_id>/jobs/<line>``; folder names are ids only; caches under ``envs/``, weights
   under ``models/``.
2. **A run keeps its folder across sessions.** A run continued after the session that started it
   ended (or from before session folders, in ``Temp/<user>``) finds its checkpoints where they are.
3. **The cleanup clears a session folder but its runs and logs**, takes a run folder only when its
   results match the manifest, and touches only folders this account owns.
4. **The sweep** clears cold session folders (results kept), never a live one, and retires the old
   per-user folders once nothing uses them.
5. **The Storage tab's Delete** reaches the account's own session folders.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.gateway import hpc_layout as layout  # noqa: E402
from aiscientist.gateway.executor import ExecResult  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CLEAN = REPO / "deploy" / "hpc3" / "aiscientist_session_clean.sh"
SWEEP = REPO / "deploy" / "hpc3" / "aiscientist_temp_gc.sh"
DELETE = REPO / "deploy" / "hpc3" / "aiscientist_storage_delete.sh"
ROOT = "/dfs3b/lab/software/AiScientist"


# --- 1. the layout ---------------------------------------------------------------------------------

def test_a_runs_folders_in_the_session_layout_and_in_the_retired_one():
    new = layout.session_run(ROOT, "s1", "r1")
    assert new.root == f"{ROOT}/sessions/s1/runs/r1"
    assert new.dir("analysis") == f"{ROOT}/sessions/s1/runs/r1/analysis"
    assert new.jobs("runcode") == f"{ROOT}/sessions/s1/runs/r1/jobs/runcode"
    old = layout.legacy_run(ROOT, "tester", "r1")
    assert old.dir("analysis") == f"{ROOT}/Temp/tester/analysis/r1"
    assert old.jobs("runcode") == f"{ROOT}/Temp/tester/scratch/runcode/r1"
    assert layout.RunHome.from_json(new.to_json()) == new
    assert layout.RunHome.from_json({"run_id": "../x", "layout": "session", "root": "/"}) is None


def test_names_are_ids_and_the_shared_caches_live_under_envs():
    with pytest.raises(ValueError):
        layout.session_dir(ROOT, "../etc")
    with pytest.raises(ValueError):
        layout.session_run(ROOT, "s1", "a/b")
    assert layout.package_cache_root(ROOT) == f"{ROOT}/envs/run-code-pkgs"
    assert layout.conda_pkgs_dir(ROOT) == f"{ROOT}/envs/conda-pkgs"
    st = HPCSettings()
    for weights in (st.hf_home, st.scgpt_model_dir, st.vlreview_model_dir):
        assert weights.startswith(f"{st.shared_root}/models/")


def test_a_stored_path_is_ours_only_inside_a_session_folder_or_our_temp():
    ok = lambda p, **k: layout.owns_path(ROOT, p, user="tester", **k)  # noqa: E731
    assert ok(f"{ROOT}/sessions/s1/runs/r1")
    assert ok(f"{ROOT}/Temp/tester/analysis/r1")
    assert not ok(f"{ROOT}/Temp/other/analysis/r1")
    assert not ok(f"{ROOT}/sessions/s1/../../containers")
    assert not ok(f"{ROOT}/containers/analysis.sif")
    assert not ok(f"{ROOT}/sessions/s2/runs/r1", session_ids=("s1",))


# --- 2. where a run's folders are -------------------------------------------------------------------

@pytest.fixture
def gw(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app
    saved = dict(gw_app.CONNECTIONS)
    gw_app.CONNECTIONS.clear()
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    yield gw_app
    gw_app.CONNECTIONS.clear()
    gw_app.CONNECTIONS.update(saved)


class _Remote:
    def __init__(self, existing: "set[str]" = frozenset()) -> None:
        self.username = "tester"
        self.existing = set(existing)
        self.commands: list[str] = []

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        ok = not command.startswith("test -d ") or command.split(" ", 2)[2] in self.existing
        return ExecResult(command=command, exit_status=0 if ok else 1, stdout="", stderr="")


def _conn(gw, tmp_path, remote):
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(shared_root=ROOT), mock=False, loop=loop, username="tester")
    loop.close()
    conn.workspace = tmp_path / "runs" / "tester"
    conn.executor = remote
    return conn


def test_a_new_run_lives_in_this_session_and_says_so(gw, tmp_path):
    conn = _conn(gw, tmp_path, _Remote())
    home = gw._resolve_run_home(conn, "r1")
    assert home == layout.session_run(ROOT, conn.id, "r1")
    rec = json.loads((conn.workspace / "r1" / "hpc3_home.json").read_text())
    assert rec["session_id"] == conn.id and rec["root"] == home.root
    n = len(conn.executor.commands)
    assert gw._resolve_run_home(conn, "r1") is home and len(conn.executor.commands) == n   # once


def test_a_run_continued_later_keeps_its_earlier_sessions_folder_while_it_is_there(gw, tmp_path):
    earlier = layout.session_run(ROOT, "0ld-5e55", "r1")
    rec = tmp_path / "runs" / "tester" / "r1" / "hpc3_home.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps(earlier.to_json()))
    conn = _conn(gw, tmp_path, _Remote({earlier.dir("analysis")}))
    assert gw._resolve_run_home(conn, "r1") == earlier
    assert gw._run_home(conn, "r1").jobs("analysis") == f"{ROOT}/sessions/0ld-5e55/runs/r1/jobs/analysis"
    # Gone (its session was cleaned): the run starts again in this session's folder.
    conn2 = _conn(gw, tmp_path, _Remote())
    assert gw._resolve_run_home(conn2, "r1") == layout.session_run(ROOT, conn2.id, "r1")


def test_a_run_from_before_session_folders_keeps_its_temp_folders(gw, tmp_path):
    old = layout.legacy_run(ROOT, "tester", "r1")
    conn = _conn(gw, tmp_path, _Remote({old.dir("analysis")}))
    assert gw._resolve_run_home(conn, "r1") == old
    assert gw._scratch_dir(conn, "analysis", run_id="r1") == f"{ROOT}/Temp/tester/scratch/analysis/r1"


def test_a_recorded_folder_of_someone_else_is_not_taken(gw, tmp_path):
    rec = tmp_path / "runs" / "tester" / "r1" / "hpc3_home.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"run_id": "r1", "layout": "legacy", "root": f"{ROOT}/Temp/other",
                               "session_id": ""}))
    conn = _conn(gw, tmp_path, _Remote({f"{ROOT}/Temp/other/analysis/r1"}))
    assert gw._resolve_run_home(conn, "r1").layout == "session"


def test_a_handed_on_result_is_recognised_in_either_layout_and_nowhere_else(gw, tmp_path):
    conn = _conn(gw, tmp_path, _Remote())
    ok = lambda p, t="cellqc": gw._is_run_result_path(conn, "r1", p, t)  # noqa: E731
    assert ok(f"{ROOT}/sessions/abc-123/runs/r1/analysis/result/cellqc")
    assert ok(f"{ROOT}/Temp/tester/analysis/r1/result/cellqc")
    assert ok(f"{ROOT}/sessions/abc-123/runs/r1/analysis/result/x.h5ad", "x.h5ad")
    assert not ok(f"{ROOT}/sessions/abc-123/runs/r2/analysis/result/cellqc")
    assert not ok(f"{ROOT}/Temp/other/analysis/r1/result/cellqc")
    assert not ok(f"{ROOT}/sessions/../x/runs/r1/analysis/result/cellqc")
    assert not ok(f"{ROOT}/sessions/abc-123/runs/r1/analysis/result/../../work", "../../work")


# --- 3. the cleanup script on a real tree ------------------------------------------------------------

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _session_tree(tmp_path: Path) -> "tuple[Path, Path]":
    root = tmp_path / "AiScientist"
    s = root / "sessions" / "s1"
    for rel, body in (("app/aiscientist/__init__.py", "x"), ("inputs/key/matrix.h5", "m"),
                      ("scripts/session_clean.sh", "#"), ("logs/sweep.log", "GC_RESULT"),
                      ("shell-home/.bashrc", "x"), ("downloads/ref.fa", "A"), ("jobs/worker/w.log", "w"),
                      ("manifest.json", "{}"),
                      ("runs/r1/analysis/result/x.h5ad", "RESULT"), ("runs/r1/analysis/work/c.h5ad", "C"),
                      ("runs/r1/jobs/analysis/j.out", "o"),
                      ("runs/r2/variant/out.vcf", "v")):
        f = s / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return root, s


def _clean(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(CLEAN), "--root", str(root), "--user", "tester", *args],
                          capture_output=True, text=True, timeout=120)


def test_the_cleanup_clears_a_session_folder_but_its_runs_logs_and_manifest(tmp_path):
    root, s = _session_tree(tmp_path)
    out = _clean(root, "--session-dir", str(s), "--quiet")
    assert out.returncode == 0, out.stderr
    assert sorted(p.name for p in s.iterdir()) == ["logs", "manifest.json", "runs"]
    assert (s / "runs" / "r1" / "analysis" / "work" / "c.h5ad").exists()


def test_the_cleanup_keeps_inputs_when_asked(tmp_path):
    root, s = _session_tree(tmp_path)
    _clean(root, "--session-dir", str(s), "--keep-inputs", "--quiet")
    assert (s / "inputs" / "key" / "matrix.h5").exists() and not (s / "app").exists()


def test_the_cleanup_takes_a_run_folder_only_when_its_results_are_home(tmp_path):
    root, s = _session_tree(tmp_path)
    verify = s / "logs" / "stageout" / "t1"
    verify.mkdir(parents=True)
    (verify / "r1.tsv").write_text(f"{_sha(b'RESULT')}\t6\tx.h5ad\n")
    out = _clean(root, "--session-dir", str(s), "--run-dir", str(s / "runs" / "r1"),
                 "--run-dir", str(s / "runs" / "r2"), "--verify-dir", str(verify), "--quiet")
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip().splitlines()[-1] == "CLEAN_RESULT removed=3 missing=0 kept=0"
    assert sorted(p.name for p in s.iterdir()) == ["logs", "manifest.json"]   # runs/ went once empty
    assert not verify.exists()

    root2, s2 = _session_tree(tmp_path / "again")
    (s2 / "runs" / "r1" / "analysis" / "result" / "x.h5ad").write_text("CHANGED")
    out = _clean(root2, "--run-dir", str(s2 / "runs" / "r1"), "--quiet")
    assert out.returncode == 3 and "KEPT run=r1 reason=no-stage-out-manifest" in out.stdout
    assert (s2 / "runs" / "r1" / "analysis" / "work" / "c.h5ad").exists()


@pytest.mark.parametrize("bad", ["/etc", "{root}/sessions/s1/../s2", "{root}/sessions", "{root}/Temp/tester"])
def test_a_session_folder_must_be_one_under_sessions(tmp_path, bad):
    root, _s = _session_tree(tmp_path)
    out = _clean(root, "--session-dir", bad.format(root=root))
    assert out.returncode == 2 and "--session-dir must be" in out.stderr


def test_a_run_folder_must_be_a_runs_folder_of_a_session(tmp_path):
    root, s = _session_tree(tmp_path)
    out = _clean(root, "--run-dir", str(s / "inputs"))
    assert out.returncode == 2 and "--run-dir must be" in out.stderr


def test_a_session_folder_that_is_a_link_is_not_followed(tmp_path):
    root, s = _session_tree(tmp_path)
    elsewhere = tmp_path / "precious"
    (elsewhere / "app").mkdir(parents=True)
    (root / "sessions" / "s9").symlink_to(elsewhere)
    out = _clean(root, "--session-dir", str(root / "sessions" / "s9"), "--quiet")
    assert out.returncode == 0 and (elsewhere / "app").exists()
    assert out.stdout.strip().splitlines()[-1] == "CLEAN_RESULT removed=0 missing=1 kept=0"


# --- 4. the sweep ------------------------------------------------------------------------------------

def _gnu_tools() -> bool:
    try:
        ok_find = subprocess.run(["find", "/dev/null", "-newer", "/dev/null", "-print", "-quit"],
                                 capture_output=True, timeout=10).returncode == 0
        ok_touch = subprocess.run(["touch", "-d", "-1 days", "/dev/null"],
                                  capture_output=True, timeout=10).returncode == 0
        return ok_find and ok_touch
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _gnu_tools(), reason="the sweeper needs GNU find/touch (as on HPC3)")
def test_the_sweep_clears_cold_sessions_keeps_results_and_the_live_one(tmp_path):
    root, s = _session_tree(tmp_path)
    live = root / "sessions" / "live1"
    (live / "app").mkdir(parents=True)
    empty = root / "sessions" / "done2"
    (empty / "logs").mkdir(parents=True)
    (empty / "logs" / "clean.log").write_text("CLEAN_RESULT removed=1")
    old = time.time() - 10 * 86400
    for p in root.rglob("*"):
        os.utime(p, (old, old), follow_symlinks=False)
    out = subprocess.run(["bash", str(SWEEP), "--root", str(root), "--ttl-days", "3", "--user", "tester",
                          "--keep-session", "live1", "--quiet"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "GC_RESULT removed=2 kept=0 held=1 retired=0"
    # s1: r1's result stays, everything else of it goes; r2 had no result and is gone.
    assert sorted(str(p.relative_to(s)) for p in s.rglob("*") if p.is_file()) == [
        "logs/sweep.log", "manifest.json", "runs/r1/analysis/result/x.h5ad"]
    assert not empty.exists()                       # nothing but logs: the session is over
    assert (live / "app").exists()                  # a live session is never swept


@pytest.mark.skipif(not _gnu_tools(), reason="the sweeper needs GNU find/touch (as on HPC3)")
def test_the_sweep_retires_the_old_per_user_folders_once_temp_is_empty(tmp_path):
    root = tmp_path / "AiScientist"
    for rel in ("Temp/tester", "pysrc/tester/aiscientist", "bin/tester/originals",
                "uploads/tester/server-data", "pysrc/other/aiscientist"):
        (root / rel).mkdir(parents=True)
    (root / "uploads" / "tester2.keep").mkdir()
    out = subprocess.run(["bash", str(SWEEP), "--root", str(root), "--ttl-days", "3", "--user", "tester",
                          "--retire-legacy", "--quiet"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "retired=3" in out.stdout.strip().splitlines()[-1]
    for gone in ("Temp/tester", "pysrc/tester", "bin/tester", "uploads/tester"):
        assert not (root / gone).exists()
    assert (root / "pysrc" / "other" / "aiscientist").exists()       # another member's: never


@pytest.mark.skipif(not _gnu_tools(), reason="the sweeper needs GNU find/touch (as on HPC3)")
def test_the_old_folders_stay_while_temp_holds_anything_or_uploads_hold_a_file(tmp_path):
    root = tmp_path / "AiScientist"
    (root / "Temp" / "tester" / "analysis" / "r1").mkdir(parents=True)
    (root / "Temp" / "tester" / "analysis" / "r1" / "f").write_text("x")
    (root / "pysrc" / "tester").mkdir(parents=True)
    out = subprocess.run(["bash", str(SWEEP), "--root", str(root), "--ttl-days", "3", "--user", "tester",
                          "--retire-legacy", "--quiet"], capture_output=True, text=True, timeout=120)
    assert "retired=0" in out.stdout and (root / "pysrc" / "tester").exists()


def test_retiring_needs_a_user(tmp_path):
    out = subprocess.run(["bash", str(SWEEP), "--root", str(tmp_path), "--retire-legacy"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 2 and "--retire-legacy needs --user" in out.stderr


# --- 5. the Storage tab's Delete ---------------------------------------------------------------------

def _delete(root: Path, *paths: str) -> subprocess.CompletedProcess:
    args = ["bash", str(DELETE), "--root", str(root), "--user", "tester"]
    for p in paths:
        args += ["--path", p]
    return subprocess.run(args, capture_output=True, text=True, env={**os.environ, "SLURM_JOB_ID": "7"})


def test_the_delete_reaches_a_session_folder_this_account_owns(tmp_path):
    root, s = _session_tree(tmp_path)
    out = _delete(root, str(s / "runs" / "r2"), str(root / "sessions" / "nobody" / "x"))
    assert out.returncode == 0, out.stderr
    assert not (s / "runs" / "r2").exists()
    assert "DELETED" in out.stdout and "SKIP\toutside the user's own folders" in out.stdout
    out = _delete(root, str(s))
    assert not s.exists() and "deleted=1" in out.stdout


def test_the_gateway_refuses_to_delete_the_folder_of_the_live_session(gw, tmp_path, monkeypatch):
    conn = _conn(gw, tmp_path, _Remote())
    gw.CONNECTIONS[conn.id] = conn
    monkeypatch.setattr(gw, "_owned_conn", lambda cid, request, **k: conn)
    res = asyncio.run(gw.delete_storage({"connection_id": conn.id, "path": gw._temp_base(conn)}, None))
    assert res.status_code == 409 and "session you are in" in json.loads(res.body)["error"]
    res = asyncio.run(gw.delete_storage({"connection_id": conn.id, "path": f"{ROOT}/containers/x.sif"}, None))
    assert res.status_code == 403


def test_the_shell_writes_only_in_this_session_and_the_old_per_user_folders(gw, monkeypatch):
    from aiscientist.hpc import shell as hs_mod
    captured: dict = {}
    monkeypatch.setattr(hs_mod, "HpcShell", lambda **kw: captured.update(kw))
    monkeypatch.setattr(gw, "_hpc_user", lambda _c: "tester")
    monkeypatch.setattr(gw, "_hpc_home", lambda _c: "")
    st = HPCSettings(shared_root=ROOT)
    conn = types.SimpleNamespace(id="s1", settings=st, executor=object(), mock=False,
                                 emit=lambda *a, **k: None, workspace=None)
    gw._build_hpc_shell(conn, None)
    ws = captured["workspace"]
    assert ws.write_roots[0] == f"{ROOT}/sessions/s1"
    assert f"{ROOT}/envs/run-code-pkgs" in ws.read_roots and f"{ROOT}/pkgs" not in ws.read_roots
    assert not any("/pysrc/" in r for r in ws.read_roots)
