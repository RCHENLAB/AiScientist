"""MCP names and original schemas survive planning, editing and pinned execution."""
import json
import shlex

import pytest

from aiscientist.agents.plan_doc import PlanDoc, PlanStep, apply_edit, bind_settings, call_settings, check_plan
from aiscientist.agents.research_lab import (
    _declared_tools, _is_literature_step, _known_tool_names, _primary_tool, _tool_schemas,
    check_plan_tooling, retick_tools,
)
from aiscientist.agents.scope import _mentioned
import test_mcp_bridge
from test_mcp_bridge import Runner, build, write_config

registry = test_mcp_bridge.registry


def configured(registry, schema, name="tables-check"):
    path, _ = registry
    write_config(path.parent, entries=[{"name": name, "description": "Check supplied values.", "inputSchema": schema}],
                 overrides={"prefix": "", "resources": {}})
    _known_tool_names.cache_clear()
    runner = Runner()
    tool, = build(runner)
    return runner, tool


@pytest.fixture(autouse=True)
def clear_tool_names():
    _known_tool_names.cache_clear()
    yield
    _known_tool_names.cache_clear()


@pytest.mark.parametrize("name", ["tables-check", "_check", "Tables-Check"])
def test_name_contract_across_classification_binding_editing_and_scope(registry, name):
    _, tool = configured(registry, {"type": "object", "properties": {"value": {"type": "integer"}}}, name)
    text = f'Use `{name}(value=3)` for biological interpretation.'
    assert _declared_tools(text) == {name}
    assert _primary_tool(text) == name and not _is_literature_step(text)
    assert call_settings(text, name) == {"value": "3"}
    assert retick_tools([f"Use {name} for interpretation."], [name]) == [f"Use `{name}` for interpretation."]
    assert _mentioned(f"only {name}", frozenset({name}), True) == [name]
    longer = "other-" + name
    assert not call_settings(f"{longer}(value=3)", name)
    assert not _declared_tools(f"{longer}(value=3)")
    assert not _mentioned(f"only {longer}", frozenset({name}), True)
    assert retick_tools([longer], [name]) == [longer]
    before = PlanDoc("", (PlanStep("s1", f"Use `{name}` then `{longer}`.", name),))
    after = PlanDoc("", (PlanStep("s1", before.steps[0].text, "replacement"),))
    edited, _, _ = apply_edit(before, after)
    assert edited.steps[0].text == f"Use `replacement` then `{longer}`."
    _, findings = check_plan_tooling([f'{name}(value="bad")'], [tool])
    assert findings and findings[0]["kind"] == "bad_value"


def test_root_ref_plan_edit_and_harness_execution_use_the_approved_setting(registry):
    from aiscientist.agents.research_harness import ResearchHarness
    from test_plan_yaml_flow import _lab, _run, _of

    schema = {"type": "object", "$ref": "#/$defs/Args", "$defs": {"Args": {
        "type": "object", "properties": {"value": {"type": "integer", "minimum": 1}},
        "required": ["value"], "additionalProperties": False}}}
    runner, tool = configured(registry, schema)

    def scientist(messages, _tools):
        done = any(m.get("role") == "tool" for m in messages)
        return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
            "name": "finish" if done else tool.name,
            "arguments": json.dumps({"answer": "done"} if done else {"value": 1})}}]}

    def complete(messages):
        system = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in system:
            return json.dumps({"agenda": ['Check with `tables-check(value=2)` for biological interpretation.']})
        if "filling in a form" in system:
            return "[]"
        if "rigorous scientific Critic" in system:
            return '{"verdict":"accept","score":0.9,"critique":"ok"}'
        return "Report."

    lab = _lab([], {}, scientist=ResearchHarness(catalog=[tool], chat_fn=scientist))
    lab._complete_fn = complete

    def review(n, _steps, doc):
        if n == 1:
            assert doc["steps"][0]["tool"] == tool.name
            assert doc["steps"][0]["settings"] == {"value": 2}
            return {"action": "edit", "plan_yaml": doc["yaml"].replace("value: 2", "value: 4")}
        return {"action": "approve"}

    _, events, _ = _run(lab, review)
    assert _of(events, "plan_edited") and _of(events, "plan_setting_enforced")
    assert len(runner.calls) == 1
    cli = next(line for line in runner.calls[0][0].splitlines() if line.startswith("aiscientist-mcp "))
    assert json.loads(shlex.split(cli)[7]) == {"value": 4}


