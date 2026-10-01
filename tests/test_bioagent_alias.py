"""The ``bioagent`` -> ``aiscientist`` rename (2026-09-30) keeps the old name importable.

Things outside the repository still say ``bioagent``: the prod systemd unit runs ``python -m
bioagent.gateway``, older images' runscripts run ``python -m bioagent.tools...`` from the synced source,
and checkpoints/pickles recorded ``bioagent.*`` class paths. These tests pin that all of them resolve
to the SAME ``aiscientist`` module objects, not to a second copy."""
from __future__ import annotations

import os
import pickle
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def test_old_name_is_the_same_module_object():
    import aiscientist.core.config as new
    import bioagent.core.config as old
    from bioagent.core import config as via_from

    assert old is new and via_from is new
    # the real module keeps its own spec, so importlib.resources / reload see the real package
    assert new.__spec__.name == "aiscientist.core.config"


def test_a_pickle_naming_the_old_path_loads_the_current_object():
    import aiscientist.core.config as cfg

    assert pickle.loads(b"cbioagent.core.config\nenv\n.") is cfg.env


def test_python_dash_m_with_the_old_name_runs_the_gateway():
    pytest.importorskip("fastapi")
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    r = subprocess.run([sys.executable, "-m", "bioagent.gateway", "--help"], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "--port" in r.stdout


def test_legacy_env_names_are_mirrored_before_any_submodule_loads():
    code = ("import os, aiscientist; "
            "print(os.environ.get('AISCIENTIST_ZZ_ALIAS_PROBE'), os.environ.get('BIOAGENT_ZZ_NEW_PROBE'))")
    env = {**os.environ, "PYTHONPATH": str(SRC), "BIOAGENT_ZZ_ALIAS_PROBE": "old",
           "AISCIENTIST_ZZ_NEW_PROBE": "new"}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert r.stdout.split() == ["old", "new"], r.stderr


def test_the_synced_source_carries_the_alias(tmp_path):
    pytest.importorskip("fastapi")
    from aiscientist.gateway.app import _pack_aiscientist_source

    out = tmp_path / "src.tgz"
    _pack_aiscientist_source(out)
    with tarfile.open(out) as tar:
        names = set(tar.getnames())
    assert "bioagent/__init__.py" in names and "aiscientist/__init__.py" in names
