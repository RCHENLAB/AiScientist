"""When a session disconnects, what it alone left on HPC3 goes with it (RCHENLAB/AiScientist issue 4).

Before this, the only cleanup on the cluster was by AGE: a folder under ``Temp/<user>`` untouched
for 3 days, swept when that user next connected. A session's run folders and its copy of a
server-side dataset therefore outlived it, and the copy sat among the user's uploads, which are
never swept at all.

What this file pins down:

1. **A disconnect names exactly this session's runs.** Not a run still executing, not the earlier
   run a continuing run reads its dataset from, not a run another live session also knows.
2. **Server-side datasets are copied per session, under Temp.** Nothing is reused by the next
   session, and removing a session's copies cannot reach another session's.
3. **A dataset the user uploaded is never a target.** On HPC3 it is the only copy.
4. **The cleanup script removes only what it is told by name**, and refuses a name that could
   walk out of the user's own tree.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.gateway import hpc_gc  # noqa: E402
from aiscientist.gateway.executor import ExecResult  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

CLEAN_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "hpc3" / "aiscientist_session_clean.sh"
ROOT = "/dfs3b/lab/software/AiScientist"


class _FakeRemote:
    """Records commands and staged files; ``sbatch`` answers with a job id."""

    def __init__(self, *, fail: str = "") -> None:
        self.username = "tester"
        self.commands: list[str] = []
        self.staged: list[tuple[str, str]] = []
        self.closed = False
        self.fail = fail

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        if self.fail and self.fail in command:
            return ExecResult(command=command, exit_status=1, stdout="", stderr="boom")
        if command.startswith("sbatch"):
            return ExecResult(command=command, exit_status=0, stdout="8123456", stderr="")
        return ExecResult(command=command, exit_status=0, stdout="", stderr="")

    def put_file(self, local: str, remote: str) -> None:
        self.staged.append((local, remote))

    def close(self) -> None:
        self.closed = True

    @property
    def sbatch(self) -> list[str]:
        return [c for c in self.commands if c.startswith("sbatch")]


# --- hpc_gc: the command and the batch job ------------------------------------------------------

def test_the_command_names_every_target_and_nothing_else():
    cmd = hpc_gc.session_clean_command(
        f"{ROOT}/bin/tester/session_clean.sh", ROOT + "/", "tester",
        runs=["aaa111", "bbb222"], session="sess01", legacy_mirrors=["data_Users_shared"])
    assert cmd == (f"bash {ROOT}/bin/tester/session_clean.sh --root {ROOT} --user tester "
                   "--run aaa111 --run bbb222 --session sess01 "
                   "--legacy-mirror data_Users_shared --quiet")


def test_a_hostile_name_cannot_smuggle_a_second_command():
    cmd = hpc_gc.session_clean_command("/x/clean.sh", ROOT, "tester", runs=["a; rm -rf ~"])
    assert "--run 'a; rm -rf ~'" in cmd            # one quoted argument, which the script rejects


def test_the_cleanup_runs_as_a_batch_job_on_a_compute_node():
    remote = _FakeRemote()
    out = hpc_gc.clean_session(remote, ROOT, "tester", runs=["aaa111"], session="sess01",
                               partition="free", account="lab_acct")
    assert out == {"status": "submitted", "job_id": "8123456", "runs": 1, "session": "sess01",
                   "legacy_mirrors": 0}
    assert remote.staged == [(str(hpc_gc.CLEAN_SCRIPT_SRC), f"{ROOT}/bin/tester/session_clean.sh")]
    (submit,) = remote.sbatch
    assert "--job-name=aiscientist-sessionclean-tester" in submit
    assert "--partition=free" in submit and "--account=lab_acct" in submit
    # Its own log: the sweeper's is read back for the previous sweep's result.
    assert f"--output={ROOT}/bin/tester/session_clean.log" in submit
    assert "temp_gc.log" not in submit
    # The login node submits; it never walks or removes a tree itself.
    assert not any(c.startswith(("rm ", "find ")) for c in remote.commands)


def test_nothing_to_remove_submits_nothing():
    remote = _FakeRemote()
    assert hpc_gc.clean_session(remote, ROOT, "tester")["status"] == "nothing"
    assert remote.commands == [] and remote.staged == []


def test_a_cleanup_that_cannot_be_queued_is_reported_not_raised():
    out = hpc_gc.clean_session(_FakeRemote(fail="sbatch"), ROOT, "tester", runs=["aaa111"])
    assert out == {"status": "error", "error": "boom"}


def test_a_staged_script_is_not_staged_again():
    remote = _FakeRemote()
    hpc_gc.clean_session(remote, ROOT, "tester", session="sess01", script_path="/staged/clean.sh")
    assert remote.staged == [] and "bash /staged/clean.sh" in remote.sbatch[0]


def test_the_session_mirror_is_under_temp_and_per_session():
    a = hpc_gc.session_mirror_base(ROOT, "tester", "sess01")
    b = hpc_gc.session_mirror_base(ROOT, "tester", "sess02")
    assert a == f"{ROOT}/Temp/tester/server-data/sess01" and a != b
    assert "/uploads/" not in a


def test_the_session_job_is_not_one_a_release_would_cancel():
    """"Free my session" scancels by job-name prefix; the cleanup must outlive the session."""
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw
    assert gw._classify_session_job("aiscientist-sessionclean-tester") is None


# --- the gateway: which runs a disconnect names -------------------------------------------------

@pytest.fixture
def gw():
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app
    saved = dict(gw_app.CONNECTIONS)
    gw_app.CONNECTIONS.clear()
    yield gw_app
    gw_app.CONNECTIONS.clear()
    gw_app.CONNECTIONS.update(saved)


def _conn(gw, tmp_path, name="tester", **settings):
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(shared_root=ROOT, **settings), mock=False, loop=loop,
                         username=name)
    loop.close()
    conn.workspace = tmp_path / name
    conn.executor = _FakeRemote()
    conn.executor.username = name
    conn.shared_root_ok = True
    conn.status = "ready"
    conn.emit = lambda *a, **k: None
    gw.CONNECTIONS[conn.id] = conn
    return conn


def _finished(conn, run_id):
    run = conn.begin_run("conv", run_id)
    conn.end_run(run)
    return run


def test_a_disconnect_names_this_sessions_finished_runs(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SERVER_DATA_ROOTS", "")
    conn = _conn(gw, tmp_path)
    _finished(conn, "aaa111")
    _finished(conn, "bbb222")
    targets = gw._session_cleanup_targets(conn)
    assert targets["runs"] == ["aaa111", "bbb222"]
    assert targets["session"] == ""                 # it copied no server data: nothing to remove
    assert targets["legacy_mirrors"] == []          # server paths are not enabled here
    conn.server_data_synced = True
    assert (gw._session_cleanup_targets(conn)["session"] == gw._session_tag(conn)
            == conn.id.replace("-", "")[:12])


def test_a_run_still_executing_is_left_alone_and_so_are_the_sessions_copies(gw, tmp_path):
    conn = _conn(gw, tmp_path)
    conn.server_data_synced = True
    _finished(conn, "aaa111")
    conn.begin_run("conv", "live99")
    conn.chat_running = True
    targets = gw._session_cleanup_targets(conn)
    assert targets["runs"] == ["aaa111"] and targets["kept_busy"] == ["live99"]
    assert targets["session"] == ""                 # the live run may be reading them


def test_the_run_a_continuing_run_reads_from_is_left_alone(gw, tmp_path):
    conn = _conn(gw, tmp_path)
    _finished(conn, "first1")
    _finished(conn, "other2")
    live = conn.begin_run("conv", "second")
    live.continues = "first1"                       # its dataset is inside first1's HPC3 folder
    conn.chat_running = True
    assert gw._session_cleanup_targets(conn)["runs"] == ["other2"]


def test_a_run_another_live_session_knows_is_left_alone(gw, tmp_path):
    conn = _conn(gw, tmp_path)
    other = _conn(gw, tmp_path, name="tester2")
    _finished(conn, "mine11")
    _finished(conn, "both22")
    _finished(other, "both22")                      # e.g. resumed from a second device
    assert gw._session_cleanup_targets(conn)["runs"] == ["mine11"]
    other.status = "disconnected"                   # once that session is gone it is ours to remove
    assert gw._session_cleanup_targets(conn)["runs"] == ["mine11", "both22"]


def test_a_run_executing_in_another_session_is_left_alone(gw, tmp_path):
    conn = _conn(gw, tmp_path)
    other = _conn(gw, tmp_path, name="tester2")
    _finished(conn, "aaa111")
    other.begin_run("conv", "zzz999").continues = "aaa111"
    other.chat_running = True
    assert gw._session_cleanup_targets(conn)["runs"] == []


def test_every_run_of_a_long_session_is_remembered(gw, tmp_path):
    """``conn.runs`` keeps 64; a run that fell out of the cleanup's list could never be removed."""
    conn = _conn(gw, tmp_path)
    for i in range(80):
        _finished(conn, f"run{i:03d}")
    assert len(conn.runs) == 64
    assert len(gw._session_cleanup_targets(conn)["runs"]) == 80