@pytest.mark.parametrize("form", ["{name}", "{name}(value=3)"])
def test_name_correction_preserves_complete_names_and_corrects_only_standalone_aliases(registry, form):
    from aiscientist.agents.research_harness import HarnessTool
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
    _, tool = configured(registry, schema, "tables-run_qc_scanpy")
    legacy = HarnessTool("run_scanpy_qc", "Legacy", schema, lambda args, ctx: {})
    exact = form.format(name=tool.name)
    typo = form.format(name="run_qc_scanpy")
    text = f"Use `{exact}` and `{typo}`; other-run_qc_scanpy(value=3)."
    fixed, findings = check_plan_tooling([text], [tool, legacy])
    assert fixed == [text.replace(f"`{typo}`", f"`{form.format(name=legacy.name)}`")]
    assert findings == [{"kind": "renamed", "tool": "run_qc_scanpy", "detail": legacy.name}]
    _, registered_alias = configured(registry, schema, "run_qc_scanpy")
    assert check_plan_tooling([f"Use `{typo}`."], [registered_alias, legacy]) == ([f"Use `{typo}`."], [])


@pytest.mark.parametrize("early_unevaluated", [False, True])
def test_conditional_annotations_follow_one_consistent_branch(registry, early_unevaluated):
    schema = {"type": "object", "properties": {"kind": {"type": "string"}, "query_id": {"type": "string"}},
              "if": {"properties": {"kind": {"const": "a"}}, "required": ["kind"]},
              "then": {"properties": {"a": {"type": "string"}}},
              "else": {"properties": {"b": {"type": "string"}}}, "unevaluatedProperties": False}
    if early_unevaluated:
        schema = {"unevaluatedProperties": False, **schema}
    _, tool = configured(registry, schema)
    props = _tool_schemas([tool])[tool.name]
    assert tool.executor({"b": "ok"}, None)["status"] == "ok"
    assert bind_settings('b="ok"', props, {"b": "ok"}) == ({"b": "ok"}, [])
    _, findings = check_plan(PlanDoc("", (PlanStep("s1", "Check", tool.name, {"b": "ok"}),)),
                             {tool.name: props})
    assert not findings
    # These pins need a future runtime argument, so ordinary full validation
    # cannot short-circuit the branch/annotation check.
    _, tool = configured(registry, {**schema, "required": ["query_id"]})
    props = _tool_schemas([tool])[tool.name]
    assert not props.errors({"a": "ok"}) and not props.errors({"b": "ok"})
    assert props.errors({"a": "ok", "b": "ok"})  # cannot combine annotations from incompatible branches
    assert props.errors({"kind": "a", "b": "ok"})
    assert props.errors({"other": "unexpected"})


def test_fixed_false_condition_cannot_be_rescued_by_an_unbound_property(registry):
    schema = {"type": "object", "if": {"properties": {
        "mode": {"const": "number"}, "flag": {"type": "boolean"}}},
        "then": {"properties": {"value": {"type": "integer"}}},
        "else": {"properties": {"value": {"type": "string"}}}}
    _, tool = configured(registry, schema)
    props = _tool_schemas([tool])[tool.name]
    pins = {"mode": "text", "value": 3}
    _, findings = check_plan(PlanDoc("", (PlanStep("s1", "Check", tool.name, pins),)), {tool.name: props})
    assert findings
    bound, dropped = bind_settings('mode="text", value=3', props, pins)
    assert bound == {} and dropped
    assert tool.executor(pins, None)["status"] == "error"
    assert not props.errors({"mode": "text", "value": "word"})
    assert not props.errors({"value": 3})  # mode genuinely remains unknown


@pytest.mark.parametrize("dialect", ["https://json-schema.org/draft/2020-12/schema",
                                     "http://json-schema.org/draft-07/schema#"])
