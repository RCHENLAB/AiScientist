"""The source tree the gateway ships to HPC3 for the analysis jobs (``_pack_aiscientist_source``).

Jobs import ``aiscientist.tools...`` from that tree, so it has to carry the tools' manifests as well as
their code, and after the repository split it has to merge two installs of the ``bioagent``
namespace (the platform and AiScientist-tools) into one tree."""
from __future__ import annotations

import tarfile

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway.app import _pack_aiscientist_source  # noqa: E402


def _members(tgz):
    with tarfile.open(tgz) as tar:
        return set(tar.getnames())


def test_the_live_package_ships_tool_code_and_manifests(tmp_path):
    out = tmp_path / "src.tgz"
    _pack_aiscientist_source(out)
    names = _members(out)
    for expected in ("aiscientist/gateway/app.py", "aiscientist/tools/sdk.py", "aiscientist/tools/catalog.py",
                     "aiscientist/tools/run_de/tool.py", "aiscientist/tools/run_de/TOOL.md",
                     "aiscientist/tools/scrna_cli.py"):
        assert expected in names, expected
    assert not any(n.endswith(".pyc") or "__pycache__" in n for n in names)


def test_namespace_portions_merge_into_one_tree(tmp_path):
    platform, tools = tmp_path / "platform" / "aiscientist", tmp_path / "tools" / "aiscientist"
    (platform / "gateway").mkdir(parents=True)
    (platform / "gateway" / "app.py").write_text("x = 1\n")
    (tools / "tools" / "run_de").mkdir(parents=True)
    (tools / "tools" / "run_de" / "TOOL.md").write_text("---\nname: run_de\n---\n")
    out = tmp_path / "src.tgz"
    _pack_aiscientist_source(out, portions=[platform, tools])
    names = _members(out)
    assert "aiscientist/gateway/app.py" in names and "aiscientist/tools/run_de/TOOL.md" in names


def test_two_portions_shipping_the_same_package_is_refused(tmp_path):
    a, b = tmp_path / "a" / "aiscientist", tmp_path / "b" / "aiscientist"
    (a / "tools").mkdir(parents=True)
    (b / "tools").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="provided by both"):
        _pack_aiscientist_source(tmp_path / "src.tgz", portions=[a, b])


def test_the_same_directory_listed_twice_is_one_portion(tmp_path):
    # Seen in the split rehearsal: pytest's `pythonpath = ["src"]` plus PYTHONPATH=src put the
    # platform's src on sys.path twice, so aiscientist.__path__ named the same portion twice.
    portion = tmp_path / "platform" / "aiscientist"
    (portion / "gateway").mkdir(parents=True)
    (portion / "gateway" / "app.py").write_text("x = 1\n")
    out = tmp_path / "src.tgz"
    assert _pack_aiscientist_source(out, portions=[portion, tmp_path / "platform" / "." / "aiscientist"]) == [portion.resolve()]
    assert "aiscientist/gateway/app.py" in _members(out)
