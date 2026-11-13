"""Read-only introspection uses real callable source and declared schemas after tool retirement."""
from __future__ import annotations

import dataclasses
import types
from pathlib import Path

import pytest

from aiscientist.agents.registry import _route_to_executor
from aiscientist.agents.tool_source import _declared_defaults, _split_top_level, make_tool_source_tool
from aiscientist.tools import catalog
from aiscientist.tools.sdk import HarnessTool


def source_helper(value):
    return value


def source_fixture(args, ctx):
    count = int(args.get("count", 50))
    return {"count": source_helper(count)}


def _catalog():
    return [HarnessTool("source_fixture", "Read fixture parameter metadata.",
                        {"type": "object", "properties": {"count": {
                            "type": "integer", "default": 50, "description": "maximum entries"}}}, source_fixture)]


def _reader(tools=None):
    return make_tool_source_tool(lambda: _catalog() if tools is None else tools)


def test_reads_real_callable_and_its_declared_contract():
    result = _reader().executor({"tool": "source_fixture"}, None)
    assert "def source_fixture" in result["source"]
    assert result["module"] == __name__
    assert Path(result["file"]).samefile(__file__) and result["first_line"] > 0
    assert result["declared_parameters"] == _catalog()[0].parameters
    assert result["declared_description"] == _catalog()[0].description


def test_declared_defaults_carry_value_meaning_and_source_line():
    result = _reader().executor({"tool": "source_fixture"}, None)
    default = next(value for value in result["defaults"] if value["param"] == "count")
    assert default["value"] == 50 and default["declared"]
    assert default["meaning"] == "maximum entries" and default["line"] > 0


@pytest.mark.parametrize("phrase", ["THIS dataset", "run_code", "conventional and correct", "downstream step"])
def test_review_prompt_requires_relevant_evidence(phrase):
    result = _reader().executor({"tool": "source_fixture"}, None)
    assert phrase in result["review_prompt"]


def test_helpers_are_read_only_symbol_lookups():
    result = _reader().executor({"tool": "source_fixture", "symbol": "source_helper"}, None)
    assert "def source_helper" in result["source"] and result["symbol"] == "source_helper"
    assert "defaults" not in result
    tool = _reader()
    assert not tool.reads_private_data and set(tool.parameters["properties"]) == {"tool", "symbol"}


def test_unknown_tool_symbol_and_empty_catalog_are_explicit():
    result = _reader().executor({"tool": "absent"}, None)
    assert "unknown tool" in result["error"] and "source_fixture" in result["available"]
    result = _reader().executor({"tool": "source_fixture", "symbol": "absent"}, None)
    assert "does not resolve a symbol" in result["error"] and "read the tool body" in result["hint"]
    assert "no tool catalog" in _reader([]).executor({"tool": "source_fixture"}, None)["error"]


def test_literal_default_parser_preserves_containers_quotes_and_multiple_calls():
    source = ('def f(args, ctx):\n'
              '    a = int(args.get("n_genes", 50))\n'
              '    b = str(args.get("groupby", "leiden"))\n'
              '    c = args.get("gene_sets", ["GO", "Reactome"])\n'
              '    d = float(args.get("thr", 0.05)) * int(args.get("k", 3))\n'
              '    e = args.get("no_default")\n')
    found = {value["param"]: value["default"] for value in _declared_defaults(source)}
    assert found == {"n_genes": "50", "groupby": '"leiden"', "gene_sets": '["GO", "Reactome"]',
                     "thr": "0.05", "k": "3"}
    assert _split_top_level('"a", [1, 2], "x,y"') == ['"a"', ' [1, 2]', ' "x,y"']


def test_routed_callable_exposes_implementation_and_helpers():
    calls = []
    line = types.SimpleNamespace(run_tool=lambda name, args, ctx: calls.append(name) or {"status": "dispatched"})
    routed = _route_to_executor(_catalog()[0], line, ("source_fixture",))
    assert routed.executor({}, None)["status"] == "dispatched" and calls == ["source_fixture"]
    result = _reader([routed]).executor({"tool": "source_fixture"}, None)
    assert "def source_fixture" in result["source"] and Path(result["file"]).samefile(__file__)
    helper = _reader([routed]).executor({"tool": "source_fixture", "symbol": "source_helper"}, None)
    assert "def source_helper" in helper["source"]


def test_manifest_can_recover_opaque_wrapper(monkeypatch):
    base = _catalog()[0]
    manifest = types.SimpleNamespace(folder=Path(__file__).parent, runs_on="hpc:analysis",
                                     factory=lambda: lambda: base, injected=lambda: [], name=base.name, entry="fixture")
    monkeypatch.setattr(catalog, "manifest", lambda name: manifest if name == base.name else None)
    opaque = dataclasses.replace(base, executor=print)
    result = _reader([opaque]).executor({"tool": base.name}, None)
    assert "def source_fixture" in result["source"] and result["runs_on"] == "hpc:analysis"


def test_closure_dependencies_resolve_to_unwrapped_implementation():
    import functools

    @functools.wraps(source_fixture)
    def routed(args, ctx):
        return source_fixture(args, ctx)

    def factory(child):
        def composite(args, ctx):
            return child(args, ctx)
        return composite

    tool = dataclasses.replace(_catalog()[0], executor=factory(routed))
    result = _reader([tool]).executor({"tool": tool.name}, None)
    assert str(Path(__file__)) in result["dispatches_to"]["child"]["where"]
    child = _reader([tool]).executor({"tool": tool.name, "symbol": "child"}, None)
    assert "def source_fixture" in child["source"]


def test_platform_tool_has_no_invented_execution_location():
    result = _reader().executor({"tool": "source_fixture"}, None)
    assert "runs_on" not in result and "note" not in result
