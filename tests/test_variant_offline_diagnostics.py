"""Offline-VEP wiring diagnostics — the preflight that turns a silent "fell back to REST" into a
concrete "clinvar_vcf NOT found on HPC3: <path>". Pure helpers over a fake remote executor; no SSH.
"""
from __future__ import annotations

import types

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw  # noqa: E402


class _FakeExec:
    """Emulates `test -e/-d <path> && echo __OK__ || echo __MISSING__` over a known-present set."""

    def __init__(self, present):
        self.present = set(present)

    def exec(self, cmd):
        import shlex
        path = shlex.split(cmd)[2]
        return types.SimpleNamespace(
            stdout="__OK__" if path in self.present else "__MISSING__", stderr="")


class _BrokenExec:
    def exec(self, cmd):
        raise RuntimeError("ssh channel dropped")


def test_preflight_all_present():
    ex = _FakeExec({"/dfs/vep.sif", "/dfs/cache", "/dfs/clinvar.vcf.gz"})
    checks = gw._variant_offline_preflight(ex, "/dfs/vep.sif", "/dfs/cache", "/dfs/clinvar.vcf.gz")
    assert {k: v[1] for k, v in checks.items()} == {
        "vep_image": True, "cache_dir": True, "clinvar_vcf": True}


def test_preflight_flags_missing_clinvar():
    ex = _FakeExec({"/dfs/vep.sif", "/dfs/cache"})
    checks = gw._variant_offline_preflight(ex, "/dfs/vep.sif", "/dfs/cache", "/dfs/clinvar.vcf.gz")
    assert checks["clinvar_vcf"][1] is False                 # concrete "missing", not a mystery
    assert checks["vep_image"][1] is True and checks["cache_dir"][1] is True


def test_preflight_unverifiable_on_ssh_error_is_none():
    checks = gw._variant_offline_preflight(_BrokenExec(), "/dfs/vep.sif", "/dfs/cache", "/dfs/c.vcf.gz")
    assert all(v[1] is None for v in checks.values())         # None ≠ False: couldn't check


def test_preflight_empty_path_is_none():
    ex = _FakeExec({"/dfs/cache"})
    checks = gw._variant_offline_preflight(ex, "", "/dfs/cache", "")
    assert checks["vep_image"][1] is None and checks["clinvar_vcf"][1] is None
    assert checks["cache_dir"][1] is True
