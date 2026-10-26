"""The two credential stores keep their folders closed to everyone but the service account.

On production the owner folders were 0775 with ``index.json`` 0664: they had taken the state dir's
default ACL, so the keys inside were closed only by the state dir's own permissions — one chmod
away from readable. The stores now keep every folder they make or use at 0700, and write every file
at 0600 from its first byte (an SSH private key used to be written with the default mode and
chmod-ed after). The SSH store's own case is in ``test_ssh_credentials.py``.
"""

from __future__ import annotations

import importlib
import os
import stat

import pytest


@pytest.fixture()
def lc(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AISCIENTIST_LLM_KEY_ENCRYPTION", raising=False)
    from aiscientist.gateway import llm_credentials
    importlib.reload(llm_credentials)
    return llm_credentials


def test_the_api_key_store_folders_are_closed_and_its_files_private(lc, tmp_path):
    (tmp_path / "llm_creds" / "alice").mkdir(parents=True)
    os.chmod(tmp_path / "llm_creds" / "alice", 0o775)          # as production had them
    lc.create("alice", provider="deepseek", base_url="https://api.deepseek.com/v1",
              model="deepseek-chat", api_key="placeholder-test-key")
    for d in (tmp_path / "llm_creds", tmp_path / "llm_creds" / "alice"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    files = list((tmp_path / "llm_creds" / "alice").iterdir())
    assert {f.suffix for f in files} == {".key", ".json"}
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
