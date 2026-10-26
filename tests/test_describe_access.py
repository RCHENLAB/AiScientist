"""Who may read what through ``POST /api/dataset/describe``.

The endpoint returns a file's head (text lines or a hexdump), so a path taken from the request body
must never reach a file the caller does not own: another user's uploads, the prod ``.env``, or the
stored API and SSH keys under the state dir. These pin the guard: only the caller's own connection,
and only a path inside their own uploads, an allowlisted server folder, or their HPC3 storage.
"""

from __future__ import annotations

import asyncio
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway import server_data as sd  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

_VCF = (b"##fileformat=VCFv4.2\n"
        b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE1\n"
        b"1\t100\t.\tA\tT\t50\tPASS\t.\tGT\t0/1\n")
_SECRET = "VICTIM-SECRET-7f3a"


class _FakeHPC:
    """An executor whose dfs3b holds every path: records what was read."""

    username = "tester"

    def __init__(self):
        self.reads: list[str] = []

    def read_bytes(self, path, max_bytes):
        self.reads.append(path)
        return _VCF[:max_bytes]

    def remote_size(self, path):
        return len(_VCF)


@pytest.fixture
def site(tmp_path, monkeypatch):
    """The gateway host: two users' workspaces, the state dir with stored keys, and an .env.
    Accounts on; the caller is account 7, "tester", on its own connection."""
    results = tmp_path / "console"
    own = results / "tester" / "uploads"
    own.mkdir(parents=True)
    (own / "callset.vcf").write_bytes(_VCF)
    victim = results / "victim" / "uploads"
    victim.mkdir(parents=True)
    (victim / "secret.vcf").write_text(_SECRET)
    key = tmp_path / "state" / "llm_creds" / "victim" / "abc.key"
    key.parent.mkdir(parents=True)
    key.write_text(_SECRET)
    (tmp_path / ".env").write_text(f"AISCIENTIST_SECRET_KEY={_SECRET}\n")
    monkeypatch.delenv(sd.ROOTS_ENV, raising=False)

    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user",
                        lambda request: types.SimpleNamespace(id=7, username="tester"))
    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = results / "tester"
    conn.app_user_id = 7
    gw_app.CONNECTIONS[conn.id] = conn
    try:
        yield types.SimpleNamespace(conn=conn, own=own, victim=victim, key=key, root=tmp_path,
                                    client=TestClient(gw_app.app))
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


def _describe(s, path):
    return s.client.post("/api/dataset/describe", json={"connection_id": s.conn.id, "path": str(path)})


def _refused(r):
    assert r.status_code == 403, r.text
    assert _SECRET not in r.text                       # nothing of the file came back
    return r


def test_own_upload_is_described(site):
    r = _describe(site, site.own / "callset.vcf")
    assert r.status_code == 200, r.text
    assert r.json()["peek"]["detected_format"] == "vcf"


def test_another_users_upload_is_refused(site):
    _refused(_describe(site, site.victim / "secret.vcf"))


def test_traversal_out_of_own_uploads_is_refused(site):
    _refused(_describe(site, f"{site.own}/../../victim/uploads/secret.vcf"))


def test_state_dir_keys_and_env_are_refused(site):
    _refused(_describe(site, site.key))
    _refused(_describe(site, site.root / ".env"))


def test_host_files_are_refused(site):
    assert "Refusing to read /etc/passwd" in _refused(_describe(site, "/etc/passwd")).json()["error"]
    _refused(_describe(site, "/etc/passwd\x00.vcf"))               # a 403, not a 500


def test_a_symlink_out_of_own_uploads_is_refused(site):
    (site.own / "link.vcf").symlink_to(site.victim / "secret.vcf")
    (site.own / "escape").symlink_to(site.victim.parent, target_is_directory=True)
    (site.own / "keys").symlink_to(site.key)
    _refused(_describe(site, site.own / "link.vcf"))
    _refused(_describe(site, site.own / "escape" / "uploads" / "secret.vcf"))
    _refused(_describe(site, site.own / "keys"))


def test_an_allowlisted_server_folder_is_described(site, monkeypatch):
    shared = site.root / "shared"
    shared.mkdir()
    (shared / "panel.vcf").write_bytes(_VCF)
    monkeypatch.setenv(sd.ROOTS_ENV, str(shared))
    assert _describe(site, shared / "panel.vcf").status_code == 200
    _refused(_describe(site, site.victim / "secret.vcf"))   # the allowlist widens nothing else


def test_hpc3_storage_is_read_over_the_users_own_login(site):
    site.conn.executor = hpc = _FakeHPC()
    lab = site.conn.settings.lab_storage.rstrip("/")
    assert lab
    r = _describe(site, f"{lab}/tester/uploads/callset.vcf")
    assert r.status_code == 200, r.text
    assert r.json()["peek"]["remote"] is True
    assert hpc.reads == [f"{lab}/tester/uploads/callset.vcf"]
    _refused(_describe(site, f"{lab}/../../etc/passwd"))      # the prefix test cannot be walked out of
    assert len(hpc.reads) == 1


def test_a_remote_path_without_an_hpc3_login_is_refused(site):
    lab = site.conn.settings.lab_storage.rstrip("/")
    _refused(_describe(site, f"{lab}/tester/uploads/callset.vcf"))


def test_the_connection_must_be_the_callers(site, monkeypatch):
    path = site.own / "callset.vcf"
    monkeypatch.setattr(gw_app, "_optional_user",
                        lambda request: types.SimpleNamespace(id=8, username="other"))
    assert _describe(site, path).status_code == 403
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: None)
    assert _describe(site, path).status_code == 401


def test_a_connection_opened_without_a_login_belongs_to_nobody(site):
    # Logged out, /api/connect names the workspace after the request's `ucinetid`, so such a
    # connection can sit on another user's folder: no logged-in account may read through it.
    site.conn.app_user_id = None
    assert _describe(site, site.own / "callset.vcf").status_code == 403


def test_the_path_guard_holds_with_accounts_off(site, monkeypatch):
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    assert _describe(site, site.own / "callset.vcf").status_code == 200
    _refused(_describe(site, "/etc/passwd"))
    _refused(_describe(site, site.victim / "secret.vcf"))
