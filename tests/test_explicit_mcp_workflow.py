"""Explicit registered literature workflows retain typed settings and plan review."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from aiscientist.agents import mcp_bridge
from aiscientist.agents.mcp_workflow import explicit_workflow, validated_agenda
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
from aiscientist.agents.research_lab import LabConfig, ResearchLab
from aiscientist.agents.scope import read_scope
from aiscientist.gateway import app as gw


@pytest.fixture(autouse=True)
def clear_cached_tool_names():
    from aiscientist.agents.research_lab import _known_tool_names
    _known_tool_names.cache_clear()
    yield
    _known_tool_names.cache_clear()


@pytest.fixture
def setup(monkeypatch):
    names = ['literature_search', 'search_corpus', 'deep_literature', 'extract_haploinsufficient_genes']
    server = SimpleNamespace(name='literature', adapter='analysis', tools=[{'name': n} for n in names],
                             planner_name=lambda name: 'mcp_' + name)
    monkeypatch.setattr(mcp_bridge, 'configured_servers', lambda: (server,))
    args = [{'query': 'PRPF31 deletion', 'limit': 3, 'analysis_id': 'web-literature'},
            {'queries': ['PRPF31 segregation'], 'top_k': 3, 'terms': ['PRPF31'], 'analysis_id': 'web-literature'},
            {'question': 'What deletion segregated in the Chinese family?', 'analysis_id': 'web-literature'},
            {'max_passages': 4, 'max_cost_usd': 0.1, 'pilot_fraction': 0.01, 'analysis_id': 'web-literature'}]
    request = ('Use the selected model and execute exactly the following registered MCP tools in the listed order. '
               'Do not substitute legacy tools, generic code execution, or another model.\n' + '\n'.join(
                   f'{i}. `mcp_{name}` {json.dumps(arguments)}'
                   for i, (name, arguments) in enumerate(zip(names, args), 1)))
    catalog = [HarnessTool(name='mcp_' + name, description='registered test tool', parameters={
        'type': 'object', 'properties': {key: {'type': ('number' if isinstance(value, (float, int)) else
                                                      'array' if isinstance(value, list) else 'string')}
                                       for key, value in arguments.items()},
        'additionalProperties': False}, executor=lambda *_: pytest.fail('planning cannot execute'))
               for name, arguments in zip(names, args)]
    return request, args, catalog


def test_ordered_workflow_has_exact_scope_without_a_model_inventing_exclusions(setup):
    question, args, catalog = setup
    calls = explicit_workflow(question)
    assert [c.arguments for c in calls] == args
    scope = read_scope(question, {t.name: t.description for t in catalog},
                       lambda _: pytest.fail('explicit scope must not ask a model'))
    assert scope.keep == tuple(t.name for t in catalog) and not scope.exclude
    assert not scope.allows('run_code') and not scope.allows('literature_search')
    assert len(validated_agenda(calls, catalog)) == 4


def test_pi_emits_reviewable_literal_plan_and_refuses_missing_live_tool(setup):
    question, args, catalog = setup
    scientist = ResearchHarness(catalog=catalog)
    lab = ResearchLab(scientist=scientist, ctx=HarnessContext(),
                      complete_fn=lambda *_a, **_kw: pytest.fail('literal first draft needs no model'))
    events = []
    kind, agenda = lab._pi_plan(question, events.append)
    assert kind == 'agenda' and len(agenda) == 4
    assert any(e.get('source') == 'explicit_mcp' for e in events)
    assert [step.settings for step in lab._plan_doc.steps] == args
    lab._present_plan_document(question, agenda, events.append)
    assert [step.settings for step in lab._plan_doc.steps] == args
    assert lab._plan_review(question, agenda, events.append) == agenda
    lab.scientist.catalog = [t for t in catalog if t.name != 'mcp_extract_haploinsufficient_genes']
    kind, reason = lab._pi_plan(question, events.append)
    assert kind == 'failed' and 'mcp_extract_haploinsufficient_genes' in reason


def test_gateway_dispatches_workflow_to_normal_research_review(setup, monkeypatch):
    question, _, _ = setup
    calls = []
    async def reviewed(conn, req):
        calls.append((conn, req))
    monkeypatch.setattr(gw, '_run_lab', reviewed)
    monkeypatch.setattr(gw, '_run_quick_chat', lambda *_: pytest.fail('not a plain chat'))
    monkeypatch.setattr(gw, '_corpus_curation', lambda *_: pytest.fail('not an inferred corpus scan'))
    connection = object()
    request = gw.LabRequest(connection_id='test', question=question, route='chat')
    asyncio.run(gw._dispatch_lab_run(connection, request))
    assert calls == [(connection, request)]  # same selected-model session, same review/cancel path


@pytest.mark.parametrize('alter', [
    lambda q: q.replace('execute exactly', 'Do not execute'),
    lambda q: 'Explain this hypothetical example. ' + q,
    lambda q: q.replace('execute exactly the following registered MCP tools in the listed order', 'Explain this example'),
    lambda q: q.replace('mcp_deep_literature', 'mcp_unregistered_deep_literature'),
    lambda q: q.replace('3. `', '7. `'),
    lambda q: q + '\nDo not call any of these tools.',
])
def test_non_execution_and_incomplete_lists_keep_existing_routing(setup, alter):
    assert not explicit_workflow(alter(setup[0]))


def test_json_schema_applies_before_review_and_no_legacy_alias_is_admitted(setup, monkeypatch):
    question, _, catalog = setup
    calls = explicit_workflow(question.replace('"max_cost_usd": 0.1', '"max_cost_usd": "0.1"'))
    with pytest.raises(ValueError, match='max_cost_usd'):
        validated_agenda(calls, catalog)
    monkeypatch.setattr(mcp_bridge, 'configured_servers', lambda: ())
    assert not explicit_workflow(question)
    assert not explicit_workflow('Explain PRPF31 haploinsufficiency with published evidence')


def test_nested_json_multiline_source_repeated_calls_survive_review_and_cancel(monkeypatch):
    names = ['run_depth_matched_de', 'make_schematic', 'run_de']
    monkeypatch.setattr(mcp_bridge, 'configured_servers', lambda: (
        SimpleNamespace(adapter='analysis', tools=[{'name': n} for n in names],
                        planner_name=lambda n: 'mcp_' + n),))
    dot = 'digraph G {\n a [label="Gene (PRPF31)"];\n a -> b;\n}'
    args = [{'analysis_id': 'web-heca', 'bins': [[1, 2], [3, None]], 'options': {'enabled': True}},
            {'analysis_id': 'figure', 'source': dot},
            {'analysis_id': 'one', 'label': 'first'}, {'analysis_id': 'two', 'label': 'second'}]
    call_names = ['mcp_run_depth_matched_de', 'mcp_make_schematic', 'mcp_run_de', 'mcp_run_de']
    question = ('Run exactly these 4 registered MCP tool calls in this order. Do not add analyses. '
                'Report actual completion.\n' + '\n'.join(
                    f'{i}. `{name}` {json.dumps(value)}'
                    for i, (name, value) in enumerate(zip(call_names, args), 1)))
    catalog = [HarnessTool(name='mcp_' + n, description='test',
                          parameters={'type': 'object'}, executor=lambda *_: pytest.fail('canceled')) for n in names]
    lab = ResearchLab(HarnessContext(decisions={}), LabConfig(plan_yaml=True, plan_review=True, mode='auto'),
                      scientist=ResearchHarness(catalog=catalog),
                      complete_fn=lambda *_a, **_kw: pytest.fail('exact workflow needs no planning model'))
    shown = []
    def review(kind, payload):
        assert kind == 'agenda' and len(payload) == 4
        assert [s.settings for s in lab._plan_doc.steps] == args
        assert 'split_direction' not in lab._plan_doc.steps[0].settings  # retain server default
        assert lab._plan_doc.steps[1].settings['source'] == dot
        assert len(set(payload)) == 4
        shown.extend(payload)
        return {'action': 'cancel'}
    result = lab.run(question, plan_review=review)
    assert shown and result.accepted_steps == 0 and not result.rounds


def test_ambiguous_duplicate_json_arguments_are_not_silently_changed(setup):
    question = setup[0].replace('"max_cost_usd": 0.1', '"max_cost_usd": 0.1, "max_cost_usd": 10.0')
    assert not explicit_workflow(question)


def test_exact_execution_preserves_server_defaults_and_source_without_model_echo(setup):
    seen, advertised, events = [], [], []
    source = 'digraph G {\n a [label="PRPF31 (deletion)"]; a -> b;\n}'
    target = HarnessTool(name='mcp_make_schematic', description='schematic', parameters={
        'type': 'object', 'properties': {'source': {'type': 'string'}}, 'required': ['source']},
        executor=lambda args, _: seen.append(args) or {'status': 'ok', 'artifacts': ['figures/schema.png']})
    other = HarnessTool(name='run_code', description='not approved', parameters={'type': 'object'},
                        executor=lambda *_: pytest.fail('unapproved generic execution'))
    def chat(_messages, tools):
        advertised.extend(t['function']['name'] for t in tools)
        return {'content': '', 'tool_calls': [
            {'id': 'bad', 'type': 'function', 'function': {'name': 'run_code', 'arguments': '{}'}},
            {'id': 'ok', 'type': 'function', 'function': {'name': target.name,
                'arguments': json.dumps({'split_direction': False, 'rerun': True})}},
        ]}
    harness = ResearchHarness(catalog=[target, other], chat_fn=chat)
    result = harness.run('Run the approved schematic.', HarnessContext(decisions={}), events.append,
                         pinned_args={target.name: {'source': source}}, exact_args=True,
                         planned_tools=[target.name])
    assert seen == [{'source': source}]
    assert result.steps[0]['args'] == {'source': source}
    assert 'run_code' not in advertised
    assert result.stop_reason == 'planned_tool_succeeded'
    assert result.errors[0]['error'] == 'tool is outside this explicitly approved step'
    assert any(e['type'] == 'plan_exact_arguments_applied' for e in events)


def test_exact_empty_arguments_stay_empty_and_cancel_still_wins():
    seen = []
    target = HarnessTool(name='mcp_inspect_dataset', description='inspection', parameters={'type': 'object'},
                        executor=lambda args, _: seen.append(args) or {'status': 'ok'})
    def chat(_messages, _tools):
        return {'tool_calls': [{'id': 'x', 'function': {'name': target.name,
                                                     'arguments': '{"rerun":true}'}}]}
    harness = ResearchHarness(catalog=[target], chat_fn=chat)
    kwargs = dict(pinned_args={target.name: {}}, exact_args=True, planned_tools=[target.name])
    harness.run('inspect', HarnessContext(decisions={}), **kwargs)
    assert seen == [{}]
    result = harness.run('inspect', HarnessContext(decisions={}), should_cancel=lambda: True, **kwargs)
    assert seen == [{}] and result.stop_reason == 'cancelled'
