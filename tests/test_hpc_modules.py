"""HPC3's module catalog: read once on the login node, searched before anything is installed.

The catalog lines below are copied from a real fetch of RCIC's module files (2026-10-25). What is
pinned: a program finds its module even when the names differ (``bedtools`` is in ``bedtools2``),
the newest version is the one offered, a failed fetch is not cached, and a failed command's output
is read for missing software without mistaking a missing data file for a missing program.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from aiscientist.hpc import modules as m
from aiscientist.hpc.modules import ModuleCatalog, cached_catalog, missing_from_output, software_note

CATALOG = "\n".join([
    "M\tBIOTOOLS\tbedtools2/2.29.2\tBedtools2 utilities are a swiss-army knife of tools for a wide-range",
    "M\tBIOTOOLS\tbedtools2/2.30.0\tBedtools2 utilities are a swiss-army knife of tools for a wide-range",
    "C\tbedtools2/2.29.2\tbedtools",
    "C\tbedtools2/2.30.0\tbedtools",
    "C\tbedtools2/2.30.0\tintersectBed",
    "M\tLANGUAGES\tR/4.1.2\tR is a freely available language and environment for statistical computing and graphics.",
    "M\tLANGUAGES\tR/4.5.2\tR is a freely available language and environment for statistical computing and graphics.",
    "C\tR/4.5.2\tR",
    "C\tR/4.5.2\tRscript",
    "M\tBIOTOOLS\tsamtools/1.10\tSamtools is a suite of programs for interacting with high-throughput sequencing data.",
    "M\tBIOTOOLS\tsamtools/1.15.1\tSamtools is a suite of programs for interacting with high-throughput sequencing data.",
    "C\tsamtools/1.15.1\tsamtools",
    "M\tBIOTOOLS\tncbi-blast/2.13.0\tBLAST (Basic Local Alignment Search Tool) command line applications from NCBI.",
    "C\tncbi-blast/2.13.0\tblastn",
    "M\tBIOTOOLS\tsubread/2.0.3\tThe Subread is a toolkit for processing next-gen sequencing data.",
    "C\tsubread/2.0.3\tfeatureCounts",
    "M\tBIOTOOLS\tmacs/2.2.7.1\tMACS - Model-based Analysis of ChIP-Seq for identifying transcript",
    "M\tCOMPILERS\tgcc/system\tSystem default GCC system.",
    "M\tCOMPILERS\tgcc/11.2.0\tGNU GCC compiler.",
    "M\tCOMPILERS\tgcc/15.2.0\tGNU GCC compiler.",
    "M\tTOOLS\tMATLAB/R2020a\tMATLAB",
    "M\tTOOLS\tMATLAB/R2025b\tMATLAB",
    "M\tCHEMISTRY\tqe/7.1/gcc.11.2.0-openmpi.4.1.2\tQuantum Espresso",
    "not a catalog line",
    "M\tBROKEN\tnoversion\t",
    "C\tR/4.5.2\t",
])


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch):
    monkeypatch.setattr(m, "_CACHE", {})


def _cat() -> ModuleCatalog:
    return ModuleCatalog.parse(CATALOG)


# --- parsing ------------------------------------------------------------------------------------


def test_parse_groups_versions_and_skips_malformed_lines():
    cat = _cat()
    assert len(cat) == 9
    assert "noversion" not in cat.modules
    r = cat.modules["r"].as_dict()
    assert r["name"] == "R" and r["category"] == "LANGUAGES"
    assert r["versions"] == ["R/4.5.2", "R/4.1.2"] and r["load"] == "R/4.5.2"
    assert cat.modules["r"].latest().commands == ("R", "Rscript")


def test_the_newest_numbered_version_is_the_one_offered():
    cat = _cat()
    assert cat.modules["gcc"].as_dict()["load"] == "gcc/15.2.0"           # not gcc/system
    assert cat.modules["gcc"].as_dict()["versions"][-1] == "gcc/system"
    assert cat.modules["matlab"].as_dict()["load"] == "MATLAB/R2025b"
    assert cat.modules["qe"].as_dict()["load"] == "qe/7.1/gcc.11.2.0-openmpi.4.1.2"


@pytest.mark.parametrize("newer,older", [
    ("4.10", "4.9"), ("2.7.10a", "2.7.8a"), ("1.0", "system"), ("R2025b", "R2020a"), ("v429", "v8"),
])
def test_version_order_is_natural(newer, older):
    assert m._version_key(newer) > m._version_key(older)


# --- search -------------------------------------------------------------------------------------


@pytest.mark.parametrize("query,module,via", [
    ("samtools", "samtools", "name"),
    ("bedtools", "bedtools2", "program `bedtools`"),      # the module is named bedtools2
    ("Rscript", "R", "program `Rscript`"),
    ("blastn", "ncbi-blast", "program `blastn`"),
    ("featureCounts", "subread", "program `featureCounts`"),
    ("macs2", "macs", "name"),                             # the query extends the module's name
    ("r:R", "R", "name"),                                  # a requirement's "r:" prefix is dropped
    ("SAMTOOLS", "samtools", "name"),
])
def test_find_resolves_a_program_to_its_module(query, module, via):
    hits = _cat().find(query)
    assert hits and hits[0]["name"] == module and hits[0]["matched_by"] == via


def test_find_by_description_and_nothing_for_unknown_software():
    names = [h["name"] for h in _cat().find("sequencing")]
    assert set(names) == {"samtools", "subread"}
    assert all(h["matched_by"] == "description" for h in _cat().find("sequencing"))
    assert _cat().find("mosdepth") == []
    assert _cat().find("  ") == []


def test_find_ranks_exact_names_above_partial_ones():
    cat = ModuleCatalog.parse("M\tB\tstar/2.7.10a\tSTAR aligner\nM\tB\tstarfusion/1.10\tSTAR-Fusion")
    assert [h["name"] for h in cat.find("star")] == ["star", "starfusion"]
    assert len(cat.find("star", limit=1)) == 1


# --- the cache ----------------------------------------------------------------------------------


def test_the_catalog_is_fetched_once_per_ttl():
    calls, clock = [], [1000.0]

    def fetch():
        calls.append(1)
        return CATALOG

    def now():
        return clock[0]

    a = cached_catalog("hpc3", fetch, now=now, ttl_s=60)
    b = cached_catalog("hpc3", fetch, now=now, ttl_s=60)
    assert a is b and len(calls) == 1
    clock[0] += 61
    cached_catalog("hpc3", fetch, now=now, ttl_s=60)
    assert len(calls) == 2


def test_a_failed_fetch_is_not_cached_and_keeps_the_last_good_catalog():
    clock = [0.0]
    assert cached_catalog("hpc3", lambda: "", now=lambda: clock[0], ttl_s=60) is None

    def boom():
        raise OSError("ssh down")

    assert cached_catalog("hpc3", boom, now=lambda: clock[0], ttl_s=60) is None
    good = cached_catalog("hpc3", lambda: CATALOG, now=lambda: clock[0], ttl_s=60)
    clock[0] = 120.0
    assert cached_catalog("hpc3", boom, now=lambda: clock[0], ttl_s=60) is good     # stale beats none
    fresh = cached_catalog("hpc3", lambda: CATALOG, now=lambda: clock[0], ttl_s=60)
    assert fresh is not good


def test_ttl_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_MODULE_CATALOG_TTL_S", "3600")
    assert m.catalog_ttl_s() == 3600
    monkeypatch.setenv("AISCIENTIST_MODULE_CATALOG_TTL_S", "5")
    assert m.catalog_ttl_s() == 60
    monkeypatch.setenv("AISCIENTIST_MODULE_CATALOG_TTL_S", "a day")
    assert m.catalog_ttl_s() == 86400


# --- reading a failure --------------------------------------------------------------------------

_SUBPROCESS_TRACE = """Traceback (most recent call last):
  File "<snippet>", line 3, in <module>
    subprocess.run(["bedtools", "intersect", "-a", "a.bed"], check=True)
  File "/usr/lib/python3.11/subprocess.py", line 1901, in _execute_child
    raise child_exception_type(errno_num, err_msg, err_filename)
