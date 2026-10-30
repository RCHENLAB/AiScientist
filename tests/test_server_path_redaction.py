"""No server path reaches a user (RCHENLAB/AiScientist issue 37).

Error messages and tracebacks carry the paths of the files they failed on — the key stores, the
state dir, the code checkout. ``errors.redact_server_paths`` takes this host's paths out of
anything a user sees, keeping a traceback frame's module path and any other path's file name.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiscientist.gateway import errors
from aiscientist.gateway.errors import GatewayError, error_detail, public_error, redact_server_paths

STATE = "/srv/aisci-test/state"


@pytest.fixture(autouse=True)
def _state_dir(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", STATE)


def test_a_server_path_keeps_only_its_file_name():
    text = f"No such file or directory: '{STATE}/llm_creds/alice/abc.key'"
    assert redact_server_paths(text) == "No such file or directory: '…/abc.key'"


def test_paths_under_the_checkout_home_and_cwd_are_hidden():
    for root in (Path(errors.__file__).resolve().parents[3], Path.home(), Path.cwd()):
        out = redact_server_paths(f"Could not create {root}/runs/console/alice/r1/artifacts: denied")
        assert str(root) not in out
        assert out == "Could not create …/artifacts: denied"


def test_a_root_matches_only_whole_directory_names():
    for other in (f"{STATE}2/x.key", f"{STATE}.bak/x.key"):
        assert redact_server_paths(other) == other
    assert redact_server_paths(f"in {STATE}.") == "in …."


def test_hpc3_paths_are_the_users_own_and_stay():
    for text in ("/dfs3b/ruic20_lab/alice/data.h5ad", "/data/homezvol0/alice/run.sh: not found",
                 "/tmp/aiscientist_job/run.sh"):
        assert redact_server_paths(text) == text


def test_public_error_never_shows_the_path_and_is_never_empty():
    with pytest.raises(OSError) as info:
        open(f"{STATE}/ssh_creds/alice/k1.key", encoding="utf-8")
    assert STATE not in public_error(info.value)
    assert public_error(info.value).endswith("'…/k1.key'")
    assert public_error(RuntimeError()) == "RuntimeError"


def test_error_detail_tracebacks_keep_module_paths_only():
    try:
        open(f"{STATE}/llm_key.master", encoding="utf-8")
    except OSError as exc:
        try:
            raise GatewayError("could not read the key", stage="llm") from exc
        except GatewayError as outer:
            detail = error_detail(outer)
    dumped = repr(detail)
    assert STATE not in dumped
    assert str(Path(errors.__file__).resolve().parents[2]) not in dumped, "no checkout path"
    assert 'File "' in detail["traceback"]
    assert os.path.basename(__file__) in detail["traceback"], "the frame's file is still named"
    assert detail["cause"]["message"].endswith("'…/llm_key.master'")


def test_a_saved_ssh_key_s_path_is_not_in_its_errors(tmp_path):
    from aiscientist.gateway.ssh_gateway import SSHExecutor

    missing = tmp_path / "ssh_creds" / "alice" / "k1.key"
    with pytest.raises(GatewayError) as info:
        SSHExecutor._load_key(str(missing), None)
    assert str(tmp_path) not in info.value.message

    broken = tmp_path / "k2.key"
    broken.write_text("not a key", encoding="utf-8")
    with pytest.raises(GatewayError) as info:
        SSHExecutor._load_key(str(broken), None)
    assert str(tmp_path) not in info.value.message
    assert str(tmp_path) not in repr(info.value.detail)


# --- the chat: a failed run's message (run 82fab7a4a437) -----------------------------------------
#
# A GatewayError's message went to the chat as it was, so an upload that failed showed the user's
# dataset folder on the gateway host: "Failed to upload /data/aiscientist/data/users/lijinbio/
# datasets/<uuid>/original/analysis/…/clusters.csv -> /dfs3b/…".

HPC3_DEST = "/dfs3b/ruic20_lab/lijinbio/aiscientist/82fab7a4a437/clusters.csv"


def _upload_failure(data_root: str) -> str:
    return (f"Failed to upload {data_root}/users/lijinbio/datasets/3f2c9a1e-5b7d-4c8e-9f0a-1b2c3d4e5f60"
            f"/original/analysis/leiden/clusters.csv -> {HPC3_DEST}.")


def _connection():
    import asyncio

    pytest.importorskip("fastapi")
    from aiscientist.gateway.app import Connection
    from aiscientist.gateway.settings import HPCSettings

    conn = Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    published: list[dict] = []
    conn._publish = lambda payload: published.append(dict(payload))
    return conn, published


def test_a_chat_error_loses_the_server_path_live_and_on_replay(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", "/data/aiscientist/data")
    conn, published = _connection()
    conn.push({"type": "chat_start"})
    conn.push({"type": "chat_error", "message": _upload_failure("/data/aiscientist/data")})

    shown = next(p for p in published if p["type"] == "chat_error")["message"]
    assert shown == f"Failed to upload …/clusters.csv -> {HPC3_DEST}."
    replayed = conn.stream_replay_payloads()[-1]
    assert replayed["type"] == "chat_error" and replayed["message"] == shown


def test_a_lab_run_that_fails_on_an_upload_shows_no_server_path(tmp_path, monkeypatch):
    import asyncio

    pytest.importorskip("fastapi")
    from aiscientist.agents import research_lab as rl
    from aiscientist.gateway import app as gw_app

    data_root = tmp_path / "data"
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(data_root))
    message = _upload_failure(str(data_root))

    class _FailingLab:
        def __init__(self, *a, **k):
            self.guidance = None
            self.protocols = []
            self.self_sourced = ""
            self.execution_profile = {}

        def run(self, *a, **k):
            raise GatewayError(message, stage="ssh_put")

    monkeypatch.setattr(rl, "ResearchLab", _FailingLab)
    conn, published = _connection()
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    gw_app.CONNECTIONS[conn.id] = conn
    try:
        req = gw_app.LabRequest(connection_id=conn.id, question="cluster this", conversation_id="c1")
        conn.begin_run("c1")
        asyncio.run(gw_app._run_lab(conn, req))
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)

    error = next(p for p in published if p["type"] == "chat_error")["message"]
    assert str(data_root) not in error and "lijinbio/datasets" not in error
    assert error == f"Failed to upload …/clusters.csv -> {HPC3_DEST}."
    logged = [e for e in conn.log if e["level"] == "error" and e["stage"] == "ssh_put"]
    assert logged and str(data_root) not in logged[-1]["message"]


def test_transfer_failures_name_the_file_not_its_server_folder(tmp_path, monkeypatch):
    from aiscientist.gateway.ssh_gateway import SSHExecutor

    folder = tmp_path / "users" / "lijinbio" / "datasets" / "u1" / "original"
    folder.mkdir(parents=True)
    local = folder / "clusters.csv"
    local.write_text("cell,cluster\n", encoding="utf-8")

    class _BrokenSFTP:
        def put(self, *a, **k):
            raise OSError("Permission denied")

        get = put

        def close(self):
            pass

    ex = SSHExecutor.__new__(SSHExecutor)
    monkeypatch.setattr(ex, "exec", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ex, "_open_sftp", lambda: _BrokenSFTP(), raising=False)

    with pytest.raises(GatewayError) as up:
        ex.put_file(str(local), HPC3_DEST)
    assert up.value.message == f"Failed to upload clusters.csv to {HPC3_DEST}."

    with pytest.raises(GatewayError) as down:
        ex.get_file(HPC3_DEST, str(local))
    assert down.value.message == f"Failed to download {HPC3_DEST}."
    assert str(tmp_path) not in up.value.message + down.value.message
