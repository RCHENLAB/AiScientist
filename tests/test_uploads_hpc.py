"""Phase 2 of the HPC3 offload: uploaded datasets land on HPC3 dfs3b, not the eyeserver.

Unit-level coverage of the staging helpers (put to dfs3b + drop the local copy; stage back
for the still-local tools; remote-aware identification) using MockExecutor, which records
put_file calls and no-ops get_file. See docs/hpc3_offload_migration.md.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.gateway.mock_host import MockExecutor  # noqa: E402
from bioagent.gateway.settings import HPCSettings  # noqa: E402


def _conn():
    return gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")


def test_uploads_on_hpc_gate():
    conn = _conn()
    conn.settings.uploads_on_hpc = True
    assert gw_app._uploads_on_hpc(conn) is False          # no SSH executor yet
    conn.executor = MockExecutor(username="tester")
    assert gw_app._uploads_on_hpc(conn) is True
    conn.settings.uploads_on_hpc = False
    assert gw_app._uploads_on_hpc(conn) is False           # flag off


def test_is_remote_dataset():
    conn = _conn()
    assert gw_app._is_remote_dataset(conn, "/dfs3b/ruic20_lab/tester/uploads/x.h5ad") is True
    assert gw_app._is_remote_dataset(conn, "/data/BioAgent/tester/uploads/x.h5ad") is False
    assert gw_app._is_remote_dataset(conn, "") is False


def test_stage_upload_to_hpc_moves_and_records(tmp_path):
    conn = _conn()
    conn.executor = MockExecutor(username="tester")
    local = tmp_path / "pbmc.h5ad"
    local.write_bytes(b"x" * 100)

    remote = gw_app._stage_upload_to_hpc(conn, local, "pbmc.h5ad")

    # Uploads live in the SHARED project root now, not the member's personal lab dir — and
    # deliberately outside Temp/, so the 3-day sweeper can never reach raw research data.
    assert remote == "/dfs3b/ruic20_lab/software/AiScientist/uploads/tester/pbmc.h5ad"
    assert (str(local), remote) in conn.executor.state.staged_files   # put_file was called
    assert not local.exists()                                         # local copy removed
    assert gw_app._is_remote_dataset(conn, remote) is True


def test_ensure_local_dataset_stages_remote_back(tmp_path):
    conn = _conn()
    conn.executor = MockExecutor(username="tester")
    cache = tmp_path / "staged"
    remote = "/dfs3b/ruic20_lab/tester/uploads/pbmc.h5ad"

    got = gw_app._ensure_local_dataset(conn, remote, cache)
    assert got == cache / "pbmc.h5ad"                    # staged into the run's cache dir

    local = tmp_path / "local.h5ad"
    local.write_bytes(b"y")
    assert gw_app._ensure_local_dataset(conn, str(local), cache) == local   # local passes through


def test_sync_bioagent_source_to_hpc_tars_and_caches():
    conn = _conn()
    conn.executor = MockExecutor(username="tester")

    pysrc = gw_app._sync_bioagent_source_to_hpc(conn)
    assert pysrc == "/dfs3b/ruic20_lab/software/AiScientist/pysrc/tester"
    assert conn.hpc_pysrc == pysrc
    staged = conn.executor.state.staged_files
    assert any(remote == f"{pysrc}/bioagent-src.tgz" for _l, remote in staged)   # tarball pushed

    n = len(staged)
    assert gw_app._sync_bioagent_source_to_hpc(conn) == pysrc     # cached — no re-stage
    assert len(conn.executor.state.staged_files) == n


def _fake_listing_conn(listing: str):
    from bioagent.gateway.executor import ExecResult

    class FakeExec:
        username = "tester"

        def exec(self, cmd, timeout=60.0):
            return ExecResult(command=cmd, exit_status=0, stdout=listing, stderr="")

    conn = _conn()
    conn.executor = FakeExec()
    return conn


def test_find_primary_matrix_remote_ranks_like_local():
    conn = _fake_listing_conn("1000\t/dfs/f/sample_sheet.csv\n"
                              "50\t/dfs/f/nested/a.h5ad\n"
                              "999\t/dfs/f/matrix.h5ad\n"
                              "10\t/dfs/f/readme.txt\n")
    # .h5ad beats .csv/.txt; shallowest wins → top-level matrix.h5ad over nested/a.h5ad
    assert gw_app._find_primary_matrix_remote(conn, "/dfs/f") == "/dfs/f/matrix.h5ad"


def test_find_primary_matrix_remote_none_when_no_dataset_file():
    conn = _fake_listing_conn("20\t/dfs/f/notes.pdf\n")
    assert gw_app._find_primary_matrix_remote(conn, "/dfs/f") is None


# --- a VCF is a primary dataset too ----------------------------------------------------------
# Uploads land on dfs3b in prod (BIOAGENT_UPLOADS_ON_HPC=1), so a folder upload resolves through the
# REMOTE finder. It only knew single-cell matrices, so a folder holding a WGS callset resolved to
# nothing and the run silently had no dataset.


@pytest.mark.parametrize("name,expected", [
    ("case.vcf.gz", ".vcf.gz"),      # the normal form for a WGS callset — was read as ".gz" and missed
    ("case.vcf", ".vcf"),
    ("case.bcf", ".bcf"),
    ("matrix.h5ad", ".h5ad"),        # must not be shadowed by the shorter ".h5"
    ("counts.h5", ".h5"),
    ("notes.pdf", None),
    ("archive.gz", None),            # a bare .gz is not a dataset
    ("noextension", None),
])
def test_primary_suffix_matches_longest_extension(name, expected):
    assert gw_app._primary_suffix(name) == expected


def test_a_vcf_folder_resolves_to_the_vcf_not_the_readme():
    """The IRD case shape: a callset next to its notes. The generic text formats rank LAST precisely
    so the note can never win — picking notes.txt would hand the variant tools a text file."""
    conn = _fake_listing_conn("2000000000\t/dfs/f/CASE_A.GATK.HaplotypeCaller.mark.vcf.gz\n"
                              "1200\t/dfs/f/clinical_notes.txt\n"
                              "300\t/dfs/f/README.md\n")
    assert gw_app._find_primary_matrix_remote(conn, "/dfs/f") == \
        "/dfs/f/CASE_A.GATK.HaplotypeCaller.mark.vcf.gz"


def test_local_and_remote_finders_agree_on_the_same_tree(tmp_path):
    """The two finders are separate implementations over the same ranking; they drifted once already
    (Path.suffix vs a string split). Pin them to the same answer on one tree."""
    for rel, size in [("sample_sheet.csv", 1000), ("nested/old.vcf", 50),
                      ("case.vcf.gz", 900), ("readme.txt", 10)]:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * size)
    local = gw_app._find_primary_matrix(tmp_path)
    conn = _fake_listing_conn("".join(
        f"{size}\t/dfs/f/{rel}\n" for rel, size in
        [("sample_sheet.csv", 1000), ("nested/old.vcf", 50), ("case.vcf.gz", 900), ("readme.txt", 10)]))
    remote = gw_app._find_primary_matrix_remote(conn, "/dfs/f")
    assert local.name == "case.vcf.gz"
    assert remote == "/dfs/f/case.vcf.gz"


def test_uploads_on_hpc_flag_from_env(monkeypatch):
    monkeypatch.setenv("BIOAGENT_UPLOADS_ON_HPC", "1")
    assert HPCSettings.from_env().uploads_on_hpc is True
    monkeypatch.delenv("BIOAGENT_UPLOADS_ON_HPC", raising=False)
    assert HPCSettings.from_env().uploads_on_hpc is False
