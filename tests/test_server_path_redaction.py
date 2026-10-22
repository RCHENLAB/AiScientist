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
