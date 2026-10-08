"""Binding a dataset that already lives on the gateway host, by path.

The lab's data sits on the eyeserver (/data/Users/shared/...). These tests cover the allowlist (the
only thing between a browser request and the host's disks), what a Cell Ranger folder copies to HPC3,
the incremental copy, and how a run admits bound paths."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from aiscientist.gateway import server_data as sd


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """An allowlisted root holding one Cell Ranger pipestance, plus a folder outside it."""
    root = (tmp_path / "shared").resolve()
    outs = root / "lab" / "Sample3" / "outs"
    (outs / "analysis").mkdir(parents=True)
    for name, size in {"filtered_feature_bc_matrix.h5": 30, "raw_feature_bc_matrix.h5": 90,
                       "molecule_info.h5": 500, "cloupe.cloupe": 400, "web_summary.html": 5,
                       "metrics_summary.csv": 2, "possorted_genome_bam.bam": 1000,
                       "possorted_genome_bam.bam.bai": 10}.items():
        (outs / name).write_bytes(b"x" * size)
    (outs / "analysis" / "clusters.csv").write_bytes(b"c")
    stance = root / "lab" / "Sample3"
    (stance / "SC_RNA_COUNTER_CS" / "fork0").mkdir(parents=True)
    (stance / "SC_RNA_COUNTER_CS" / "fork0" / "chunk.bam").write_bytes(b"y" * 700)
    (stance / "_log").write_bytes(b"l")
    (stance / "notes.txt").write_bytes(b"the user's own note")
    secret = (tmp_path / "private").resolve()
    secret.mkdir()
    (secret / "keys.txt").write_text("nope")
    monkeypatch.setenv(sd.ROOTS_ENV, f"{root}:{tmp_path / 'missing'}")
    return root, secret


def test_roots_come_from_the_environment_and_missing_ones_are_dropped(shared):
    root, _ = shared
    assert sd.server_data_roots() == (root,)
    assert sd.server_data_roots({}) == ()                       # unset = the feature is off


def test_a_path_inside_the_allowlist_resolves(shared):
    root, _ = shared
    path, got_root = sd.resolve(f"  '{root}/lab/Sample3/'  ", sd.server_data_roots())
    assert path == root / "lab" / "Sample3" and got_root == root


@pytest.mark.parametrize("make", [
    lambda root, secret: str(secret / "keys.txt"),                          # elsewhere
    lambda root, secret: f"{root}/lab/../../private/keys.txt",              # .. out of the root
    lambda root, secret: "/etc/passwd",
])
def test_a_path_outside_the_allowlist_is_refused(shared, make):
    root, secret = shared
    with pytest.raises(sd.ServerPathError, match="outside the folders"):
        sd.resolve(make(root, secret), sd.server_data_roots())


def test_a_link_out_of_the_allowlist_is_refused_and_not_copied(shared):
    root, secret = shared
    (root / "lab" / "escape").symlink_to(secret)
    with pytest.raises(sd.ServerPathError, match="outside the folders"):
        sd.resolve(f"{root}/lab/escape", sd.server_data_roots())
    (root / "lab" / "Sample3" / "outs" / "keys.txt").symlink_to(secret / "keys.txt")
    found = sd.scan(root / "lab" / "Sample3", root, sd.server_data_roots())
    assert "outs/keys.txt" not in {rel for rel, _ in found.copy}
    assert any(rel == "outs/keys.txt" and "outside the allowed" in why
               for rel, _, why in found.skipped)


def test_existence_outside_the_allowlist_is_not_revealed(shared):
    root, secret = shared
    for p in (str(secret / "keys.txt"), str(secret / "nothing-here")):
        with pytest.raises(sd.ServerPathError) as err:
            sd.resolve(p, sd.server_data_roots())
        assert "does not exist" not in str(err.value)
    with pytest.raises(sd.ServerPathError, match="does not exist"):
        sd.resolve(f"{root}/lab/nothing-here", sd.server_data_roots())


def test_a_mistyped_path_suggests_the_close_name(shared):
    root, secret = shared
    (root / "lab" / "Sample1_WT").mkdir()
    with pytest.raises(sd.ServerPathError, match="does not exist") as err:
        sd.resolve(f"{root}/lab/Samplel_WT", sd.server_data_roots())        # l for 1
    assert f"Did you mean {root}/lab/Sample1_WT" in str(err.value)
    with pytest.raises(sd.ServerPathError) as err:                         # a deeper miss too
        sd.resolve(f"{root}/lab/Samplel_WT/outs", sd.server_data_roots())
    assert f"{root}/lab/Sample1_WT" in str(err.value)
    with pytest.raises(sd.ServerPathError) as err:                         # nothing close
        sd.resolve(f"{root}/lab/zzzz", sd.server_data_roots())
    assert "Did you mean" not in str(err.value)
    (root / "lab" / "escape").symlink_to(secret)                           # never lists outside
    with pytest.raises(sd.ServerPathError) as err:
        sd.resolve(f"{root}/lab/escape/key.txt", sd.server_data_roots())
    assert "keys.txt" not in str(err.value)


def test_a_path_split_at_a_slash_by_wrapped_text_still_resolves(shared):
    root, _ = shared
    for raw in (f"{root}/lab/ Sample3/outs", f"{root}/lab/\nSample3 /outs"):
        path, _ = sd.resolve(raw, sd.server_data_roots())
        assert path == root / "lab" / "Sample3" / "outs"
    with pytest.raises(sd.ServerPathError, match=f"{root}/lab/Sample9 does not exist"):
        sd.resolve(f"{root}/lab/ Sample9", sd.server_data_roots())         # names the joined path
    (root / "lab" / " spaced").mkdir()                                     # a real name wins
    path, _ = sd.resolve(f"{root}/lab/ spaced", sd.server_data_roots())
    assert path.name == " spaced"


def test_binding_is_off_without_roots():
    with pytest.raises(sd.ServerPathError, match="not enabled"):
        sd.resolve("/data/x", ())


def test_paths_in_chat_text_are_found_only_inside_a_root(shared):
    root, secret = shared
    text = (f"QC the snRNA in {root}/lab/Sample3. Compare with {secret}/keys.txt and "
            f"see also ({root}/lab/Sample3/outs) and {root}/lab/Sample3.")
    assert sd.find_paths(text, sd.display_roots()) == [f"{root}/lab/Sample3",
                                                       f"{root}/lab/Sample3/outs"]


def test_a_cell_ranger_pipestance_copies_the_delivery_not_the_working_files(shared):
    root, _ = shared
    from aiscientist.tools.api import describe_cellranger_layout
    found = sd.scan(root / "lab" / "Sample3", root, sd.server_data_roots(), describe_cellranger_layout)
    copied = {rel for rel, _ in found.copy}
    assert copied == {"outs/filtered_feature_bc_matrix.h5", "outs/raw_feature_bc_matrix.h5",
                      "outs/web_summary.html", "outs/metrics_summary.csv",
                      "outs/possorted_genome_bam.bam", "outs/possorted_genome_bam.bam.bai",
                      "outs/analysis/clusters.csv", "notes.txt"}
    why = {rel: reason for rel, _, reason in found.skipped}
    assert "Loupe" in why["outs/cloupe.cloupe"] and "aggr" in why["outs/molecule_info.h5"]
    assert "outside outs/" in why["SC_RNA_COUNTER_CS/fork0/chunk.bam"] and "_log" in why
    summary = found.summary()
    assert summary["cellranger"] == {"n_libraries": 1, "samples": ["Sample3"], "all_have_bam": True}
    assert summary["copy_bytes"] == 30 + 90 + 5 + 2 + 1000 + 10 + 1 + len(b"the user's own note")


def test_the_mirror_path_is_stable_and_shared_by_parent_and_child(shared):
    root, _ = shared
    base = "/dfs3b/x/uploads/u"
    tag = "_".join(p for p in root.parts if p != "/")
    assert sd.mirror_dir(base, root, root / "lab") == f"{base}/server-data/{tag}/lab"
    # Binding the child later reuses the parent's copy: same place, nothing to copy again.
    assert (sd.mirror_dir(base, root, root / "lab" / "Sample3")
            == sd.mirror_dir(base, root, root / "lab") + "/Sample3")


class _FakeHPC:
    """Just enough of an executor: a dict for the remote filesystem."""

    def __init__(self):
        self.files: dict[str, int] = {}
        self.puts: list[str] = []
        self.commands: list[str] = []

    def exec(self, command, timeout=60.0):
        from aiscientist.gateway.executor import ExecResult
        self.commands.append(command)
        out = ""
        if command.startswith("find "):
            base = command.split()[1].strip("'")
            out = "\n".join(f"{size}\t{p[len(base) + 1:]}" for p, size in self.files.items()
                            if p.startswith(base + "/"))
        return ExecResult(command, 0, out, "")

    def put_file_resumable(self, local, remote, progress=None, make_parent=True):
        size = os.path.getsize(local)
        if progress:
            progress(size, size)
        self.files[remote] = size
        self.puts.append(remote)


def test_the_copy_is_incremental(shared):
    root, _ = shared
    found = sd.scan(root / "lab" / "Sample3", root, sd.server_data_roots())
    hpc = _FakeHPC()
    first = sd.sync_to_hpc(hpc, found, "/dfs/m")
    assert first["copied"] == len(found.copy) and first["kept"] == 0
    assert any(c.startswith("mkdir -p ") and "/dfs/m/outs/analysis" in c for c in hpc.commands)
    hpc.puts.clear()
    again = sd.sync_to_hpc(hpc, found, "/dfs/m")
    assert again["copied"] == 0 and hpc.puts == []
    hpc.files["/dfs/m/outs/possorted_genome_bam.bam"] = 12          # an interrupted copy
    third = sd.sync_to_hpc(hpc, found, "/dfs/m")
    assert hpc.puts == ["/dfs/m/outs/possorted_genome_bam.bam"] and third["copied"] == 1


def test_stop_interrupts_the_copy(shared):
    root, _ = shared
    found = sd.scan(root / "lab" / "Sample3", root, sd.server_data_roots())
    with pytest.raises(sd.TransferCancelled):
        sd.sync_to_hpc(_FakeHPC(), found, "/dfs/m", should_cancel=lambda: True)


# --- the gateway side -------------------------------------------------------------------------

pytest.importorskip("fastapi")
from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402


def _conn(tmp_path, **settings):
    conn = gw_app.Connection(HPCSettings(**settings), mock=False, loop=asyncio.new_event_loop(),
                             username="tester")
    conn.workspace = (tmp_path / "ws" / "tester").resolve()
    conn.workspace.mkdir(parents=True)
    conn.executor = _FakeHPC()
    conn.executor.username = "tester"
    return conn


def _admit(conn, paths):
    said: list[str] = []
    bound = [{"path": p, "name": p.rsplit("/", 1)[-1], "role": None} for p in paths]
    out = asyncio.run(gw_app._admit_bound_paths(
        conn, bound, lambda text, level="info": said.append(text),
        lambda level, stage, text: said.append(text)))
    return out, said


def test_a_run_admits_own_uploads_and_server_data_and_drops_the_rest(shared, tmp_path, monkeypatch):
    root, secret = shared
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    conn = _conn(tmp_path, analysis_on_hpc=True)
    own = conn.workspace / "uploads" / "a.h5ad"
    own.parent.mkdir(parents=True)
    own.write_bytes(b"")
    sample = str(root / "lab" / "Sample3")
    (admitted, mirrors), said = _admit(conn, [str(own), sample, str(secret / "keys.txt")])
    assert [a["path"] for a in admitted] == [str(own), sample]
    assert any("Not bound" in s and "keys.txt" in s for s in said)
    remote, copied = mirrors[sample]
    assert remote.startswith(f"{gw_app._hpc_uploads_dir(conn)}/server-data/")
    assert "outs/filtered_feature_bc_matrix.h5" in copied
    assert f"{remote}/outs/filtered_feature_bc_matrix.h5" in conn.executor.files
    assert gw_app._mirror_of(mirrors, sample, Path(sample) / "outs" / "filtered_feature_bc_matrix.h5") \
        == f"{remote}/outs/filtered_feature_bc_matrix.h5"
    assert gw_app._mirror_of(mirrors, sample, Path(sample) / "outs" / "molecule_info.h5") is None


def test_without_analysis_on_hpc_server_data_is_read_in_place(shared, tmp_path, monkeypatch):
    root, _ = shared
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    conn = _conn(tmp_path)
    sample = str(root / "lab" / "Sample3")
    (admitted, mirrors), _ = _admit(conn, [sample])
    assert [a["path"] for a in admitted] == [sample] and mirrors == {}
    assert conn.executor.puts == []


def test_check_and_bind_endpoints(shared, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    root, secret = shared
    monkeypatch.setenv(sd.CONFIRM_GB_ENV, "0.000001")              # ~1 KB: this delivery asks first
    conn = _conn(tmp_path, analysis_on_hpc=True)
    gw_app.CONNECTIONS[conn.id] = conn
    try:
        client = TestClient(gw_app.app)
        monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
        assert client.get("/api/server-data/roots").status_code == 401   # the roots are not public
        monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
        assert client.get("/api/server-data/roots").json() == {"enabled": True, "roots": [str(root)]}
        r = client.post("/api/server-data/check", json={"connection_id": conn.id,
                                                        "path": f"{root}/lab/Sample3"})
        info = r.json()
        assert r.status_code == 200 and info["confirm"] and info["copies_to_hpc"]
        assert info["primary"] == "outs/filtered_feature_bc_matrix.h5"
        assert "Cell Ranger delivery: 1 library (Sample3); QC route: CellQC" in info["message"]
        assert conn.executor.puts == []                              # checking copies nothing
        bad = client.post("/api/server-data/bind", json={"connection_id": conn.id,
                                                         "path": str(secret / "keys.txt")})
        assert bad.status_code == 400 and "outside" in bad.json()["error"]
        ok = client.post("/api/server-data/bind", json={"connection_id": conn.id,
                                                        "path": f"{root}/lab/Sample3"}).json()
        assert ok["status"] == "bound" and ok["kind"] == "server-folder"
        assert ok["path"] == str(root / "lab" / "Sample3")
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
