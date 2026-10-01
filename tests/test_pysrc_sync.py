"""The source tree the gateway ships to HPC3 for the analysis jobs (``_pack_bioagent_source``).

Jobs import ``bioagent.tools...`` from that tree, so it has to carry the tools' manifests as well as
their code, and after the repository split it has to merge two installs of the ``bioagent``
namespace (the platform and AiScientist-tools) into one tree."""
from __future__ import annotations

import tarfile

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway.app import _pack_bioagent_source  # noqa: E402


def _members(tgz):
    with tarfile.open(tgz) as tar:
        return set(tar.getnames())


def test_the_live_package_ships_tool_code_and_manifests(tmp_path):
    out = tmp_path / "src.tgz"
    _pack_bioagent_source(out)
    names = _members(out)
    for expected in ("bioagent/gateway/app.py", "bioagent/tools/sdk.py", "bioagent/tools/catalog.py",
                     "bioagent/tools/run_de/tool.py", "bioagent/tools/run_de/TOOL.md",
                     "bioagent/tools/scrna_cli.py"):
        assert expected in names, expected
    assert not any(n.endswith(".pyc") or "__pycache__" in n for n in names)


def test_namespace_portions_merge_into_one_tree(tmp_path):
    platform, tools = tmp_path / "platform" / "bioagent", tmp_path / "tools" / "bioagent"
    (platform / "gateway").mkdir(parents=True)
    (platform / "gateway" / "app.py").write_text("x = 1\n")
    (tools / "tools" / "run_de").mkdir(parents=True)
    (tools / "tools" / "run_de" / "TOOL.md").write_text("---\nname: run_de\n---\n")
    out = tmp_path / "src.tgz"
    _pack_bioagent_source(out, portions=[platform, tools])
    names = _members(out)
    assert "bioagent/gateway/app.py" in names and "bioagent/tools/run_de/TOOL.md" in names


def test_two_portions_shipping_the_same_package_is_refused(tmp_path):
    a, b = tmp_path / "a" / "bioagent", tmp_path / "b" / "bioagent"
    (a / "tools").mkdir(parents=True)
    (b / "tools").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="provided by both"):
        _pack_bioagent_source(tmp_path / "src.tgz", portions=[a, b])


def test_the_same_directory_listed_twice_is_one_portion(tmp_path):
    # Seen in the split rehearsal: pytest's `pythonpath = ["src"]` plus PYTHONPATH=src put the
    # platform's src on sys.path twice, so bioagent.__path__ named the same portion twice.
    portion = tmp_path / "platform" / "bioagent"
    (portion / "gateway").mkdir(parents=True)
    (portion / "gateway" / "app.py").write_text("x = 1\n")
    out = tmp_path / "src.tgz"
    assert _pack_bioagent_source(out, portions=[portion, tmp_path / "platform" / "." / "bioagent"]) == [portion.resolve()]
    assert "bioagent/gateway/app.py" in _members(out)