def test_reference_dialect_preserves_partial_root_and_complete_nested_validation(registry, dialect):
    args = {"$schema": dialect, "type": "object", "properties": {
        "query_id": {"type": "string"}, "top": {"type": "integer"}}, "required": ["query_id"]}
    schema = {"type": "object", "$ref": "#/$defs/Args", "$defs": {"Args": args},
              "properties": {"nested": {"$ref": "#/$defs/Args"}}}
    _, tool = configured(registry, schema)
    props = _tool_schemas([tool])[tool.name]
    assert bind_settings("top=5", props, {"top": 5}) == ({"top": 5}, [])
    _, findings = check_plan(PlanDoc("", (PlanStep("s1", "top=5", tool.name, {"top": 5}),)), {tool.name: props})
    assert not findings
    assert props.errors({"top": "wrong"})
    assert props.errors({"nested": {"top": 5}})
    assert not props.errors({"nested": {"top": 5, "query_id": "runtime"}})
    assert tool.executor({"top": 5}, None)["status"] == "error"
    assert tool.executor({"top": 5, "query_id": "runtime"}, None)["status"] == "ok"
    # Preserve the chosen dialect, not merely the deferred required check:
    # unevaluatedProperties was introduced after Draft 7.
    _, tool = configured(registry, {"type": "object", "$ref": "#/$defs/Args", "$defs": {
        "Args": {**args, "unevaluatedProperties": False}}})
    errors = _tool_schemas([tool])[tool.name].errors({"top": 5, "extra": "permitted in Draft 7"})
    assert bool(errors) == ("2020-12" in dialect)


@pytest.mark.parametrize("schema, valid, invalid", [
    ({"type": "object", "allOf": [
        {"properties": {"n": {"type": "integer", "minimum": 1}}},
        {"properties": {"n": {"maximum": 4}}}]}, {"n": 3}, {"n": 8}),
    ({"type": "object", "anyOf": [
        {"properties": {"value": {"type": "integer"}}},
        {"properties": {"value": {"type": "string"}}}]}, {"value": "ok"}, {"value": []}),
    ({"type": "object", "properties": {"card": {"type": "boolean"}}, "dependentSchemas": {
        "card": {"properties": {"billing_address": {"type": "string"}}, "required": ["billing_address"]}}},
     {"card": True, "billing_address": "address"}, {"card": True, "billing_address": 3}),
    ({"type": "object", "properties": {"kind": {"enum": ["n", "s"]}},
      "if": {"properties": {"kind": {"const": "n"}}, "required": ["kind"]},
      "then": {"properties": {"value": {"type": "integer"}}},
      "else": {"properties": {"value": {"type": "string"}}}},
     {"kind": "s", "value": "ok"}, {"kind": "n", "value": "wrong"}),
    ({"type": "object", "$defs": {"Ns": {"type": "array", "items": {"type": "integer"}}},
      "properties": {"values": {"$ref": "#/$defs/Ns"}}}, {"values": [1, 2]}, {"values": [True]}),
    ({"type": "object", "properties": {"n": {"type": ["integer", "null"]}}}, {"n": None}, {"n": True}),
    ({"type": "object", "properties": {"value": {"oneOf": [
        {"type": "integer", "minimum": 0}, {"type": "integer", "maximum": 10}]}}},
     {"value": 20}, {"value": 5}),
    ({"type": "object", "patternProperties": {"^x_": {"type": "integer"}}, "additionalProperties": False},
     {"x_n": 2}, {"x_n": "bad"}),
])
def test_plan_checks_and_calls_preserve_full_schema_constraints(registry, schema, valid, invalid):
    runner, tool = configured(registry, schema)
    assert tool.parameters == schema  # the generic bridge does not add restrictions
    schemas = _tool_schemas([tool])
    def doc(values):
        return PlanDoc("", (PlanStep("s1", "Check values", tool.name, values),))
    _, findings = check_plan(doc(valid), schemas)
    assert not findings
    _, findings = check_plan(doc(invalid), schemas)
    assert findings
    assert tool.executor(valid, None)["status"] == "ok"
    assert tool.executor(invalid, None)["status"] == "error"
    assert len(runner.calls) == 1