FileNotFoundError: [Errno 2] No such file or directory: 'bedtools'"""

_OPEN_TRACE = """Traceback (most recent call last):
  File "<snippet>", line 2, in <module>
    df = pd.read_csv('counts.csv')
FileNotFoundError: [Errno 2] No such file or directory: 'counts.csv'"""


@pytest.mark.parametrize("text,expected", [
    ("bash: line 1: bedtools: command not found", [("command", "bedtools")]),
    ("/bin/sh: 1: STAR: command not found\nbash: line 2: STAR: command not found", [("command", "STAR")]),
    (_SUBPROCESS_TRACE, [("command", "bedtools")]),
    (_OPEN_TRACE, []),                                    # a missing data file is not a missing program
    ("Error in library(DESeq2) : there is no package called ‘DESeq2’", [("r_package", "DESeq2")]),
    ("Error: there is no package called 'Seurat'", [("r_package", "Seurat")]),
    ("KeyError: 'leiden'", []),
    ("", []),
])
def test_missing_software_is_read_from_the_output(text, expected):
    assert missing_from_output(text) == expected


def test_the_note_says_where_the_module_runs():
    cat = _cat()
    shell_note = software_note("bash: line 1: bedtools: command not found", cat, where="run_shell")
    assert shell_note[0]["modules"] == ["bedtools2/2.30.0"]
    assert "module load bedtools2/2.30.0 && bedtools" in shell_note[0]["hint"]
    code_note = software_note(_SUBPROCESS_TRACE, cat, where="run_code")
    assert "run_shell" in code_note[0]["hint"] and "AISCIENTIST_WORK" in code_note[0]["hint"]


def test_the_note_for_software_no_module_provides():
    note = software_note("bash: mosdepth: command not found", _cat(), where="run_shell")[0]
    assert note["modules"] == [] and "No HPC3 module provides 'mosdepth'" in note["hint"]
    assert "do not hand-write a replacement" in note["hint"]
    unread = software_note("bash: mosdepth: command not found", None, where="run_shell")[0]
    assert "find_software('mosdepth')" in unread["hint"]
    r_pkg = software_note("there is no package called ‘DESeq2’", _cat(), where="run_shell")[0]
    assert r_pkg["kind"] == "r_package" and "DESeq2" in r_pkg["hint"]


def test_at_most_three_notes():
    text = "\n".join(f"bash: tool{i}: command not found" for i in range(6))
    assert len(software_note(text, _cat(), where="run_shell")) == 3


# --- the login-node command, run against a module tree ------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_catalog_command_reads_a_module_tree(tmp_path):
    root = tmp_path / "modulefiles"
    bindir = tmp_path / "apps" / "bedtools2" / "2.30.0" / "bin"
    bindir.mkdir(parents=True)
    for prog in ("bedtools", "intersectBed"):
        (bindir / prog).write_text("#!/bin/sh\n")
    mod = root / "BIOTOOLS" / "bedtools2"
    mod.mkdir(parents=True)
    (mod / "2.30.0").write_text(
        "#%Module1.0\n"
        'module-whatis "Category_______ BIOTOOLS"\n'
        'module-whatis "Name___________ bedtools2"\n'
        'module-whatis "Version________ 2.30.0"\n'
        'module-whatis "Description____ Bedtools2 utilities are a swiss-army knife"\n'
        f"prepend-path          PATH              {bindir}\n"
        "exec /bin/logger -p local6.notice -t module-hpc $env(USER) \"bedtools2/2.30.0\"\n")
    (mod / ".version").write_text('#%Module\nset ModulesVersion "2.30.0"\n')
    gone = root / "LANGUAGES" / "R"
    gone.mkdir(parents=True)
    (gone / "4.5.2").write_text(
        'module-whatis "Description____ R is a language"\n'
        f"prepend-path PATH {tmp_path}/does/not/exist/bin\n")

    out = subprocess.run(["bash", "-c", m.catalog_command(str(root))],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    cat = ModuleCatalog.parse(out)
    assert set(cat.modules) == {"bedtools2", "r"}                     # .version is not a module
    bt = cat.modules["bedtools2"]
    assert bt.category == "BIOTOOLS" and bt.description == "Bedtools2 utilities are a swiss-army knife"
    assert bt.latest().commands == ("bedtools", "intersectBed")
    assert cat.modules["r"].latest().commands == ()                   # its PATH dir does not exist
    assert cat.find("intersectBed")[0]["load"] == "bedtools2/2.30.0"
