"""Legacy chat clients use native reviewed retrieval without provisioning plain chat."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from aiscientist.agents import request_router as rr
from aiscientist.agents.registry import build_quickchat_catalog
from aiscientist.gateway import app as gw, literature_mcp
from aiscientist.gateway.settings import HPCSettings


def verdict(route="literature", **extra):
    return json.dumps({"route": route, "confidence": 0.95, "reason": "public evidence", **extra})


@pytest.mark.parametrize("question", [
    "不要联网，解释显性遗传", "Don't search for papers; just explain PRPF31",
    "Explain this from memory without looking anything up", "Don't search PubMed; explain the term",
    "Why is cluster 3 high in RHO?", "What explains my patient's PRPF31 variant?",
])
def test_explicit_privacy_and_no_search_intent_override_semantic_router(question):
    route, resolved = rr.route_quickchat_literature(
        lambda *_: pytest.fail("a guarded request must not invoke the classifier"), question)
    assert not route.literature and resolved == question


def test_semantic_router_understands_indirect_non_english_request():
    question = "为什么丢失一份剪接体基因就会让视杆细胞退化？有哪些研究支持？"
    prompts = []
    route, resolved = rr.route_quickchat_literature(
        lambda messages: prompts.append(messages) or verdict(), question)
    assert route.literature and route.source == "model" and resolved == question
    assert question in prompts[0][1]["content"]


def test_history_resolves_a_public_subject_without_switching_source_scope():
    history = [{"role": "user", "content": "Search the RetiGene papers about PRPF31 deletion."}]
    question = "Can you find more evidence for that mechanism?"
    resolved_question = "Find more evidence for PRPF31 deletion in the RetiGene papers only, no PubMed."
    route, resolved = rr.route_quickchat_literature(
        lambda _: verdict(question=resolved_question), question, history=history)
    assert route.literature and resolved == resolved_question
    route, _ = rr.route_quickchat_literature(lambda _: verdict(), "What about the list above?", history=history)
    assert not route.literature


def test_omitting_pubmed_can_preserve_explicit_corpus_retrieval():
    question = "Don't search PubMed; search the RetiGene papers for PRPF31 deletion."
    route, resolved = rr.route_quickchat_literature(lambda _: verdict(), question)
    assert route.literature and resolved == question


def test_native_plain_chat_roster_does_not_construct_old_literature_tools():
    native = {tool.name for tool in build_quickchat_catalog(object(), exclude_literature=True)}
    assert native.isdisjoint({"literature_search", "search_corpus", "deep_literature",
                              "extract_haploinsufficient_genes"})
    assert "literature_search" in {tool.name for tool in build_quickchat_catalog()}


@pytest.fixture
def session(tmp_path, monkeypatch):
    conn = gw.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="test")
    conn.workspace = tmp_path / "runs"
    conn.selected_model = "user-selected-haiku"
    conn.executor = object()
    conn.alloc = None
    conn.pushed = []
    conn.push = lambda event, **_: conn.pushed.append(event)
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setattr(literature_mcp, "registered_server", lambda: object())
    monkeypatch.setattr(gw, "_model_ready", lambda c: True)
    monkeypatch.setattr(gw, "_corpus_curation", lambda *a, **kw: None)
    monkeypatch.setattr(gw, "_literature_corpus_available", lambda c: False)
    monkeypatch.setattr(gw, "_build_literature_executor", lambda *a, **kw: pytest.fail("legacy HPC executor"))
    monkeypatch.setattr(gw, "_stopped_run_target", lambda *a: pytest.fail("chat entered analysis resume router"))
    monkeypatch.setattr(gw, "_followup_target", lambda *a: pytest.fail("chat entered report follow-up router"))
    yield conn
    conn.loop.close()


def dispatch(conn, question):
    req = gw.LabRequest(connection_id=conn.id, conversation_id="chat-test", question=question, route="chat")
    asyncio.run(gw._dispatch_lab(conn, gw._fixed_run_options(req)))


@pytest.mark.parametrize("decision", ["approve", "cancel"])
def test_native_chat_handoff_keeps_selected_model_plan_citations_artifacts_and_cancel(session, monkeypatch, decision):
    calls, searches, bound_models = [], [], []
    def selected(messages, *, role=None):
        calls.append((messages[0]["content"], role))
        system = messages[0]["content"]
        if "Decide whether this chat" in system:
            return verdict()
        if "You write search queries" in system:
            return '{"queries":["PRPF31 deletion"],"terms":[]}'
        return "The supplied paper reports a PRPF31 deletion [1]."
    monkeypatch.setattr(gw, "_lab_llm", lambda c: bound_models.append(c.selected_model) or SimpleNamespace(
        complete_fn=selected, scientist_chat=None, model=c.selected_model))
    def review(conn, emit, step):
        conn.push({"type": "plan_prompt", "agenda": [step]})
        return {"action": decision}
    monkeypatch.setattr(gw, "_literature_plan_review", review)
    def search(tool, args, context):
        searches.append((tool, args))
        return {"status": "ok", "results": [{"title": "Synthetic fixture evidence", "pmid": "123",
            "doi": "10.example/test", "citation": "Fixture study. PMID:123", "abstract": "A PRPF31 deletion was reported."}]}
    async def build(conn, run, run_id, emit):
        run.literature_mcp = SimpleNamespace(run_tool=search)
        run.literature_mcp_context = object()
        return run.literature_mcp
    monkeypatch.setattr(gw, "_build_literature_mcp_route", build)
    monkeypatch.setattr(gw, "_run_quick_chat", lambda *a: pytest.fail("literature handoff stayed plain"))
    dispatch(session, "Find published evidence for PRPF31 deletion")
    assert calls[0][1] == "classify"
    types = [event["type"] for event in session.pushed]
    assert "plan_prompt" in types and types[-1] == "chat_done"
    assert not session.live_runs
    if decision == "cancel":
        assert searches == [] and len(calls) == 1
        assert not list(session.workspace.rglob("summary.md"))
    else:
        assert searches[0][0] == "literature_search"
        assert searches[0][1]["pubmed_only"] is True
        assert any(role == "literature" for _, role in calls)
        answer = next(event["token"] for event in session.pushed if event["type"] == "chat_token")
        assert "[1]" in answer and "**References**" in answer and "Fixture study" in answer
        assert "artifacts" in types and list(session.workspace.rglob("summary.md"))
        assert bound_models == ["user-selected-haiku", "user-selected-haiku"]


def test_plain_chat_does_not_construct_hpc_or_local_corpus_route(session, monkeypatch):
    from aiscientist.gateway import local_corpus
    for name in ("AISCIENTIST_LLM_BASE_URL", "AISCIENTIST_LLM_API_KEY", "AISCIENTIST_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gw, "_lab_llm", lambda c: SimpleNamespace(complete_fn=lambda *a, **kw: verdict("analysis")))
    monkeypatch.setattr(gw, "_bound_credential", lambda c: None)
    monkeypatch.setattr(gw, "_quickchat_context_fns", lambda *a, **kw: (None, None))
    monkeypatch.setattr(gw, "_quickchat_stream_fn", lambda *a, **kw: lambda *a, **kw: iter([("content", "Hello!")]))
    monkeypatch.setattr(local_corpus, "has_local_index", lambda: pytest.fail("plain chat checked local science"))
    monkeypatch.setattr(gw, "_build_literature_mcp_route", lambda *a: pytest.fail("plain chat provisioned MCP"))
    dispatch(session, "你好")
    assert any(event.get("token") == "Hello!" for event in session.pushed)
    assert not list(session.workspace.rglob("summary.md"))
    assert not any(event.get("type") == "plan_prompt" for event in session.pushed)


def test_stop_during_classification_does_not_start_retrieval(session, monkeypatch):
    def selected(*a, **kw):
        session.active_run.chat_stop.set()
        return verdict()
    monkeypatch.setattr(gw, "_lab_llm", lambda c: SimpleNamespace(complete_fn=selected))
    monkeypatch.setattr(gw, "_run_literature", lambda *a, **kw: pytest.fail("retrieval after stop"))
    dispatch(session, "Find papers on PRPF31")
    assert session.pushed[-1]["type"] == "chat_stopped"


def test_routing_never_cold_starts_a_model(session, monkeypatch):
    monkeypatch.setattr(gw, "_model_ready", lambda c: False)
    monkeypatch.setattr(gw, "_lab_llm", lambda *a: pytest.fail("cold model requested"))
    req = gw.LabRequest(connection_id=session.id, question="hello", route="chat")
    route, _ = gw._quickchat_literature_route(session, req)
    assert not route.literature


def test_model_resolution_cannot_override_explicit_source_restrictions():
    route, question = rr.route_quickchat_literature(
        lambda _: verdict(question="Search PubMed for PRPF31 deletion"),
        "Search only the RetiGene papers about that gene; no PubMed.",
        history=[{"role": "user", "content": "PRPF31"}])
    assert not route.literature and "RetiGene" in question


def test_private_history_followup_does_not_become_a_public_query():
    route, _ = rr.route_quickchat_literature(
        lambda *_: pytest.fail("private history must remain in chat"), "What about that gene?",
        history=[{"role": "user", "content": "My patient has a PRPF31 variant in the uploaded VCF."}])
    assert not route.literature
