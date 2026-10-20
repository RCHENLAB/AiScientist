"""The storage panel's Delete runs as a Slurm job on HPC3, never as ``rm`` on a login node.

The panel lists the user's own HPC3 folders (``Temp/<user>``, ``uploads/<user>``, the personal lab
folder) and deletes an entry on a click. This file pins down:

1. **The guard**: only a clean path strictly inside one of the user's own folders, never a folder
   itself, never another user's, never with ``..``. Refused before anything reaches HPC3.
2. **The job**: a one-CPU job on the CPU partition that runs the staged script with the path
   quoted, submitted through the compute backend; the route answers at once with its id.
3. **Settling**: one ``sacct`` for the queued deletions, the job's log for what it deleted.
4. **The script**, against a real directory tree: refuses to run outside Slurm, deletes only inside
   the user's own folders, never follows a symbolic link.
5. **The mock session** still deletes at once.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.gateway import storage_delete as sd  # noqa: E402
from aiscientist.gateway.executor import ExecResult  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "hpc3" / "aiscientist_storage_delete.sh"
LAB = "/dfs3b/lab"
ROOT = f"{LAB}/software/AiScientist"
TEMP = f"{ROOT}/Temp/tester"
UPLOADS = f"{ROOT}/uploads/tester"
PERSONAL = f"{LAB}/tester"
AREAS = [TEMP, UPLOADS, PERSONAL]


# --- the guard -------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    f"{TEMP}/analysis/8f2c1d4a", f"{TEMP}/scratch/runcode", f"{UPLOADS}/pbmc.h5ad",
    f"{UPLOADS}/My Sample 2", f"{PERSONAL}/old_pbmc_run.h5ad", f"{PERSONAL}/uploads/x.h5ad",
])
def test_the_users_own_entries_may_be_deleted(path):
    assert sd.allowed(path, AREAS)


@pytest.mark.parametrize("path", [
    TEMP, UPLOADS, PERSONAL, f"{TEMP}/", "",                         # a folder itself, never
    f"{ROOT}/Temp/other/analysis/x", f"{ROOT}/uploads/other/x.h5ad", f"{LAB}/other/x.h5ad",
    f"{ROOT}/Temp/tester2/x", f"{LAB}/testerx/y",                     # a prefix is not a folder
    f"{TEMP}/../other/x", f"{UPLOADS}/a/../../other", f"{TEMP}/./x", f"{TEMP}//x",
    "Temp/tester/x", f"{TEMP}/x\nrm -rf /", f"{TEMP}/x\tb",
    f"{ROOT}/containers/analysis.sif", f"{ROOT}/bin/tester/session_clean.sh", ROOT, "/",
])
def test_anything_else_is_refused(path):
    assert not sd.allowed(path, AREAS)


# --- the job ---------------------------------------------------------------------------------------

def test_the_command_quotes_the_path_and_names_the_personal_folder_only_when_given():
    cmd = sd.command("/r/bin/tester/storage_delete.sh", ROOT + "/", "tester", [f"{UPLOADS}/My Sample 2"])
    assert cmd == (f"bash /r/bin/tester/storage_delete.sh --root {ROOT} --user tester "
                   f"--path '{UPLOADS}/My Sample 2'")
    cmd = sd.command("s.sh", ROOT, "tester", [f"{PERSONAL}/x"], personal_dir=PERSONAL)
    assert f"--personal {PERSONAL} --path {PERSONAL}/x" in cmd
    evil = sd.command("s.sh", ROOT, "tester", [f"{UPLOADS}/a'; rm -rf ~; '"])
    assert "--path '" + f"{UPLOADS}/a'\"'\"'; rm -rf ~; '\"'\"''" in evil


def test_the_job_runs_on_a_compute_node_of_the_free_partition():
    spec = sd.job_spec("bash s.sh --path x", "tester", f"{ROOT}/bin/tester/deletes/abc.log",
                       f"{ROOT}/bin/tester", partition="standard", account="ruic20_lab")
    lines = spec.script.splitlines()
    assert lines[0] == "#!/bin/bash" and lines[-1] == "bash s.sh --path x"
    for want in ("#SBATCH --job-name=aiscientist-delete-tester", "#SBATCH --partition=standard",
                 "#SBATCH --account=ruic20_lab", "#SBATCH --cpus-per-task=1",
                 f"#SBATCH --output={ROOT}/bin/tester/deletes/abc.log"):
        assert want in lines
    assert spec.job_name == "aiscientist-delete-tester" and spec.submit_dir == f"{ROOT}/bin/tester"
    assert "--account" not in sd.job_spec("p", "tester", "l", "d", partition="standard").script


def test_the_log_is_read_line_by_line():
    parsed = sd.parse_log(f"DELETED\t{UPLOADS}/a b\nSKIP\tnot there\t{UPLOADS}/gone\nnoise\n"
                          "DELETE_RESULT deleted=1 skipped=1 job=901\n")
    assert parsed["results"] == [{"verdict": "deleted", "path": f"{UPLOADS}/a b"},
                                 {"verdict": "skip", "reason": "not there", "path": f"{UPLOADS}/gone"}]
    assert parsed["summary"] == {"deleted": 1, "skipped": 1, "job": 901}
    assert sd.parse_log("")["summary"] is None


def _job(path=f"{UPLOADS}/x"):
    return {"id": "abc", "path": path, "job_id": "901", "status": "queued", "log": "l"}


def test_a_deletion_is_settled_only_once_slurm_says_it_ended():
    job = _job()
    assert sd.settle(job, "", "") is False and job["status"] == "queued"          # not in sacct yet
    assert sd.settle(job, "PENDING", "") is False and job["slurm_state"] == "PENDING"
    assert sd.settle(job, "RUNNING", "") is False and job["status"] == "queued"
    log = f"DELETED\t{UPLOADS}/x\nDELETE_RESULT deleted=1 skipped=0 job=901\n"
    assert sd.settle(job, "COMPLETED", log) is True and job["status"] == "done"
    assert sd.settle(job, "COMPLETED", log) is False                             # once


@pytest.mark.parametrize("state, log, status, said", [
    ("COMPLETED", "SKIP\tnot there\t{p}\nDELETE_RESULT deleted=0 skipped=1 job=901\n", "done",
     "it was not there any more"),
    ("COMPLETED", "SKIP\ta symbolic link on the way to it\t{p}\nDELETE_RESULT deleted=0 skipped=1 job=901\n",
     "failed", "a symbolic link on the way to it"),
    ("COMPLETED", "DELETED\t{p}\n", "failed", "without its result line"),
    ("COMPLETED", "DELETED\t{p}\nDELETE_RESULT deleted=1 skipped=0 job=777\n", "failed", "result line"),
    ("FAILED", "", "failed", "the job ended FAILED"),
    ("CANCELLED by 123", "", "failed", "the job ended CANCELLED"),
    ("TIMEOUT", "DELETE_RESULT deleted=0 skipped=0 job=901\n", "failed", "the job ended TIMEOUT"),
])
def test_how_an_ended_deletion_went(state, log, status, said):
    job = _job()
    assert sd.settle(job, state, log.format(p=job["path"])) is True
    assert job["status"] == status
    assert said in (job.get("note") or job.get("error") or "")


def test_a_session_remembers_its_last_deletions_and_every_queued_one():
    jobs: list[dict] = []
    sd.remember(jobs, {**_job("/q"), "id": "q"})
    for i in range(sd.KEEP + 5):
        sd.remember(jobs, {**_job(f"/d{i}"), "id": f"d{i}", "status": "done"})
    assert len(jobs) == sd.KEEP and jobs[0]["id"] == "q" and jobs[-1]["id"] == f"d{sd.KEEP + 4}"
    assert "log" not in sd.public(jobs[0])


# --- the script, against a real tree ---------------------------------------------------------------

def _tree(tmp_path: Path) -> Path:
    """``<tmp>/software/AiScientist`` with the user's folders, another user's, and a personal one."""
    root = tmp_path / "software" / "AiScientist"
    for rel, data in {
        "Temp/tester/analysis/run1/work/adata.h5ad": b"AAAA",
        "Temp/tester/scratch/runcode/x.out": b"o",
        "Temp/other/analysis/run9/keep.h5ad": b"K",
        "uploads/tester/pbmc.h5ad": b"MATRIX",
        "uploads/tester/My Sample 2/outs/a.h5": b"A",
        "uploads/other/theirs.h5ad": b"T",
        "containers/analysis.sif": b"SIF",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    (tmp_path / "tester" / "old").mkdir(parents=True)
    (tmp_path / "tester" / "old" / "run.h5ad").write_bytes(b"OLD")
    (tmp_path / "precious").mkdir()
    (tmp_path / "precious" / "data.h5ad").write_bytes(b"P")
    return root


def _run(root: Path, *paths: str, personal: "Path | None" = None, slurm=True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "SLURM_JOB_ID"}
    if slurm:
        env["SLURM_JOB_ID"] = "555"
    args = ["bash", str(SCRIPT), "--root", str(root), "--user", "tester"]
    if personal is not None:
        args += ["--personal", str(personal)]
    for p in paths:
        args += ["--path", p]
    return subprocess.run(args, capture_output=True, text=True, env=env)


def test_the_script_refuses_to_run_outside_slurm(tmp_path):
    root = _tree(tmp_path)
    out = _run(root, f"{root}/uploads/tester/pbmc.h5ad", slurm=False)
    assert out.returncode == 2 and "never on a login node" in out.stderr
    assert (root / "uploads/tester/pbmc.h5ad").exists()


def test_the_script_deletes_the_users_own_entries(tmp_path):
    root = _tree(tmp_path)
    personal = tmp_path / "tester"
    out = _run(root, f"{root}/Temp/tester/analysis/run1", f"{root}/uploads/tester/My Sample 2",
               f"{root}/uploads/tester/pbmc.h5ad", f"{personal}/old", personal=personal)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert f"DELETED\t{root}/uploads/tester/My Sample 2" in lines
    assert lines[-1] == "DELETE_RESULT deleted=4 skipped=0 job=555"
    assert not (root / "Temp/tester/analysis/run1").exists()
    assert not (root / "uploads/tester/My Sample 2").exists()
    assert not (root / "uploads/tester/pbmc.h5ad").exists()
    assert not (personal / "old").exists()
    for kept in ("Temp/tester/analysis", "Temp/tester/scratch/runcode/x.out", "uploads/tester",
                 "Temp/other/analysis/run9/keep.h5ad", "uploads/other/theirs.h5ad", "containers/analysis.sif"):
        assert (root / kept).exists(), kept
    assert personal.is_dir()


@pytest.mark.parametrize("make, path, reason", [
    (None, "{root}/uploads/other/theirs.h5ad", "outside the user's own folders"),
    (None, "{root}/Temp/other/analysis/run9", "outside the user's own folders"),
    (None, "{root}/containers/analysis.sif", "outside the user's own folders"),
    (None, "{root}/uploads/tester", "outside the user's own folders"),
    (None, "{root}/Temp/tester", "outside the user's own folders"),
    (None, "{root}/Temp/tester/../other/analysis/run9", "not a clean absolute path"),
    (None, "{root}/Temp/tester/./scratch", "not a clean absolute path"),
    (None, "uploads/tester/pbmc.h5ad", "not a clean absolute path"),
    (None, "{root}/uploads/tester/nope.h5ad", "not there"),
    (None, "{tmp}/tester/old", "outside the user's own folders"),          # no --personal given
    ("linkdir", "{root}/uploads/tester/via/data.h5ad", "a symbolic link on the way to it"),
])
def test_the_script_skips_what_it_must_not_delete(tmp_path, make, path, reason):
    root = _tree(tmp_path)
    if make == "linkdir":
        os.symlink(tmp_path / "precious", root / "uploads/tester/via")
    target = path.format(root=root, tmp=tmp_path)
    out = _run(root, target)
    assert out.returncode == 0, out.stderr
    assert f"SKIP\t{reason}\t{target}" in out.stdout.splitlines(), out.stdout
    assert out.stdout.splitlines()[-1] == "DELETE_RESULT deleted=0 skipped=1 job=555"
    assert (tmp_path / "precious/data.h5ad").exists()
    assert (root / "uploads/other/theirs.h5ad").exists() and (root / "Temp/other/analysis/run9").exists()


def test_the_script_never_follows_a_link(tmp_path):
    root = _tree(tmp_path)
    up = root / "uploads/tester"
    os.symlink(tmp_path / "precious", up / "shortcut")                 # an entry that is a link
    (up / "folder").mkdir()
    (up / "folder" / "own.txt").write_bytes(b"x")
    os.symlink(tmp_path / "precious", up / "folder" / "inside")        # a link inside a folder
    out = _run(root, f"{up}/shortcut", f"{up}/folder")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[-1] == "DELETE_RESULT deleted=2 skipped=0 job=555"
    assert not os.path.lexists(up / "shortcut") and not (up / "folder").exists()
    assert (tmp_path / "precious/data.h5ad").read_bytes() == b"P"      # what they pointed to stays


@pytest.mark.parametrize("args", [
    ["--root", "relative", "--user", "tester", "--path", "/x/y"],
    ["--root", "/", "--user", "tester", "--path", "/x/y"],
    ["--root", "/a/../b", "--user", "tester", "--path", "/x/y"],
    ["--root", "/a", "--user", "te/ster", "--path", "/x/y"],
    ["--root", "/a", "--user", "tester", "--personal", "/lab/other", "--path", "/x/y"],
    ["--root", "/a", "--user", "tester"],
    ["--root", "/a", "--user", "tester", "--path", "/x/y", "--force"],
])
def test_the_script_refuses_bad_arguments(args):
    out = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                         env={**os.environ, "SLURM_JOB_ID": "1"})
    assert out.returncode == 2 and out.stderr.startswith("aiscientist_storage_delete:"), out.stderr


# --- the gateway flow ------------------------------------------------------------------------------

class _Hpc:
    """HPC3 as the gateway sees it: commands recorded, sbatch numbered, sacct and logs scripted."""

    def __init__(self) -> None:
        self.username = "tester"
        self.commands: list[str] = []
        self.files: dict[str, bytes] = {}
        self.states: dict[str, str] = {}
        self.du: dict[str, str] = {}
        self.next_job = 900

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        out = ""
        if command.startswith("sbatch "):
            self.next_job += 1
            out = f"Submitted batch job {self.next_job}"
        elif command.startswith("sacct"):
            out = "".join(f"{j}|{s}\n" for j, s in self.states.items())
        elif command.startswith("du -sh "):
            out = next((v for k, v in self.du.items() if k in command), "")
        return ExecResult(command=command, exit_status=0, stdout=out, stderr="")

    def put_file(self, local: str, remote: str) -> None:
        self.files[remote] = Path(local).read_bytes()

    def read_bytes(self, path: str, max_bytes=None) -> bytes:
        return self.files.get(path, b"")


@pytest.fixture
def flow(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw
    saved = dict(gw.CONNECTIONS)
    gw.CONNECTIONS.clear()
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(shared_root=ROOT, lab_storage=LAB), mock=False, loop=loop,
                         username="tester")
    loop.close()
    conn.workspace = gw.CONSOLE_RUNS_DIR / "tester"
    conn.executor = _Hpc()
    conn.shared_root_ok = True
    conn.status = "ready"
    said: list[str] = []
    conn.emit = lambda level, stage, text, **k: said.append(text)
    gw.CONNECTIONS[conn.id] = conn
    monkeypatch.setattr(gw, "_owned_conn", lambda cid, request, **k: conn)
    yield types.SimpleNamespace(gw=gw, conn=conn, hpc=conn.executor, said=said)
    gw.CONNECTIONS.clear()
    gw.CONNECTIONS.update(saved)


def _call(coro):
    res = asyncio.run(coro)
    return res.status_code, json.loads(res.body)


def test_delete_queues_a_job_and_settles_it(flow):
    gw, conn, hpc = flow.gw, flow.conn, flow.hpc
    target = f"{UPLOADS}/My Sample 2"
    status, body = _call(gw.delete_storage({"connection_id": conn.id, "path": target}, None))
    assert status == 200, body
    assert body["status"] == "queued" and body["job"]["job_id"] == "901" and body["job"]["path"] == target
    assert "log" not in body["job"]
    assert hpc.files[f"{ROOT}/bin/tester/storage_delete.sh"] == SCRIPT.read_bytes()
    sbatch = next(c for c in hpc.commands if "cat >" in c)
    assert "#SBATCH --job-name=aiscientist-delete-tester" in sbatch
    assert "#SBATCH --partition=standard" in sbatch and "#SBATCH --account=ruic20_lab" in sbatch
    assert (f"bash {ROOT}/bin/tester/storage_delete.sh --root {ROOT} --user tester "
            f"--path '{target}'") in sbatch
    assert "--personal" not in sbatch                                  # not a personal-folder path
    assert not any(c.lstrip().startswith("rm ") for c in hpc.commands)  # nothing on a login node
    assert any("job 901, queued" in s for s in flow.said)

    # Asked again while it is queued: refused, nothing submitted.
    n = len(hpc.commands)
    status, body = _call(gw.delete_storage({"connection_id": conn.id, "path": target}, None))
    assert status == 409 and hpc.commands[n:] == []

    # The listing says it is being deleted.
    hpc.du = {f"{UPLOADS}/*": f"1.2G\t{target}\n"}
    listing = gw._list_storage(conn)
    assert next(i for i in listing["items"] if i["path"] == target)["deleting"] == "901"

    # One sacct per refresh; a running job stays queued.
    hpc.states["901"] = "RUNNING"
    status, body = _call(gw.get_storage_deletes(conn.id, None))
    assert body["deletes"][0]["status"] == "queued" and body["deletes"][0]["slurm_state"] == "RUNNING"
    assert sum(c.startswith("sacct") for c in hpc.commands) == 1

    log = f"{ROOT}/bin/tester/deletes/{conn.storage_deletes[0]['id']}.log"
    hpc.states["901"] = "COMPLETED"
    hpc.files[log] = f"DELETED\t{target}\nDELETE_RESULT deleted=1 skipped=0 job=901\n".encode()
    status, body = _call(gw.get_storage_deletes(conn.id, None))
    assert body["deletes"][0]["status"] == "done"
    assert any(s.startswith(f"Deleted on HPC3: {target} (job 901") for s in flow.said)

    # Nothing queued: no sacct at all.
    n = len(hpc.commands)
    _call(gw.get_storage_deletes(conn.id, None))
    assert hpc.commands[n:] == []


def test_a_personal_folder_entry_names_that_folder_and_the_script_is_staged_once(flow):
    gw, conn, hpc = flow.gw, flow.conn, flow.hpc
    for path in (f"{PERSONAL}/old_pbmc_run.h5ad", f"{TEMP}/analysis/8f2c1d4a"):
        status, body = _call(gw.delete_storage({"connection_id": conn.id, "path": path}, None))
        assert status == 200, body
    scripts = [c for c in hpc.commands if "cat >" in c]
    assert f"--personal {PERSONAL} --path {PERSONAL}/old_pbmc_run.h5ad" in scripts[0]
    assert "--personal" not in scripts[1]
    assert sum(c.startswith("mkdir -p") and c.endswith("/deletes") for c in hpc.commands) == 1
    assert [j["job_id"] for j in conn.storage_deletes] == ["901", "902"]


def test_a_failed_deletion_says_why(flow):
    gw, conn, hpc = flow.gw, flow.conn, flow.hpc
    _call(gw.delete_storage({"connection_id": conn.id, "path": f"{TEMP}/analysis/x"}, None))
    hpc.states["901"] = "FAILED"
    _status, body = _call(gw.get_storage_deletes(conn.id, None))
    assert body["deletes"][0]["status"] == "failed" and body["deletes"][0]["error"] == "the job ended FAILED"
    assert any(s.startswith(f"Not deleted on HPC3: {TEMP}/analysis/x") for s in flow.said)


@pytest.mark.parametrize("path", [
    f"{ROOT}/Temp/other/analysis/x", f"{ROOT}/uploads/other/theirs.h5ad", f"{LAB}/other/x",
    f"{TEMP}/../other/x", UPLOADS, f"{ROOT}/containers", "",
])
def test_other_users_paths_are_refused_before_anything_reaches_hpc3(flow, path):
    status, body = _call(flow.gw.delete_storage({"connection_id": flow.conn.id, "path": path}, None))
    assert status == 403 and "outside your own HPC3 directories" in body["error"]
    assert flow.hpc.commands == [] and flow.hpc.files == {} and flow.conn.storage_deletes == []


def test_a_submission_that_fails_is_reported_and_not_remembered(flow, monkeypatch):
    gw, conn = flow.gw, flow.conn

    def refuse(command, timeout=60.0):
        conn.executor.commands.append(command)
        ok = not command.startswith("sbatch")
        return ExecResult(command=command, exit_status=0 if ok else 1, stdout="", stderr="" if ok else "bad partition")
    monkeypatch.setattr(conn.executor, "exec", refuse)
    status, body = _call(gw.delete_storage({"connection_id": conn.id, "path": f"{TEMP}/x"}, None))
    assert status == 500 and "could not be submitted" in body["error"]
    assert conn.storage_deletes == []


def test_the_mock_session_deletes_at_once(flow):
    gw, conn = flow.gw, flow.conn
    conn.mock = True
    path = f"{TEMP}/analysis/8f2c1d4a"
    status, body = _call(gw.delete_storage({"connection_id": conn.id, "path": path}, None))
    assert status == 200 and body["status"] == "deleted"
    assert path not in [i["path"] for i in body["items"]]
    _status, body = _call(gw.get_storage_deletes(conn.id, None))
    assert body["deletes"][0]["status"] == "done"
    assert not any(c.startswith(("sbatch", "sacct", "rm ")) for c in flow.hpc.commands)
