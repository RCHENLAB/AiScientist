"""Removing the HPC3 originals of datasets now kept on the server (RCHENLAB/AiScientist issue 6).

A dataset kept only on HPC3 is copied to the server at its owner's connect, and the copy's
``source.json`` names the HPC3 original, which stays. Removing those originals is explicit and in
two Slurm jobs: a dry run that checks every one, then, on the user's word, the removal of what it
found. This file pins down:

1. **What is never a candidate**: another HPC3 account's copy, anything outside the session's own
   uploads folder, the personal lab folder unless the deployment allows it, an original whose
   server copy is incomplete or not what the dataset reads, one a row still reads on HPC3.
2. **The removal follows a finished dry run**, within a day, and takes only what that dry run would
   remove and is still unchanged now.
3. **The script removes only what it is told and checks each one itself**: it refuses to run outside
   Slurm, never follows a link, never leaves the user's folders, and skips any size mismatch.
4. **The gateway flow**: check submits a dry-run job, the removal waits for it, and the removed
   originals are recorded in their ``source.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.gateway import dataset_storage, hpc_originals as ho  # noqa: E402
from aiscientist.gateway.executor import ExecResult  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "hpc3" / "aiscientist_remove_originals.sh"
LAB = "/dfs3b/lab"
ROOT = f"{LAB}/software/AiScientist"
UPLOADS = f"{ROOT}/uploads/tester"
PERSONAL = f"{LAB}/tester"


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    return tmp_path / "data"


def _homed(owner: str, hpc_path: str, files: "dict[str, bytes]", *, user="tester", kind=None,
           extra: "dict | None" = None) -> tuple[str, Path]:
    """A dataset brought home from HPC3, as ``_home_dataset`` leaves it; returns (uuid, row path)."""
    import uuid
    identity = str(uuid.uuid4())
    tree = dataset_storage.dataset_dir(identity, owner)
    original = tree / "original"
    for rel, data in files.items():
        (original / rel).parent.mkdir(parents=True, exist_ok=True)
        (original / rel).write_bytes(data)
    source = {"hpc3_path": hpc_path, "hpc3_user": user, "bytes": sum(map(len, files.values()))}
    if kind:
        source["kind"] = kind
    source.update(extra or {})
    (tree / "source.json").write_text(json.dumps(source))
    is_file = kind == "file" or (kind is None and len(files) == 1 and "/" not in next(iter(files)))
    row = original / next(iter(files)) if is_file else original
    return identity, row


def _candidates(owner="tester", rows=None, on_hpc=(), allow=False):
    return ho.candidates(owner, "tester", uploads_dir=UPLOADS, personal_dir=PERSONAL,
                         rows_by_uuid=rows or {}, still_on_hpc=on_hpc, allow_personal=allow)


# --- what may be removed -------------------------------------------------------------------------

def test_an_upload_brought_home_is_a_candidate(data_root):
    uid, row = _homed("tester", f"{UPLOADS}/pbmc.h5ad", {"pbmc.h5ad": b"MATRIX"}, kind="file")
    fid, frow = _homed("tester", f"{UPLOADS}/Sample2", {"outs/a.h5": b"A", "outs/b/c.tsv": b"BC"},
                       kind="dir")
    check, kept = _candidates(rows={uid: str(row), fid: str(frow)})
    assert kept == []
    by_path = {c["path"]: c for c in check}
    assert by_path[f"{UPLOADS}/pbmc.h5ad"] == {"path": f"{UPLOADS}/pbmc.h5ad", "kind": "file", "bytes": 6,
                                               "dataset": uid, "name": "pbmc.h5ad", "personal": False}
    assert by_path[f"{UPLOADS}/Sample2"]["kind"] == "dir" and by_path[f"{UPLOADS}/Sample2"]["bytes"] == 3


def test_the_kind_is_read_off_an_older_copy_that_does_not_say_it(data_root):
    uid, row = _homed("tester", f"{UPLOADS}/x.h5ad", {"x.h5ad": b"12"})
    fid, frow = _homed("tester", f"{UPLOADS}/folder", {"a.csv": b"1", "b.csv": b"2"})
    check, _ = _candidates(rows={uid: str(row), fid: str(frow)})
    assert {c["name"]: c["kind"] for c in check} == {"x.h5ad": "file", "folder": "dir"}


@pytest.mark.parametrize("hpc_path, user, reason", [
    (f"{ROOT}/uploads/other/x.h5ad", "tester", "outside your HPC3 uploads folder"),
    (f"{LAB}/other/AiScientist_results/x.h5ad", "tester", "outside your HPC3 uploads folder"),
    (f"{UPLOADS}/x.h5ad", "someone", "copied by the HPC3 account someone"),
    (f"{UPLOADS}/../other/x.h5ad", "tester", "not a clean absolute path"),
    ("relative/x.h5ad", "tester", "not a clean absolute path"),
    (UPLOADS, "tester", "outside your HPC3 uploads folder"),            # the folder itself, never
    (f"{PERSONAL}/AiScientist_results/x.h5ad", "tester", "personal lab folder"),
    (f"{PERSONAL}/uploads/x.h5ad", "tester", "personal lab folder"),
])
def test_what_is_never_a_candidate(data_root, hpc_path, user, reason):
    uid, row = _homed("tester", hpc_path, {"x.h5ad": b"1"}, user=user, kind="file")
    check, kept = _candidates(rows={uid: str(row)})
    assert check == [] and reason in kept[0]["reason"]


def test_the_personal_folder_only_when_the_deployment_allows_it(data_root, monkeypatch):
    uid, row = _homed("tester", f"{PERSONAL}/AiScientist_results/x.h5ad", {"x.h5ad": b"1"}, kind="file")
    assert ho.personal_allowed({}) is False
    assert ho.personal_allowed({ho.PERSONAL_ENV: "1"}) is True
    check, _ = _candidates(rows={uid: str(row)}, allow=True)
    assert check[0]["personal"] is True


def test_an_incomplete_or_unread_copy_keeps_its_original(data_root):
    uid, row = _homed("tester", f"{UPLOADS}/a.h5ad", {"a.h5ad": b"1234"}, kind="file")
    (row).write_bytes(b"12")                                           # the copy lost bytes
    vid, vrow = _homed("tester", f"{UPLOADS}/b.h5ad", {"b.h5ad": b"1"}, kind="file")
    wid, _w = _homed("tester", f"{UPLOADS}/c.h5ad", {"c.h5ad": b"1"}, kind="file")
    check, kept = _candidates(rows={uid: str(row), vid: "/elsewhere/b.h5ad"})
    reasons = {k["name"]: k["reason"] for k in kept}
    assert check == []
    assert "holds 2 of 4 bytes" in reasons["a.h5ad"]
    assert reasons["b.h5ad"] == "no dataset reads this server copy"     # the row points elsewhere
    assert reasons["c.h5ad"] == "no dataset reads this server copy"     # no row at all


def test_an_original_a_row_still_reads_on_hpc3_is_kept(data_root):
    uid, row = _homed("tester", f"{UPLOADS}/Sample2", {"a.h5": b"A"}, kind="dir")
    check, kept = _candidates(rows={uid: str(row)}, on_hpc=[f"{UPLOADS}/Sample2/a.h5"])
    assert check == [] and kept[0]["reason"] == "a dataset still reads it on HPC3"


def test_an_original_already_removed_is_not_listed(data_root):
    uid, row = _homed("tester", f"{UPLOADS}/a.h5ad", {"a.h5ad": b"1"}, kind="file",
                      extra={"hpc3_removed": {"at": "x"}})
    assert _candidates(rows={uid: str(row)}) == ([], [])


def test_another_accounts_folder_is_never_read(data_root):
    uid, row = _homed("someone", f"{UPLOADS}/a.h5ad", {"a.h5ad": b"1"}, kind="file")
    assert _candidates(rows={uid: str(row)}) == ([], [])


# --- the job --------------------------------------------------------------------------------------

def test_the_manifest_and_the_command():
    entries = [{"kind": "file", "bytes": 6, "path": f"{UPLOADS}/a b.h5ad"}]
    assert ho.manifest(entries) == f"file\t6\t{UPLOADS}/a b.h5ad\n"
    cmd = ho.command("/s/remove.sh", ROOT + "/", "tester", "/s/p.check.tsv")
    assert cmd == f"bash /s/remove.sh --root {ROOT} --user tester --manifest /s/p.check.tsv --dry-run"
    cmd = ho.command("/s/remove.sh", ROOT, "tester", "/s/p.remove.tsv", personal_dir=PERSONAL,
                     dry_run=False)
    assert cmd.endswith(f"--personal {PERSONAL}") and "--dry-run" not in cmd


def test_the_job_runs_on_a_compute_node_of_the_free_partition():
    spec = ho.job_spec("bash x", "tester", "/b/o.log", "/b", partition="free", account="lab")
    lines = spec.script.splitlines()
    assert lines[0] == "#!/bin/bash" and lines[-1] == "bash x"
    assert "#SBATCH --partition=free" in lines and "#SBATCH --account=lab" in lines
    assert "#SBATCH --output=/b/o.log" in lines and "#SBATCH --cpus-per-task=1" in lines
    assert spec.job_name == "aiscientist-originals-tester" and spec.submit_dir == "/b"


def test_a_release_of_the_session_never_cancels_it():
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw
    assert gw._classify_session_job("aiscientist-originals-tester") is None


def test_the_log_is_read_line_by_line():
    log = (f"WOULD_REMOVE\t6\t{UPLOADS}/a.h5ad\n"
           f"SKIP\tsize 5 here, 6 on the server\t{UPLOADS}/b.h5ad\n"
           "ORIGINALS_RESULT checked=2 would_remove=1 removed=0 skipped=1 bytes=6 dry_run=1 job=55\n")
    parsed = ho.parse_log(log)
    assert parsed["results"] == [
        {"verdict": "would_remove", "bytes": 6, "path": f"{UPLOADS}/a.h5ad"},
        {"verdict": "skip", "reason": "size 5 here, 6 on the server", "path": f"{UPLOADS}/b.h5ad"}]
    assert parsed["summary"]["would_remove"] == 1 and parsed["summary"]["job"] == 55


def test_a_job_is_settled_only_once_slurm_says_it_ended():
    job = {"job_id": "55", "status": "queued"}
    assert ho.settle_job(job, "PENDING", "") is False and job["slurm_state"] == "PENDING"
    assert ho.settle_job(job, "", "") is False
    log = "ORIGINALS_RESULT checked=0 would_remove=0 removed=0 skipped=0 bytes=0 dry_run=1 job=55\n"
    assert ho.settle_job(job, "COMPLETED", log) is True and job["status"] == "done"
    bad = {"job_id": "56", "status": "queued"}
    assert ho.settle_job(bad, "TIMEOUT", "") and bad["status"] == "failed" and "TIMEOUT" in bad["error"]
    cut = {"job_id": "57", "status": "queued"}
    assert ho.settle_job(cut, "COMPLETED", "WOULD_REMOVE\t1\t/x\n") and cut["status"] == "failed"


def _checked(plan, results, *, age=0.0):
    plan["created_ts"] = time.time() - age
    plan["check"] = {"job_id": "55", "status": "done", "results": results}
    return plan


def test_the_removal_takes_only_what_the_dry_run_found_and_is_unchanged():
    a = {"path": f"{UPLOADS}/a", "kind": "file", "bytes": 6, "dataset": "u1"}
    b = {"path": f"{UPLOADS}/b", "kind": "file", "bytes": 7, "dataset": "u2"}
    c = {"path": f"{UPLOADS}/c", "kind": "file", "bytes": 8, "dataset": "u3"}
    plan = ho.new_plan("tester", "tester", [a, b, c], [], personal=False)
    assert ho.removable(plan, [a, b, c]) == ([], "the check on HPC3 has not finished")
    _checked(plan, [{"verdict": "would_remove", "path": a["path"], "bytes": 6},
                    {"verdict": "would_remove", "path": b["path"], "bytes": 7},
                    {"verdict": "skip", "path": c["path"], "reason": "size"}])
    entries, why = ho.removable(plan, [a, {**b, "bytes": 9}, c])     # b's copy changed since
    assert [e["path"] for e in entries] == [a["path"]] and why == ""
    assert ho.removable(_checked(plan, plan["check"]["results"], age=ho.PLAN_TTL_S + 1), [a])[1].startswith("the check is more")
    plan["created_ts"] = time.time()
    plan["remove"] = {"job_id": "56", "status": "queued"}
    assert ho.removable(plan, [a])[1] == "these originals were already submitted for removal"


def test_the_removed_originals_are_recorded_in_their_source_json(data_root):
    uid, row = _homed("tester", f"{UPLOADS}/a.h5ad", {"a.h5ad": b"123456"}, kind="file")
    vid, vrow = _homed("tester", f"{UPLOADS}/b.h5ad", {"b.h5ad": b"1"}, kind="file")
    check, _ = _candidates(rows={uid: str(row), vid: str(vrow)})
    plan = ho.new_plan("tester", "tester", check, [], personal=False)
    plan["remove"] = {"job_id": "56", "status": "done", "results": [
        {"verdict": "removed", "path": f"{UPLOADS}/a.h5ad", "bytes": 6},
        {"verdict": "skip", "path": f"{UPLOADS}/b.h5ad", "reason": "size"}]}
    assert ho.record_removed("tester", plan) == 1
    source = json.loads((dataset_storage.dataset_dir(uid, "tester") / "source.json").read_text())
    assert source["hpc3_removed"]["job_id"] == "56" and "was removed" in source["note"]
    assert "hpc3_removed" not in json.loads((dataset_storage.dataset_dir(vid, "tester") / "source.json").read_text())
    assert ho.record_removed("tester", plan) == 0                       # once
    assert _candidates(rows={uid: str(row), vid: str(vrow)})[0] == [c for c in check if c["dataset"] == vid]


def test_plans_are_kept_per_account(tmp_path):
    for i in range(ho.KEEP_PLANS + 2):
        ho.save_plan(tmp_path, {"id": f"p{i:02d}"})
    plans = ho.load_plans(tmp_path)
    assert len(plans) == ho.KEEP_PLANS and ho.get_plan(tmp_path, f"p{ho.KEEP_PLANS + 1:02d}")
    assert ho.get_plan(tmp_path, "p00") is None


# --- the script, against a real tree ----------------------------------------------------------------

def _tree(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "software" / "AiScientist"
    files = {
        "uploads/tester/pbmc.h5ad": b"MATRIX",
        "uploads/tester/Sample2/outs/a.h5": b"A",
        "uploads/tester/Sample2/outs/b/c.tsv": b"BC",
        "uploads/tester/Sample3/x.h5": b"XYZ",
        "uploads/tester/hidden/.DS_Store": b"z",
        "uploads/tester/hidden/m.h5": b"M",
        "uploads/other/theirs.h5ad": b"THEIRS",
        "containers/analysis.sif": b"SIF",
    }
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    personal = tmp_path.resolve() / "lab" / "tester" / "AiScientist_results"
    personal.mkdir(parents=True)
    (personal / "saved.h5ad").write_bytes(b"SAVED")
    return root


def _run(root: Path, lines: "list[tuple[str, int, str]]", *extra: str, slurm=True) -> subprocess.CompletedProcess:
    manifest = root.parent / "m.tsv"
    manifest.write_text("".join(f"{k}\t{b}\t{p}\n" for k, b, p in lines))
    env = {k: v for k, v in os.environ.items() if k != "SLURM_JOB_ID"}
    if slurm:
        env["SLURM_JOB_ID"] = "555"
    return subprocess.run(["bash", str(SCRIPT), "--root", str(root), "--user", "tester",
                           "--manifest", str(manifest), *extra], capture_output=True, text=True, env=env)


def _files(base: Path) -> list[str]:
    """Every file under ``base`` but the manifests the tests write."""
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*")
                  if p.is_file() and p.suffix != ".tsv")


def test_the_script_refuses_to_run_outside_slurm(tmp_path):
    root = _tree(tmp_path)
    out = _run(root, [("file", 6, f"{root}/uploads/tester/pbmc.h5ad")], slurm=False)
    assert out.returncode == 2 and "never on a login node" in out.stderr
    assert (root / "uploads/tester/pbmc.h5ad").exists()


def test_the_dry_run_removes_nothing_and_the_run_removes_what_matches(tmp_path):
    root = _tree(tmp_path)
    before = _files(tmp_path)
    up = f"{root}/uploads/tester"
    lines = [("file", 6, f"{up}/pbmc.h5ad"), ("dir", 3, f"{up}/Sample2"), ("dir", 2, f"{up}/Sample3")]
    dry = _run(root, lines, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert _files(tmp_path) == before
    out = dry.stdout.splitlines()
    assert f"WOULD_REMOVE\t6\t{up}/pbmc.h5ad" in out and f"WOULD_REMOVE\t3\t{up}/Sample2" in out
    assert f"SKIP\tsize 3 here, 2 on the server\t{up}/Sample3" in out
    assert out[-1] == "ORIGINALS_RESULT checked=3 would_remove=2 removed=0 skipped=1 bytes=9 dry_run=1 job=555"
    real = _run(root, lines)
    assert real.returncode == 0, real.stderr
    assert real.stdout.splitlines()[-1] == ("ORIGINALS_RESULT checked=3 would_remove=0 removed=2 "
                                            "skipped=1 bytes=9 dry_run=0 job=555")
    left = _files(tmp_path)
    assert "software/AiScientist/uploads/tester/pbmc.h5ad" not in left
    assert not (root / "uploads/tester/Sample2").exists()
    assert "software/AiScientist/uploads/tester/Sample3/x.h5" in left
    assert "software/AiScientist/uploads/other/theirs.h5ad" in left
    assert (root / "uploads/tester").is_dir()                         # the folder itself stays


@pytest.mark.parametrize("make, line, reason", [
    (None, ("file", 6, "{root}/uploads/other/theirs.h5ad"), "outside the user's own folders"),
    (None, ("file", 3, "{root}/containers/analysis.sif"), "outside the user's own folders"),
    (None, ("dir", 0, "{root}/uploads/tester"), "outside the user's own folders"),
    (None, ("file", 6, "{root}/uploads/tester/../other/theirs.h5ad"), "not a clean absolute path"),
    (None, ("file", 6, "uploads/tester/pbmc.h5ad"), "not a clean absolute path"),
    (None, ("dir", 6, "{root}/uploads/tester/pbmc.h5ad"), "not a folder"),
    (None, ("file", 3, "{root}/uploads/tester/Sample2"), "not a file"),
    (None, ("dir", 2, "{root}/uploads/tester/hidden"), "hidden files"),
    (None, ("file", 9, "{root}/uploads/tester/nope.h5ad"), "not there"),
    (None, ("file", 5, "{personal}/AiScientist_results/saved.h5ad"), "outside the user's own folders"),
    ("link", ("file", 6, "{root}/uploads/tester/link.h5ad"), "a symbolic link"),
    ("linkdir", ("file", 6, "{root}/uploads/tester/via/pbmc.h5ad"), "a symbolic link on the way"),
    ("innerlink", ("dir", 3, "{root}/uploads/tester/Sample2"), "holds a symbolic link"),
])
def test_the_script_skips_what_it_must_not_remove(tmp_path, make, line, reason):
    root = _tree(tmp_path)
    personal = tmp_path.resolve() / "lab" / "tester"
    if make == "link":
        (root / "uploads/tester/link.h5ad").symlink_to(root / "uploads/tester/pbmc.h5ad")
    elif make == "linkdir":
        (root / "uploads/tester/via").symlink_to(root / "uploads/tester")
    elif make == "innerlink":
        (root / "uploads/tester/Sample2/outs/ln").symlink_to(root / "containers/analysis.sif")
    before = _files(tmp_path)
    kind, size, path = line
    out = _run(root, [(kind, size, path.format(root=root, personal=personal))])
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[0].startswith("SKIP\t") and reason in out.stdout.splitlines()[0]
    assert _files(tmp_path) == before


def test_the_personal_folder_only_with_its_flag_and_only_the_users_own(tmp_path):
    root = _tree(tmp_path)
    personal = tmp_path.resolve() / "lab" / "tester"
    saved = personal / "AiScientist_results" / "saved.h5ad"
    out = _run(root, [("file", 5, str(saved))], "--personal", str(personal))
    assert out.returncode == 0 and f"REMOVED\t5\t{saved}" in out.stdout and not saved.exists()
    other = tmp_path.resolve() / "lab" / "other"
    out = _run(root, [("file", 5, str(saved))], "--personal", str(other))
    assert out.returncode == 2 and "must be the folder of tester" in out.stderr


@pytest.mark.parametrize("args", [
    ["--root", "relative", "--user", "tester"],
    ["--root", "/", "--user", "tester"],
    ["--root", "/x/../y", "--user", "tester"],
    ["--root", "/x", "--user", "../other"],
    ["--root", "/x", "--user", ""],
])
def test_the_script_refuses_bad_arguments(tmp_path, args):
    manifest = tmp_path / "m.tsv"
    manifest.write_text("")
    out = subprocess.run(["bash", str(SCRIPT), *args, "--manifest", str(manifest)],
                         capture_output=True, text=True, env={**os.environ, "SLURM_JOB_ID": "1"})
    assert out.returncode == 2


# --- the gateway flow ----------------------------------------------------------------------------

class _Hpc:
    """Records commands and staged files; sbatch submits, sacct answers from ``states``."""

    def __init__(self) -> None:
        self.username = "tester"
        self.commands: list[str] = []
        self.files: dict[str, bytes] = {}
        self.states: dict[str, str] = {}
        self.next_job = 900

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        out = ""
        if command.startswith("sbatch "):
            self.next_job += 1
            out = f"Submitted batch job {self.next_job}"
        elif command.startswith("sacct"):
            out = "".join(f"{j}|{s}\n" for j, s in self.states.items())
        return ExecResult(command=command, exit_status=0, stdout=out, stderr="")

    def put_file(self, local: str, remote: str) -> None:
        self.files[remote] = Path(local).read_bytes()

    def read_bytes(self, path: str, max_bytes=None) -> bytes:
        return self.files.get(path, b"")


@pytest.fixture
def flow(tmp_path, monkeypatch, data_root):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw, auth_routes
    saved = dict(gw.CONNECTIONS)
    gw.CONNECTIONS.clear()
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(shared_root=ROOT, lab_storage=LAB), mock=False, loop=loop,
                         username="tester")
    loop.close()
    conn.workspace = gw.CONSOLE_RUNS_DIR / "tester"
    conn.executor = _Hpc()
    conn.shared_root_ok = True
    conn.app_user_id = 1
    conn.status = "ready"
    said: list[str] = []
    conn.emit = lambda level, stage, text, **k: said.append(text)
    gw.CONNECTIONS[conn.id] = conn
    rows: list[dict] = []
    monkeypatch.setattr(auth_routes, "list_dataset_records", lambda uid: rows)
    monkeypatch.setattr(gw, "_owned_conn", lambda cid, request, **k: conn)
    yield types.SimpleNamespace(gw=gw, conn=conn, rows=rows, said=said)
    gw.CONNECTIONS.clear()
    gw.CONNECTIONS.update(saved)


def _call(coro):
    res = asyncio.run(coro)
    return res.status_code, json.loads(res.body)


def test_check_then_remove(flow):
    gw, conn, hpc = flow.gw, flow.conn, flow.conn.executor
    uid, row = _homed("tester", f"{UPLOADS}/pbmc.h5ad", {"pbmc.h5ad": b"MATRIX"}, kind="file")
    flow.rows.append({"uuid": uid, "path": str(row), "kind": "h5ad"})
    status, body = _call(gw.check_hpc_originals({"connection_id": conn.id}, None))
    assert status == 200, body
    plan = body["plan"]
    assert plan["check"]["job_id"] == "901" and plan["check"]["status"] == "queued"
    paths = ho.remote_paths(f"{ROOT}/sessions/{conn.id}", plan["id"], "check")
    assert hpc.files[paths["manifest"]] == f"file\t6\t{UPLOADS}/pbmc.h5ad\n".encode()
    assert paths["script"] in hpc.files
    sbatch_script = next(c for c in hpc.commands if c.startswith("mkdir -p") and "cat >" in c)
    assert "--dry-run" in sbatch_script and "#SBATCH --job-name=aiscientist-originals-tester" in sbatch_script
    assert not any(c.startswith("rm ") for c in hpc.commands)          # nothing on a login node

    # The removal waits for the dry run.
    status, body = _call(gw.remove_hpc_originals({"connection_id": conn.id, "plan_id": plan["id"]}, None))
    assert status == 409 and "has not finished" in body["error"]

    hpc.states["901"] = "COMPLETED"
    hpc.files[paths["log"]] = (f"WOULD_REMOVE\t6\t{UPLOADS}/pbmc.h5ad\n"
                               "ORIGINALS_RESULT checked=1 would_remove=1 removed=0 skipped=0 bytes=6 "
                               "dry_run=1 job=901\n").encode()
    status, body = _call(gw.get_hpc_originals(None, connection_id=conn.id))
    assert body["plan"]["check"]["status"] == "done" and body["plan"]["would_remove"] == 1

    status, body = _call(gw.remove_hpc_originals({"connection_id": conn.id, "plan_id": plan["id"]}, None))
    assert status == 200, body
    assert body["plan"]["remove"]["job_id"] == "902"
    rpaths = ho.remote_paths(f"{ROOT}/sessions/{conn.id}", plan["id"], "remove")
    assert hpc.files[rpaths["manifest"]] == f"file\t6\t{UPLOADS}/pbmc.h5ad\n".encode()

    hpc.states["902"] = "COMPLETED"
    hpc.files[rpaths["log"]] = (f"REMOVED\t6\t{UPLOADS}/pbmc.h5ad\n"
                                "ORIGINALS_RESULT checked=1 would_remove=0 removed=1 skipped=0 bytes=6 "
                                "dry_run=0 job=902\n").encode()
    status, body = _call(gw.get_hpc_originals(None, connection_id=conn.id, plan_id=plan["id"]))
    assert body["plan"]["removed"] == 1 and body["plan"]["marked"] == 1
    source = json.loads((dataset_storage.dataset_dir(uid, "tester") / "source.json").read_text())
    assert source["hpc3_removed"]["job_id"] == "902"
    assert any("Removed 1 HPC3 original" in s for s in flow.said)
    # A second check finds nothing left to remove, and submits no job.
    n = len(hpc.commands)
    status, body = _call(gw.check_hpc_originals({"connection_id": conn.id}, None))
    assert body["plan"]["check"] is None and body["plan"]["candidates"] == []
    assert not any(c.startswith("sbatch") for c in hpc.commands[n:])


def test_no_removal_while_a_run_is_executing(flow):
    gw, conn = flow.gw, flow.conn
    uid, row = _homed("tester", f"{UPLOADS}/a.h5ad", {"a.h5ad": b"1"}, kind="file")
    flow.rows.append({"uuid": uid, "path": str(row), "kind": "h5ad"})
    _status, body = _call(gw.check_hpc_originals({"connection_id": conn.id}, None))
    run = conn.begin_run("conv", "live01")
    run.running = True
    status, body = _call(gw.remove_hpc_originals({"connection_id": conn.id, "plan_id": body["plan"]["id"]}, None))
    assert status == 409 and "run is executing" in body["error"]


def test_originals_need_a_real_session_and_a_data_root(flow, monkeypatch):
    gw, conn = flow.gw, flow.conn
    conn.mock = True
    with pytest.raises(gw._Refused) as refused:
        asyncio.run(gw.check_hpc_originals({"connection_id": conn.id}, None))
    assert refused.value.response.status_code == 409
    conn.mock = False
    monkeypatch.delenv("AISCIENTIST_DATA_ROOT")
    with pytest.raises(gw._Refused) as refused:
        asyncio.run(gw.check_hpc_originals({"connection_id": conn.id}, None))
    assert refused.value.response.status_code == 400
    assert conn.executor.commands == []                                # nothing reached HPC3


def test_a_plan_of_another_hpc3_account_is_not_shown(flow):
    gw, conn = flow.gw, flow.conn
    ho.save_plan(conn.workspace, {"id": "aaaaaaaaaaaa", "hpc_user": "someone", "candidates": []})
    _status, body = _call(gw.get_hpc_originals(None, connection_id=conn.id))
    assert body["plan"] is None
    status, body = _call(gw.remove_hpc_originals({"connection_id": conn.id, "plan_id": "aaaaaaaaaaaa"}, None))
    assert status == 404


def test_a_removed_original_still_leads_a_run_to_its_copy(flow):
    """A chat that attached the HPC3 path before the move still names it. Once the original is
    removed, the run reads the copy here (``app._kept_copy_of``): source.json keeps naming it."""
    gw, conn = flow.gw, flow.conn
    uid, row = _homed("tester", f"{UPLOADS}/Sample2", {"outs/a.h5": b"A"}, kind="dir")
    flow.rows.append({"uuid": uid, "path": str(row), "kind": "folder"})
    check, _ = _candidates(rows={uid: str(row)})
    plan = ho.new_plan("tester", "tester", check, [], personal=False)
    plan["remove"] = {"job_id": "56", "status": "done", "results": [
        {"verdict": "removed", "path": f"{UPLOADS}/Sample2", "bytes": 1}]}
    assert ho.record_removed("tester", plan) == 1
    assert gw._kept_copy_of(conn, f"{UPLOADS}/Sample2") == str(row)
    assert gw._kept_copy_of(conn, f"{UPLOADS}/Sample2/outs/a.h5") == str(row / "outs" / "a.h5")