def test_an_odd_run_id_never_reaches_the_command(gw, tmp_path):
    conn = _conn(gw, tmp_path)
    _finished(conn, "ok12345")
    conn.hpc_run_ids["../../uploads"] = None
    assert gw._session_cleanup_targets(conn)["runs"] == ["ok12345"]


def test_legacy_mirrors_are_the_configured_roots_only(gw, tmp_path, monkeypatch):
    shared = tmp_path / "data" / "shared"
    shared.mkdir(parents=True)
    monkeypatch.setenv("AISCIENTIST_SERVER_DATA_ROOTS", str(shared))
    conn = _conn(gw, tmp_path)
    from aiscientist.gateway import server_data as sd
    assert gw._session_cleanup_targets(conn)["legacy_mirrors"] == [sd.mirror_tag(shared.resolve())]


def test_disconnect_queues_the_cleanup_before_the_ssh_session_closes(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SERVER_DATA_ROOTS", "")
    conn = _conn(gw, tmp_path)
    conn.session_clean_script = f"{ROOT}/bin/tester/session_clean.sh"
    conn.server_data_synced = True
    _finished(conn, "aaa111")
    said: list[str] = []
    conn.emit = lambda level, stage, text, **k: said.append(text)
    conn.broadcast_status = lambda: None
    monkeypatch.setattr(gw, "_owned_conn", lambda cid, request, **k: conn)
    remote = conn.executor
    asyncio.run(gw.disconnect({"connection_id": conn.id}, None))
    (submit,) = remote.sbatch
    assert "--run aaa111" in submit and f"--session {gw._session_tag(conn)}" in submit
    assert remote.closed and remote.commands.index(submit) == len(remote.commands) - 1
    assert any("Removing this session's temporary files" in s and "8123456" in s for s in said)
    # Nothing of the user's own data is named: not the uploads dir, not a personal folder.
    assert "/uploads/" not in submit and "--legacy-mirror" not in submit
    assert conn.hpc_run_ids == {}                   # cleaned once, not named again
    # A second disconnect does not reach for the SSH session that is now closed.
    before = list(remote.commands)
    assert gw._end_session_cleanup(conn) == {"status": "skipped"}
    assert remote.commands == before


def test_the_flag_turns_the_cleanup_off(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SESSION_CLEANUP", "0")
    assert HPCSettings.from_env().session_cleanup is False
    monkeypatch.delenv("AISCIENTIST_SESSION_CLEANUP")
    assert HPCSettings.from_env().session_cleanup is True
    conn = _conn(gw, tmp_path, session_cleanup=False)
    _finished(conn, "aaa111")
    assert gw._end_session_cleanup(conn) == {"status": "skipped"}
    assert conn.executor.commands == []


def test_a_mock_or_unprepared_session_cleans_nothing(gw, tmp_path):
    conn = _conn(gw, tmp_path)
    _finished(conn, "aaa111")
    conn.shared_root_ok = False                     # the shared root was never set up
    assert gw._end_session_cleanup(conn)["status"] == "skipped"
    conn.shared_root_ok, conn.mock = True, True
    assert gw._end_session_cleanup(conn)["status"] == "skipped"
    assert conn.executor.commands == []


def test_a_failed_submit_is_a_warning_and_the_session_still_closes(gw, tmp_path, monkeypatch):
    conn = _conn(gw, tmp_path)
    conn.executor.fail = "sbatch"
    _finished(conn, "aaa111")
    said: list[str] = []
    conn.emit = lambda level, stage, text, **k: said.append(f"{level}: {text}")
    out = gw._end_session_cleanup(conn)
    assert out["status"] == "error"
    assert any(s.startswith("warning:") and "3 days" in s for s in said)
    assert "aaa111" in conn.hpc_run_ids             # still ours: a later attempt names it again
    assert conn.session_cleaned is False


def test_the_message_says_only_what_is_being_removed(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SERVER_DATA_ROOTS", "")
    conn = _conn(gw, tmp_path)
    conn.begin_run("conv", "live99")
    conn.chat_running = True                        # no finished run, and the copies are in use
    assert gw._end_session_cleanup(conn)["status"] == "nothing"
    assert conn.executor.commands == []
    conn.chat_running = False
    conn.session_cleaned = False
    conn.hpc_run_ids.clear()                        # a session that ran nothing and copied nothing
    assert gw._end_session_cleanup(conn)["status"] == "nothing"
    assert conn.executor.commands == []             # no job for nothing
    conn.session_cleaned = False
    conn.server_data_synced = True                  # it did bind a server folder
    said: list[str] = []
    conn.emit = lambda level, stage, text, **k: said.append(text)
    assert gw._end_session_cleanup(conn)["status"] == "submitted"
    assert "copies of server datasets." in said[0] and "run" not in said[0].split(":")[1]


def test_an_old_mirror_is_named_only_when_it_is_there(gw, tmp_path, monkeypatch):
    shared = tmp_path / "data" / "shared"
    shared.mkdir(parents=True)
    monkeypatch.setenv("AISCIENTIST_SERVER_DATA_ROOTS", str(shared))
    conn = _conn(gw, tmp_path)
    conn.executor.fail = "test -d"                  # no uploads/<user>/server-data on HPC3
    assert gw._end_session_cleanup(conn)["status"] == "nothing"
    assert conn.executor.sbatch == []
    conn.executor.fail, conn.session_cleaned = "", False
    assert gw._end_session_cleanup(conn)["status"] == "submitted"
    (submit,) = conn.executor.sbatch
    assert "--legacy-mirror" in submit and "--session" not in submit and "--run" not in submit
    assert f"test -d {ROOT}/uploads/tester/server-data" in conn.executor.commands


def test_the_report_bundle_is_staged_in_the_runs_own_folder(gw, tmp_path):
    """``reports/r1_artifacts`` was one directory for every run of a user; per run, the cleanup
    removes it with the run and two renders cannot stage into each other."""
    from aiscientist.gateway.slurm_report import SlurmReportRenderer
    conn = _conn(gw, tmp_path, report_on_hpc=True)
    fn = gw._build_report_render_fn(conn, "aaa111")
    assert isinstance(fn, SlurmReportRenderer)
    assert fn.remote_base == f"{ROOT}/Temp/tester/reports/aaa111"
    assert gw._build_report_render_fn(conn).remote_base == f"{ROOT}/Temp/tester/reports"


def test_what_a_run_hands_on_says_it_goes_with_the_session(gw):
    assert gw._keep_for(3, True) == "until you disconnect (at most 3 days)"
    assert gw._keep_for(7, False) == "for 7 days"


# --- the script itself, against a real tree ------------------------------------------------------

def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "AiScientist"
    for rel in (
        "Temp/tester/analysis/aaa111/work/adata.h5ad",
        "Temp/tester/analysis/zzz999/work/adata.h5ad",          # another session's run
        "Temp/tester/variant/aaa111/out.vcf",
        "Temp/tester/reports/aaa111/r1_artifacts/report.pdf",
        "Temp/tester/scratch/analysis/job.sbatch",
        "Temp/tester/server-data/sess01/data_shared/Sample3/matrix.h5",
        "Temp/tester/server-data/sess02/data_shared/Sample3/matrix.h5",   # another session's copy
        "Temp/other/analysis/aaa111/work/adata.h5ad",           # another member, same run name
        "uploads/tester/Sample3/possorted_genome_bam.bam",      # the only copy of an upload
        "uploads/tester/server-data/data_shared/Sample3/matrix.h5",       # the old shared mirror
        "uploads/tester/server-data/my_own_folder/notes.txt",   # not a mirror we made
        "containers/analysis.sif",
    ):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    return root


def _clean(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(CLEAN_SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True)


def test_the_script_removes_the_named_run_and_session_and_nothing_else(tmp_path):
    root = _tree(tmp_path)
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    dry = _clean(root, "--user", "tester", "--run", "aaa111", "--session", "sess01",
                 "--legacy-mirror", "data_shared", "--dry-run")
    assert dry.returncode == 0 and "would remove" in dry.stdout
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == before

    out = _clean(root, "--user", "tester", "--run", "aaa111", "--session", "sess01",
                 "--legacy-mirror", "data_shared")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "CLEAN_RESULT removed=5 missing=0"
    left = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert left == [
        "Temp/other/analysis/aaa111/work/adata.h5ad",
        "Temp/tester/analysis/zzz999/work/adata.h5ad",
        "Temp/tester/scratch/analysis/job.sbatch",
        "Temp/tester/server-data/sess02/data_shared/Sample3/matrix.h5",
        "containers/analysis.sif",
        "uploads/tester/Sample3/possorted_genome_bam.bam",
        "uploads/tester/server-data/my_own_folder/notes.txt",
    ]
    # The kind dirs the removal emptied are collapsed; the user's Temp dir itself stays.
    assert not (root / "Temp/tester/variant").exists()
    assert not (root / "Temp/tester/reports").exists()
    assert (root / "Temp/tester").is_dir()


def test_an_empty_folder_the_cleanup_did_not_empty_is_left(tmp_path):
    """Another session may have just made it and be about to submit a job that binds it."""
    root = _tree(tmp_path)
    (root / "Temp/tester/paperqa").mkdir()
    (root / "Temp/tester/phenotype").mkdir()               # empty, and no run of ours was in it
    out = _clean(root, "--user", "tester", "--run", "aaa111", "--session", "sess01")
    assert out.returncode == 0
    assert (root / "Temp/tester/paperqa").is_dir() and (root / "Temp/tester/phenotype").is_dir()
    assert not (root / "Temp/tester/variant").exists()     # this one it did empty
    assert (root / "Temp/tester/server-data/sess02").is_dir()


def test_the_old_mirror_parent_goes_only_when_it_is_empty(tmp_path):
    root = _tree(tmp_path)
    (root / "uploads/tester/server-data/my_own_folder/notes.txt").unlink()
    (root / "uploads/tester/server-data/my_own_folder").rmdir()
    _clean(root, "--user", "tester", "--legacy-mirror", "data_shared")
    assert not (root / "uploads/tester/server-data").exists()
    assert (root / "uploads/tester/Sample3/possorted_genome_bam.bam").exists()


def test_a_missing_target_is_counted_not_an_error(tmp_path):
    root = _tree(tmp_path)
    out = _clean(root, "--user", "tester", "--run", "nosuchrun", "--session", "nosess", "--quiet")
    assert out.returncode == 0
    assert out.stdout.strip() == "CLEAN_RESULT removed=0 missing=2"


def test_a_link_is_never_followed(tmp_path):
    root = _tree(tmp_path)
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("x")
    (root / "Temp/tester/analysis/linked1").symlink_to(precious)
    (root / "Temp/tester/server-data/sesslink").symlink_to(precious)
    out = _clean(root, "--user", "tester", "--run", "linked1", "--session", "sesslink")
    assert out.returncode == 0 and (precious / "keep.txt").exists()


@pytest.mark.parametrize("args", [
    ["--user", "tester", "--run", "../../uploads/tester"],
    ["--user", "tester", "--run", "a/b"],
    ["--user", "tester", "--run", ".."],
    ["--user", "tester", "--session", "../analysis"],
    ["--user", "tester", "--legacy-mirror", "../Sample3"],
    ["--user", "../other", "--run", "aaa111"],
    ["--user", "", "--run", "aaa111"],
    ["--run", "aaa111"],                                   # no user: never "every member"
    ["--user", "tester", "--run", ""],
    ["--user", "tester", "--everything"],
])
def test_the_script_refuses_anything_but_plain_names(tmp_path, args):
    root = _tree(tmp_path)
    before = sorted(p for p in root.rglob("*"))
    out = _clean(root, *args)
    assert out.returncode == 2 and "aiscientist_session_clean:" in out.stderr
    assert sorted(p for p in root.rglob("*")) == before


@pytest.mark.parametrize("bad_root", ["relative/root", "/", "/a/../b"])
def test_the_script_refuses_a_root_it_cannot_trust(bad_root):
    out = subprocess.run(["bash", str(CLEAN_SCRIPT), "--root", bad_root, "--user", "tester",
                          "--run", "aaa111"], capture_output=True, text=True)
    assert out.returncode == 2
