"""My account → Storage (RCHENLAB/AiScientist issue 32), the server side.

The HPC3 listing goes area by area, says when the sweep may remove each temporary folder and reads
the lab's quota the way RCIC's ``dfsquotas`` answers; ``/api/storage/server`` lists an account's
datasets and run folders on this host with their sizes, without an HPC3 session; the account's
sessions are listed without one too; and the dead ``/api/model`` route is gone.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
import types

import pytest

from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.settings import HPCSettings

LAB = "/dfs3b/lab"
ROOT = f"{LAB}/software/AiScientist"
TEMP = f"{ROOT}/Temp/tester"
UPLOADS = f"{ROOT}/uploads/tester"
PERSONAL = f"{LAB}/tester"

# What `dfsquotas yijus12 dfs3b` printed on HPC3 on 2026-10-20 (the header is in colour).
DFSQUOTAS = ("\x1b[35m==== Group quotas on dfs3b for user yijus12\x1b[0m\n"
             "NAME        ID      TYPE   POOL   SPACE                INODE         \n"
             "ruic20_hpc  158908  group  dfs3b  574.58TiB/700.00TiB  53.94M/5.09G  \n")
# And with no arguments: its usage text, which the old panel showed as the "quota".
USAGE = ("NAME\n        dfsquotas - Check user GROUP quotas on one or more DFS filesystems\n\n"
         "SYNOPSIS\n        dfsquotas -h \n        dfsquotas UCInetID DFS \n")


@pytest.fixture()
def gw():
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw_app
    return gw_app


def test_the_lab_quota_is_read_from_the_group_row(gw):
    q = gw._parse_dfsquotas(DFSQUOTAS)
    assert q == {"group": "ruic20_hpc", "pool": "dfs3b", "used": "574.58 TiB", "limit": "700.00 TiB",
                 "percent": 82.1}
    assert gw._parse_dfsquotas(USAGE) is None
    assert gw._parse_dfsquotas("") is None


def test_a_du_line_with_and_without_its_time_column(gw):
    assert gw._du_entry(f"5592404\t1792538330\t{TEMP}/analysis/run1", TEMP) == {
        "name": "analysis/run1", "bytes": 5592404 * 1024, "path": f"{TEMP}/analysis/run1", "changed": 1792538330}
    assert gw._du_entry(f"12\t{UPLOADS}/a.h5ad\n", UPLOADS) == {"name": "a.h5ad", "bytes": 12 * 1024,
                                                                 "path": f"{UPLOADS}/a.h5ad"}
    assert gw._du_entry("du: cannot read directory", TEMP) is None
    assert gw._du_entry(f"x\t1\t{TEMP}/a", TEMP) is None


class _Hpc:
    username = "tester"

    def __init__(self, out: dict[str, str]) -> None:
        self.out = out
        self.commands: list[str] = []

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        stdout = next((v for k, v in self.out.items() if k in command), "")
        return ExecResult(command=command, exit_status=0, stdout=stdout, stderr="")


def _conn(gw, executor, *, mock=False, ttl=3):
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(shared_root=ROOT, lab_storage=LAB, temp_ttl_days=ttl), mock=mock,
                         loop=loop, username="tester")
    loop.close()
    conn.executor = executor
    return conn


def test_the_hpc3_folders_are_listed_area_by_area_with_cleanup_days_and_the_quota(gw):
    conn = _conn(gw, None)
    here = f"{ROOT}/sessions/{conn.id}"
    hpc = _Hpc({
        f"{here}/runs/*": f"5592404\t1792538330\t{here}/runs/run1\n2048\t1792362245\t{here}/inputs\n",
        "[ -O": f"100\t1792000000\t{ROOT}/sessions/old-session\n",
        f"{TEMP}/*/*": f"64\t1792000000\t{TEMP}/analysis/run0\n",
        f"{UPLOADS}/*": f"16000000\t1700000000\t{UPLOADS}/Sample2_Ddx41\n",
        f"{PERSONAL}/*": "",
        "dfsquotas tester dfs3b": DFSQUOTAS,
    })
    conn.executor = hpc
    conn.storage_deletes = [{"path": f"{UPLOADS}/Sample2_Ddx41", "job_id": "901", "status": "queued"}]
    d = gw._list_storage(conn)

    assert [a["key"] for a in d["areas"]] == ["temp", "sessions", "legacy", "uploads", "personal"]
    temp, sessions, legacy, uploads, personal = d["areas"]
    assert temp["label"] == "This session's files" and "results are copied to the server" in temp["note"]
    assert temp["path"] == here and temp["bytes"] == (5592404 + 2048) * 1024
    run1 = temp["items"][0]
    assert run1["name"] == "runs/run1" and run1["area"] == "temp"
    assert "clean_after" not in run1                                  # it goes with this session
    old = sessions["items"][0]
    assert old["name"] == "old-session" and old["clean_after"] == 1792000000 + 3 * 86400
    assert legacy["items"][0]["clean_after"] == 1792000000 + 3 * 86400
    up = uploads["items"][0]
    assert "clean_after" not in up and up["deleting"] == "901"       # uploads are never swept
    assert personal["items"] == [] and personal["bytes"] == 0
    assert d["quota"]["used"] == "574.58 TiB" and d["quota"]["percent"] == 82.1
    assert len(d["items"]) == 5
    # The quota is asked for this account on the lab storage's own file system, never bare.
    assert "dfsquotas tester dfs3b 2>/dev/null" in hpc.commands
    assert not any(c.startswith("dfsquotas 2>") for c in hpc.commands)
    assert any(c.startswith(f"du -sk --time --time-style=+%s {here}/[!r]* {here}/runs/*") for c in hpc.commands)
    # Earlier sessions: only the folders this account owns, never this session's own.
    (listing,) = [c for c in hpc.commands if "[ -O" in c]
    assert f'[ "$d" != {here} ]' in listing and "xargs -0 -r du" in listing


def test_without_a_ttl_nothing_carries_a_cleanup_day_and_an_unknown_quota_is_none(gw):
    hpc = _Hpc({f"{TEMP}/*/*": f"8\t1792538330\t{TEMP}/analysis/run1\n", "dfsquotas": USAGE})
    d = gw._list_storage(_conn(gw, hpc, ttl=0))
    assert "clean_after" not in d["areas"][2]["items"][0]
    assert d["areas"][0]["note"] == ("Removed when your session ends, once each run's results are "
                                     "copied to the server.")
    assert d["quota"] is None


def test_the_mock_lists_every_area_and_forgets_what_was_deleted(gw):
    conn = _conn(gw, _Hpc({}), mock=True)
    d = gw._list_storage(conn)
    names = {a["key"]: [i["name"] for i in a["items"]] for a in d["areas"]}
    assert names == {"temp": ["runs/8f2c1d4a", "inputs"], "sessions": [], "legacy": [], "uploads": [],
                     "personal": ["old_pbmc_run.h5ad"]}
    assert d["quota"]["pool"] == "dfs3b"
    conn.mock_deleted = {f"{PERSONAL}/old_pbmc_run.h5ad"}
    assert gw._list_storage(conn)["areas"][4]["items"] == []


def test_the_quota_is_read_before_the_personal_folder_which_comes_last(gw):
    hpc = _Hpc({"dfsquotas": DFSQUOTAS})
    gw._list_storage(_conn(gw, hpc))
    assert f"{PERSONAL}/*" in hpc.commands[-1]
    assert hpc.commands[-2].startswith("dfsquotas")


def test_a_folder_hpc3_cannot_measure_says_so_and_the_others_are_listed(gw):
    class _Fails(_Hpc):
        def exec(self, command, timeout=60.0):
            if f"{PERSONAL}/*" in command:
                assert timeout == gw.STORAGE_DU_TIMEOUT_S             # not the 60 s default
                raise RuntimeError(f"Command timed out after {timeout:.0f}s: {command}")
            if f"{TEMP}/*/*" in command:
                raise RuntimeError("SSH session not active")
            return super().exec(command, timeout)

    d = gw._list_storage(_conn(gw, _Fails({f"{UPLOADS}/*": f"8\t1700000000\t{UPLOADS}/a.h5ad\n",
                                           "dfsquotas": DFSQUOTAS})))
    areas = {a["key"]: a for a in d["areas"]}
    assert areas["personal"]["error"] == "HPC3 could not measure this folder within 5 minutes."
    assert areas["legacy"]["error"].startswith("Could not read this folder on HPC3:")
    assert areas["uploads"]["items"][0]["name"] == "a.h5ad" and "error" not in areas["uploads"]
    assert d["quota"]["percent"] == 82.1 and d["scanning"] is False


class _SlowHpc(_Hpc):
    """The member's own lab folder takes long to measure, as yijus12's did (15.8 s on 2026-10-31)."""

    def __init__(self, out: dict[str, str]) -> None:
        super().__init__(out)
        self.release = threading.Event()

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        if f"{PERSONAL}/*" in command:
            self.release.wait(10)
        return super().exec(command, timeout)


@pytest.fixture()
def slow(gw, monkeypatch):
    conn = _conn(gw, None)
    conn.executor = _SlowHpc({f"{UPLOADS}/*": f"16000000\t1700000000\t{UPLOADS}/Sample2_Ddx41\n",
                              f"{PERSONAL}/*": f"900\t1700000000\t{PERSONAL}/old_run\n",
                              "dfsquotas tester dfs3b": DFSQUOTAS})
    monkeypatch.setattr(gw, "_owned_conn", lambda cid, request, **k: conn)
    monkeypatch.setattr(gw, "STORAGE_WAIT_S", 0.5)
    yield conn
    conn.executor.release.set()


def _get(gw, conn, **kw):
    res = asyncio.run(gw.get_storage(conn.id, None, **kw))
    return res.status_code, json.loads(res.body)


def _personal_scans(hpc) -> int:
    return sum(f"{PERSONAL}/*" in c for c in hpc.commands)


def test_a_slow_folder_does_not_hold_up_the_storage_tab(gw, slow):
    conn, hpc = slow, slow.executor
    t0 = time.monotonic()
    status, d = _get(gw, conn)
    assert time.monotonic() - t0 < 3                       # far inside the ingress's 15 s
    assert status == 202 and d["scanning"] is True and d["read_at"] is None
    areas = {a["key"]: a for a in d["areas"]}
    assert areas["personal"]["pending"] is True and areas["personal"]["items"] == []
    assert areas["uploads"]["pending"] is False and areas["uploads"]["items"][0]["name"] == "Sample2_Ddx41"
    assert d["quota"]["percent"] == 82.1                   # read before the slow folder

    # Asked again while it runs: the same scan goes on, no second one starts.
    assert _get(gw, conn)[0] == 202
    hpc.release.set()
    status, d = _get(gw, conn)
    assert status == 200 and d["scanning"] is False and d["read_at"]
    assert d["areas"][4]["pending"] is False and d["areas"][4]["items"][0]["name"] == "old_run"
    assert _personal_scans(hpc) == 1

    # Just read: answered from memory, with the deletions marked as they are now.
    n = len(hpc.commands)
    conn.storage_deletes = [{"path": f"{UPLOADS}/Sample2_Ddx41", "job_id": "901", "status": "queued"}]
    status, d = _get(gw, conn)
    assert status == 200 and len(hpc.commands) == n
    assert d["areas"][3]["items"][0]["deleting"] == "901"

    # Refresh reads it again; so does a look after the listing went stale.
    assert _get(gw, conn, refresh=1)[0] == 200 and _personal_scans(hpc) == 2
    conn.storage_scan["ended"] -= gw.STORAGE_FRESH_S + 1
    assert _get(gw, conn)[0] == 200 and _personal_scans(hpc) == 3


def test_a_scan_that_fails_still_ends_so_the_tab_does_not_wait_for_ever(gw, monkeypatch):
    conn = _conn(gw, _Hpc({}))
    monkeypatch.setattr(gw, "_storage_area_info", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    scan = gw._new_storage_scan()
    gw._scan_storage_logged(conn, scan)
    assert scan["ended"] is not None


# --- the server side, per account --------------------------------------------------------------------

@pytest.fixture()
def server(tmp_path, monkeypatch, gw):
    pytest.importorskip("sqlalchemy")
    url = f"sqlite:///{(tmp_path / 'd.db').as_posix()}"
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", url)
    from aiscientist.gateway import auth_routes, db, models
    importlib.reload(db)
    importlib.reload(models)
    importlib.reload(auth_routes)
    db.reset(url)
    db.init_db()
    with db.session_scope() as s:
        user = models.User(username="yijus12", password_hash="x", role="user", is_active=True)
        other = models.User(username="bob", password_hash="x", role="user", is_active=True)
        s.add_all([user, other])
        s.commit()
        uid, bob = user.id, other.id
    results = tmp_path / "results"
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", results)
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_optional_user",
                        lambda request: types.SimpleNamespace(username="yijus12", id=uid, role="user"))
    monkeypatch.setenv("AISCIENTIST_HPC_SHARED_ROOT", ROOT)
    from fastapi.testclient import TestClient
    return types.SimpleNamespace(gw=gw, db=db, models=models, auth=auth_routes, uid=uid, bob=bob,
                                 results=results, client=TestClient(gw.app))


def _write(path, n):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * n)


def test_the_server_part_lists_datasets_and_run_folders_largest_first(server, tmp_path):
    a = server.auth
    a.record_dataset(server.uid, "small.h5ad", str(tmp_path / "data/small.h5ad"), 100, "h5ad")
    a.record_dataset(server.uid, "big", str(tmp_path / "data/big"), 9000, "folder")
    a.record_dataset(server.uid, "shared", "/data/Users/shared/x", 10 ** 9, "server-folder")
    a.record_dataset(server.uid, "old", f"{UPLOADS}/old.h5ad", 500, "h5ad")
    a.record_dataset(server.bob, "bobs", str(tmp_path / "data/bobs"), 7, "h5ad")
    a.record_run_start(server.uid, "run1", "Cluster the retina", plan_mode=False)
    a.record_run_start(server.uid, "run2", "QC only", plan_mode=False)
    a.record_run_start(server.uid, "gone", "No folder any more", plan_mode=False)
    a.record_run_start(server.bob, "bobrun", "Not mine", plan_mode=False)
    _write(server.results / "yijus12/run1/artifacts/report/report.pdf", 300)
    _write(server.results / "yijus12/run1/processed.h5ad", 700)
    _write(server.results / "yijus12/run2/artifacts/tables/qc.csv", 50)
    _write(server.results / "bob/bobrun/artifacts/x", 999)
    (server.results / "yijus12/run2/link").symlink_to(server.results / "bob/bobrun/artifacts/x")

    r = server.client.get("/api/storage/server")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [x["name"] for x in d["datasets"]] == ["big", "small.h5ad"]
    assert [x["bytes"] for x in d["datasets"]] == [9000, 100]
    assert [x["run_id"] for x in d["outputs"]] == ["run1", "run2"]
    assert [x["bytes"] for x in d["outputs"]] == [1000, 50]           # the symbolic link is not followed
    assert d["outputs"][0]["question"] == "Cluster the retina" and d["outputs"][0]["running"] is False
    assert (d["datasets_bytes"], d["outputs_bytes"], d["total_bytes"]) == (9100, 1050, 10150)
    assert (d["linked"], d["on_hpc3"]) == (1, 1)                     # counted apart, not in the total


def test_the_server_part_needs_accounts_and_a_login(server, monkeypatch):
    monkeypatch.setattr(server.gw, "_optional_user", lambda request: None)
    assert server.client.get("/api/storage/server").status_code == 401
    monkeypatch.setattr(server.gw, "_AUTH_ENABLED", False)
    assert server.client.get("/api/storage/server").status_code == 400


def test_the_accounts_sessions_are_listed_without_an_hpc3_session(server, monkeypatch):
    seen = []
    monkeypatch.setattr(server.gw.session_state, "list_sessions",
                        lambda folder, live: seen.append(folder) or [{"id": "s1", "state": "closed"}])
    r = server.client.get("/api/sessions")
    assert r.status_code == 200 and r.json()["sessions"] == [{"id": "s1", "state": "closed"}]
    assert seen == [server.results / "yijus12"]
    monkeypatch.setattr(server.gw, "_optional_user", lambda request: None)
    assert server.client.get("/api/sessions").status_code in (401, 409)


def test_the_model_route_is_gone(gw):
    from fastapi.testclient import TestClient
    assert TestClient(gw.app).post("/api/model", json={"connection_id": "x", "model": "qwen3:32b"}).status_code in (404, 405)
    assert not any(getattr(r, "path", "") == "/api/model" for r in gw.app.routes)


def test_a_cluster_session_names_its_weight_set_for_the_header(gw, monkeypatch):
    conn = _conn(gw, _Hpc({}))
    conn.cluster_model_id = "qwen38"
    monkeypatch.setattr(gw.cluster_models, "get", lambda cmid: types.SimpleNamespace(label="Qwen3.8-27B INT4"))
    ep = conn._llm_endpoint_summary()
    assert ep["kind"] == "cluster" and ep["cluster_label"] == "Qwen3.8-27B INT4"
    conn.cluster_model_id = ""
    assert conn._llm_endpoint_summary()["cluster_label"] == ""