def test_partial_plans_defer_runtime_arguments_without_loosening_complete_calls(registry):
    schema = {"type": "object", "properties": {"query_id": {"type": "string"}, "top": {"type": "integer"}},
              "required": ["query_id"]}
    runner, tool = configured(registry, schema)
    props = _tool_schemas([tool])[tool.name]
    assert bind_settings("Show top 10", props, {"top": 10}) == ({"top": 10}, [])
    _, findings = check_plan(PlanDoc("", (PlanStep("s1", "Show top 10", tool.name, {"top": 10}),)),
                             {tool.name: props})
    assert not findings
    assert tool.executor({"top": 10}, None)["status"] == "error"
    assert not runner.calls


def test_extras_and_unbound_branch_choices_are_not_mistaken_for_invalid_settings(registry):
    schema = {"type": "object", "oneOf": [
        {"properties": {"kind": {"const": "a"}, "n": {"type": "integer"}}, "required": ["kind"]},
        {"properties": {"kind": {"const": "b"}, "n": {"type": "integer"}}, "required": ["kind"]}]}
    _, tool = configured(registry, schema)
    props = _tool_schemas([tool])[tool.name]
    assert not props.errors({"n": 2, "extra": "permitted"})
    assert props.errors({"kind": "c", "n": 2})
    assert tool.executor({"kind": "a", "n": 2, "extra": "permitted"}, None)["status"] == "ok"


