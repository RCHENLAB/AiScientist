"""The agent can read the code of the tools it calls.

`run_de` capped its output at 50 genes per group for seven weeks. The tool description said
"differential expression / marker genes per cluster", the result dict reported the cap
truthfully as `n_genes_per_group: 50`, the suite was green, and the reports were self-
consistent. Nothing the model could see said "this is a cap, and 50 may be too few for what
the next step needs" — because the model could only ever see the DESCRIPTION, never the body.

These tests pin the capability that removes that asymmetry, and are written against the exact
shape of the defect that motivated it.
"""

from __future__ import annotations

import types

from bioagent.agents.tool_source import (
    _declared_defaults,
    _split_top_level,
    make_tool_source_tool,
)
from bioagent.tools.scrna_pack import scrna_catalog


def _tool(catalog=None):
    cat = catalog if catalog is not None else scrna_catalog()
    return make_tool_source_tool(lambda: cat), types.SimpleNamespace(catalog=cat)


def test_reading_a_tool_returns_its_real_body_not_its_description():
    tool, ctx = _tool()
    out = tool.executor({"tool": "run_de"}, ctx)
    assert "def run_de" in out["source"]
    assert out["module"] == "bioagent.tools.scrna_pack"
    assert out["file"].endswith("scrna_pack.py") and out["first_line"] > 0
    # The declared contract travels WITH the code, so the two can be compared. A description
    # that promises more than the body delivers is the defect class this exists to surface.
    assert out["declared_description"] == next(
        t.description for t in scrna_catalog() if t.name == "run_de")
    assert out["declared_parameters"]["type"] == "object"


def test_the_defaults_nobody_chose_are_listed_explicitly():
    # This is the whole point: `n_genes=50` was invisible to the model. Surfacing defaults as a
    # structured field turns "read the code" from an instruction that can be skipped into a
    # list the model has to look at.
    tool, ctx = _tool()
    defaults = {d["param"]: d["default"] for d in tool.executor({"tool": "run_de"}, ctx)["defaults"]}
    assert defaults["n_genes"] == "50"          # the cap, stated
    assert defaults["groupby"] == '"leiden"'
    assert all(d["line"] > 0 for d in tool.executor({"tool": "run_de"}, ctx)["defaults"])

    clustering = {d["param"]: d["default"]
                  for d in tool.executor({"tool": "run_clustering"}, ctx)["defaults"]}
    assert clustering["resolution"] == "1.0"    # the other frozen choice
    assert clustering["stability_min"] == "0.90"


def test_the_review_prompt_asks_the_question_that_finds_this_class_of_bug():
    tool, ctx = _tool()
    out = tool.executor({"tool": "run_enrichment"}, ctx)
    prompt = out["review_prompt"]
    assert "THIS dataset" in prompt                     # not "is 50 a reasonable number"
    assert "truncate" in prompt or "cap" in prompt
    assert "run_code" in prompt                         # verify, don't just assert


def test_the_review_prompt_guards_against_flagging_everything():
    # Measured against the served model (scripts/probe_tool_audit.py): the first wording caught
    # both planted defects 3/3 but flagged a perfectly standard QC step 3/3 as well — it read
    # "is anything a problem?" as an invitation to find one. An audit that objects to everything
    # gets ignored, so the prompt now states that "no problem" is the expected answer and
    # demands a concrete damaged downstream step before a flag counts.
    tool, ctx = _tool()
    prompt = tool.executor({"tool": "run_scanpy_qc"}, ctx)["review_prompt"]
    assert "conventional and correct" in prompt
    assert "flagging a sound step costs as much as missing a bad one" in prompt.lower()
    assert "downstream step" in prompt
    assert "is not a problem" in prompt.lower()         # "a different value might be better"


def test_a_helper_the_tool_leans_on_is_reachable_too():
    # Behaviour often lives in the helper, not the tool body — `_slug` decides whether a cell
    # type containing '/' gets a table at all.
    tool, ctx = _tool()
    out = tool.executor({"tool": "run_de", "symbol": "_slug"}, ctx)
    assert "def _slug" in out["source"]
    assert out["symbol"] == "_slug"
    # A module-level fetch is a single symbol, so the defaults scan (a whole-body notion) is off.
    assert "defaults" not in out


def test_unknown_names_are_reported_with_what_is_available():
    tool, ctx = _tool()
    out = tool.executor({"tool": "run_nonexistent"}, ctx)
    assert "unknown tool" in out["error"] and "run_de" in out["available"]

    out = tool.executor({"tool": "run_de", "symbol": "not_a_symbol"}, ctx)
    assert "does not resolve a symbol" in out["error"]
    assert "read the tool body" in out["hint"]


def test_no_catalog_is_a_clear_message_not_a_crash():
    tool = make_tool_source_tool(lambda: [])
    out = tool.executor({"tool": "run_de"}, types.SimpleNamespace())
    assert "no tool catalog" in out["error"]


def test_the_tool_is_read_only_and_carries_no_private_data():
    tool, _ = _tool()
    assert tool.reads_private_data is False
    # Nothing in the schema can name a file to write or code to execute.
    assert set(tool.parameters["properties"]) == {"tool", "symbol"}


# --- the default parser -------------------------------------------------------


def test_default_parsing_handles_the_shapes_that_appear_in_real_tools():
    src = (
        'def f(args, ctx):\n'
        '    a = int(args.get("n_genes", 50))\n'
        '    b = str(args.get("groupby", "leiden"))\n'
        '    c = args.get("gene_sets", ["GO", "Reactome"])\n'      # a bracketed default
        '    d = float(args.get("thr", 0.05)) * int(args.get("k", 3))\n'   # two on one line
        '    e = args.get("no_default")\n'                          # no default -> not listed
    )
    got = {d["param"]: d["default"] for d in _declared_defaults(src)}
    assert got["n_genes"] == "50"
    assert got["groupby"] == '"leiden"'
    assert got["gene_sets"] == '["GO", "Reactome"]'      # the comma inside [] is not a split
    assert got["thr"] == "0.05" and got["k"] == "3"      # both calls on one line are found
    assert "no_default" not in got


def test_split_top_level_ignores_commas_inside_brackets_and_quotes():
    assert _split_top_level('"a", [1, 2], "x,y"') == ['"a"', ' [1, 2]', ' "x,y"']
    assert _split_top_level("only") == ["only"]