def test_nullable_pattern_and_nested_values_are_checked_when_binding(registry):
    schema = {"type": "object", "properties": {
        "n": {"type": ["integer", "null"]},
        "options": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"]}},
        "patternProperties": {"^x_": {"type": "integer"}}, "additionalProperties": False}
    _, tool = configured(registry, schema)
    props = _tool_schemas([tool])[tool.name]
    assert bind_settings("n=null and x_count=2", props, {"n": None, "x_count": 2}) == (
        {"n": None, "x_count": 2}, [])
    assert props.errors({"options": {}})  # a supplied nested object is not a partial argument list


def test_unbound_conditional_is_deferred_but_selected_branch_is_checked(registry):
    schema = {"type": "object", "properties": {"mode": {"type": "string"}},
              "if": {"properties": {"mode": {"const": "number"}}},
              "then": {"properties": {"value": {"type": "integer"}}},
              "else": {"properties": {"value": {"type": "string"}}}}
    _, tool = configured(registry, schema)
    props = _tool_schemas([tool])[tool.name]
    assert not props.errors({"value": "word"})
    assert props.errors({"mode": "number", "value": "word"})
    assert tool.executor({"mode": "text", "value": "word"}, None)["status"] == "ok"


def test_partial_validation_checks_the_branch_predicate_not_unrelated_properties(registry):
    schema = {"type": "object", "properties": {"unused": {"type": "string"}}, "oneOf": [
        {"properties": {"value": {"type": "integer", "minimum": 0}}},
        {"properties": {"value": {"type": "integer", "maximum": 10}}}]}
    _, tool = configured(registry, schema)
    assert _tool_schemas([tool])[tool.name].errors({"value": 5})
    schema = {"type": "object", "if": {"not": {"required": ["flag"]}},
              "then": {"properties": {"value": {"type": "integer"}}},
              "else": {"properties": {"value": {"type": "string"}}}}
    _, tool = configured(registry, schema)
    assert not _tool_schemas([tool])[tool.name].errors({"value": "word"})
    assert tool.executor({"flag": True, "value": "word"}, None)["status"] == "ok"


def test_nonfinite_plan_settings_are_refused_before_approval(registry):
    _, tool = configured(registry, {"type": "object", "properties": {"n": {"type": "number"}}})
    for value in (float("nan"), float("inf")):
        assert _tool_schemas([tool])[tool.name].errors({"n": value})


def test_dependent_required_arguments_are_deferred_only_at_the_plan_root(registry):
    args = {"type": "object", "properties": {"card": {"type": "boolean"}, "billing": {"type": "string"}},
            "dependentRequired": {"card": ["billing"]}}
    _, tool = configured(registry, {**args, "properties": {**args["properties"], "nested": args}})
    props = _tool_schemas([tool])[tool.name]
    assert bind_settings("card=true", props, {"card": True}) == ({"card": True}, [])
    assert props.errors({"nested": {"card": True}})
    assert tool.executor({"card": True}, None)["status"] == "error"
    assert tool.executor({"card": True, "billing": "address"}, None)["status"] == "ok"


@pytest.mark.parametrize("predicate, pins, completion", [
    ({"minProperties": 2}, {"value": "word"}, {"value": "word"}),
    ({"minProperties": 2}, {"value": 3}, {"value": 3, "extra": True}),
    ({"propertyNames": {"pattern": "^value$"}}, {"value": "word"}, {"value": "word", "extra": True}),
    ({"dependentRequired": {"card": ["billing"]}}, {"card": True, "value": 3},
     {"card": True, "billing": "address", "value": 3}),
])
def test_whole_object_predicates_wait_for_unbound_runtime_arguments(registry, predicate, pins, completion):
    schema = {"type": "object", "if": predicate, "then": {"properties": {"value": {"type": "integer"}}},
              "else": {"properties": {"value": {"type": "string"}}}}
    _, tool = configured(registry, schema)
    props = _tool_schemas([tool])[tool.name]
    assert not props.errors(pins)
    assert props.errors({**pins, "value": []})
    assert tool.executor(completion, None)["status"] == "ok"


@pytest.mark.parametrize("schema, settings", [
    ({"type": "object", "properties": {"card": {"type": "boolean"}}, "dependentSchemas": {
        "card": {"properties": {"billing_address": {"type": "string"}}, "required": ["billing_address"]}}},
     {"billing_address": 3}),
    *[({"type": "object", "properties": {"mode": {"type": "string"}},
        "if": {"properties": {"mode": {"const": "number"}}, "required": ["mode"]},
        "then": {"properties": {"value": {"type": "integer"}}}}, {"mode": "text", "value": value})
      for value in ("word", "3")],
    ({"type": "object", "anyOf": [
        {"properties": {"mode": {"const": "number"}, "value": {"type": "integer"}}},
        {"properties": {"mode": {"const": "free"}}}]}, {"mode": "free", "value": "3"}),
])
def test_inactive_branches_neither_reject_nor_change_valid_plan_values(registry, schema, settings):
    _, tool = configured(registry, schema)
    props = _tool_schemas([tool])[tool.name]
    doc, findings = check_plan(PlanDoc("", (PlanStep("s1", "Check", tool.name, settings),)), {tool.name: props})
    assert not findings and doc.steps[0].settings == settings
    text = "Check " + json.dumps(settings)
    assert bind_settings(text, props, settings) == (settings, [])
    assert tool.executor(settings, None)["status"] == "ok"


def test_mcp_call_syntax_distinguishes_quoted_values_from_json_literals(registry):
    _, tool = configured(registry, {"type": "object", "properties": {"n": {"type": ["integer", "null"]}}})
    props = _tool_schemas([tool])[tool.name]
    for literal, expected in [("3", 3), ("null", None), ("None", None), ('"3"', "3")]:
        text = f"tables-check(n={literal})"
        values = call_settings(text, tool.name, json_literals=True)
        assert values == {"n": expected}
        _, findings = check_plan(PlanDoc("", (PlanStep("s1", text, tool.name, values),)), {tool.name: props})
        assert bool(findings) == (literal == '"3"')
    assert call_settings("tables-check(flag=True)", tool.name, json_literals=True) == {"flag": True}


def test_non_mcp_prose_checks_and_plan_coercion_keep_their_existing_behavior():
    from aiscientist.agents.research_harness import HarnessTool
    tool = HarnessTool("custom_function", "Legacy", {"type": "object", "properties": {
        "n": {"type": "integer", "enum": [2]}}}, lambda args, ctx: {})
    assert check_plan_tooling(["custom_function(n=2)"], [tool])[1] == []
    doc, findings = check_plan(PlanDoc("", (PlanStep("s1", "Check", tool.name, {"n": "2"}),)), _tool_schemas([tool]))
    assert not findings and doc.steps[0].settings == {"n": 2}
