"""The agent routes each request; a general question becomes a one-step literature plan (issue 21).

With no Research / Chat / Corpus scan menu, ``_dispatch_lab`` asks the session's model where a
request goes (``_route_request``). A literature question gets a plan card with one step; approved,
PubMed is searched and the model answers from the abstracts, and the answer is kept with the run
(``_run_literature``). A note on that plan that asks for an analysis hands the request to the lab.
Offline: the model, the search and the user's decisions are scripted.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from aiscientist.agents import literature_answer as lit  # noqa: E402
from aiscientist.agents import request_router as rr  # noqa: E402
from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    yield
    gw_app.CONNECTIONS.clear()


def _conn(tmp_path, **attrs):
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    conn.selected_model = "qwen"
    for k, v in attrs.items():
        setattr(conn, k, v)
    conn.pushed = []
    conn.push = lambda payload, **kw: conn.pushed.append(payload)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


class _Model:
    """The session's model, answering each kind of call by its system prompt."""

    def __init__(self, route='{"route": "literature", "confidence": 0.93, "reason": "general question"}',
                 answer="DDX41 is a DEAD-box helicase [1]; retinal data are scarce [2, 7].",
                 reroute=None, queries='["DDX41 retina", "DDX41 photoreceptor"]'):
        self.route, self.answer, self.reroute, self.queries = route, answer, reroute, queries
        self.calls: list[tuple[str, str | None]] = []
        self.routed: list[str] = []

    def __call__(self, messages, *, role=None):
        system = messages[0]["content"]
        if system == rr.ROUTER_SYSTEM:
            self.calls.append(("route", role))
            self.routed.append(messages[1]["content"])
            if self.reroute is not None and len(self.routed) > 1:
                return self.reroute
            return self.route
        if system == lit.QUERY_SYSTEM:
            self.calls.append(("queries", role))
            return self.queries
        if system == lit.ANSWER_SYSTEM:
            self.calls.append(("answer", role))
            return self.answer
        if system == gw_app._REVISE_LIT_SYSTEM:
            self.calls.append(("revise", role))
            return "What is known about DDX41 in the mouse retina?"
        if system == gw_app._NEW_STUDY_ROUTER_SYS:
            self.calls.append(("screen", role))
            return '{"intent": "study", "confidence": 0.95}'
        raise AssertionError(f"unexpected model call: {system[:80]}")


def _wire(monkeypatch, model, decisions, *, hits=None):
    """Script the model, PubMed and the user's answers to the plan card; record the lab's calls."""
    monkeypatch.setattr(gw_app, "_lab_llm",
                        lambda conn: SimpleNamespace(complete_fn=model, scientist_chat=None, model="qwen-test"))
    searched: list = []

    def search(q, n, **kw):
        searched.append((q, n, kw))
        rows = hits if hits is not None else [
            {"title": f"DDX41 paper {i}", "authors": "Lee A, Kim B", "year": "2025", "journal": "IOVS",
             "doi": f"10.1/d{i}", "pmid": str(40000 + i), "url": f"https://doi.org/10.1/d{i}",
             "citation": f"Lee et al. (2025) DDX41 paper {i}. IOVS. doi:10.1/d{i}", "abstract": "An abstract."}
            for i in range(1, 4)]
        return {"status": "ok", "results": rows}

    monkeypatch.setattr(gw_app._tools_api, "search_literature", search)
    shown: list[str] = []
    script = list(decisions)

    def review(conn, emit, step):
        shown.append(step)
        conn.push({"type": "plan_prompt", "agenda": [step]})
        return script.pop(0)

    monkeypatch.setattr(gw_app, "_literature_plan_review", review)
    lab_calls: list = []

    async def fake_lab(conn, req, **kw):
        lab_calls.append((req.question, kw))

    monkeypatch.setattr(gw_app, "_run_lab", fake_lab)
    return SimpleNamespace(searched=searched, shown=shown, lab=lab_calls)


def _dispatch(conn, question, **kw):
    req = gw_app.LabRequest(connection_id=conn.id, conversation_id="conv-1", question=question,
                            route="auto", **kw)
    asyncio.run(gw_app._dispatch_lab(conn, gw_app._fixed_run_options(req)))


def _types(conn):
    return [p.get("type") for p in conn.pushed]


def test_a_general_question_gets_a_literature_plan_then_a_cited_answer_kept_with_the_run(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    model = _Model()
    w = _wire(monkeypatch, model, [{"action": "approve"}])
    _dispatch(conn, "What is known about DDX41 in the retina?")

    assert w.lab == []                                           # the lab was not used
    assert w.shown == [lit.plan_step("What is known about DDX41 in the retina?")]
    types = _types(conn)
    assert types[0] == "chat_start" and "plan_prompt" in types and types[-1] == "chat_done"
    assert types.index("plan_prompt") < types.index("chat_token") < types.index("artifacts")
    # PubMed only, abstracts included, one search per query
    assert [q for q, _, _ in w.searched] == ["DDX41 retina", "DDX41 photoreceptor"]
    assert all(kw == {"abstracts": True, "pubmed_only": True} for _, _, kw in w.searched)
    # every model call is a short one, the answer included, in its own role
    assert ("route", "classify") in model.calls and ("queries", "classify") in model.calls
    assert ("answer", "literature") in model.calls
    answer = next(p for p in conn.pushed if p.get("type") == "chat_token")["token"]
    assert "DDX41 is a DEAD-box helicase [1]; retinal data are scarce [2]." in answer      # [7] was no paper
    assert "**References**\n\n- [1] Lee et al. (2025) DDX41 paper 1." in answer and "[3] Lee" not in answer
    # kept with the run, listed for download; no run state, so no follow-up mistakes it for a study
    (run_dir,) = list(conn.workspace.iterdir())
    art = run_dir / "artifacts"
    summary = (art / "report" / "summary.md").read_text()
    assert summary.startswith("# Literature search\n\n**Question:** What is known about DDX41 in the retina?")
    assert "Searched PubMed through Europe PMC" in summary
    assert (art / "literature" / "papers.csv").read_text().count("\n") == 4
    assert not (art / "process" / "run_state.json").exists()
    names = {i["name"] for i in next(p for p in conn.pushed if p.get("type") == "artifacts")["items"]}
    assert {"summary.md", "papers.csv"} <= names
    assert gw_app._followup_target(conn, gw_app.LabRequest(connection_id=conn.id, conversation_id="conv-1",
                                                            question="next")) is None


def test_an_analysis_request_goes_to_the_lab(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, _Model(route='{"route": "analysis", "confidence": 0.9, "reason": "QC"}'), [])
    _dispatch(conn, "Run QC on my two samples")
    assert [q for q, _ in w.lab] == ["Run QC on my two samples"] and w.shown == []


def test_a_cancelled_literature_plan_searches_nothing(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, _Model(), [{"action": "cancel"}])
    _dispatch(conn, "Which genes cause Leber congenital amaurosis?")
    assert w.searched == [] and w.lab == []
    assert _types(conn)[-1] == "chat_done"
    assert any("Plan cancelled — nothing was searched." in p.get("text", "") for p in conn.pushed)
    assert not conn.workspace.exists() or not any(conn.workspace.rglob("summary.md"))


def test_a_note_on_the_plan_revises_the_question_and_shows_the_plan_again(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    model = _Model()
    w = _wire(monkeypatch, model, [{"action": "revise", "feedback": "only mouse studies"}, {"action": "approve"}])
    _dispatch(conn, "What is known about DDX41 in the retina?")
    assert len(w.shown) == 2 and "mouse retina" in w.shown[1]
    assert "only mouse studies" in model.routed[1]               # the note was routed again
    assert ("revise", "classify") in model.calls and w.lab == []
    assert _types(conn)[-1] == "chat_done"


def test_a_note_that_asks_for_an_analysis_hands_the_request_to_the_lab(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    model = _Model(reroute='{"route": "analysis", "confidence": 0.9, "reason": "asks to analyse data"}')
    w = _wire(monkeypatch, model, [{"action": "revise", "feedback": "no, analyse my uploaded data for this"}])
    _dispatch(conn, "What is known about DDX41 in the retina?")
    assert w.searched == []
    assert w.lab == [("What is known about DDX41 in the retina?\n\nno, analyse my uploaded data for this", {})]
    assert any("PI plans one instead" in p.get("token", "") for p in conn.pushed if p.get("type") == "chat_token")
    assert not conn.live_runs                                    # both run scopes were closed


def test_no_papers_found_says_so_and_answers_nothing_from_memory(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    model = _Model()
    _wire(monkeypatch, model, [{"action": "approve"}], hits=[])
    _dispatch(conn, "What is known about XYZ123 in the retina?")
    token = next(p for p in conn.pushed if p.get("type") == "chat_token")["token"]
    assert "PubMed returned no papers" in token and ("answer", "literature") not in model.calls


def test_a_followup_question_is_routed_with_the_chats_earlier_analysis(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    conn.last_run_by_conversation["conv-1"] = "run1"
    proc = conn.workspace / "run1" / "artifacts" / "process"
    proc.mkdir(parents=True)
    (proc / "run_state.json").write_text(json.dumps({"question": "QC my two samples", "agenda": ["QC"]}))
    (proc.parent / "report").mkdir()
    (proc.parent / "report" / "summary.md").write_text("done")
    model = _Model()
    _wire(monkeypatch, model, [{"action": "cancel"}])
    _dispatch(conn, "What is known about DDX41 in the retina?")
    assert "Earlier analysis in this chat: “QC my two samples”" in model.routed[0]


def test_a_session_on_an_endpoint_asks_its_model(tmp_path, monkeypatch):
    conn = _conn(tmp_path, alloc=None)
    assert not gw_app._model_ready(conn)
    monkeypatch.setattr(gw_app, "_session_credential", lambda c: {"id": "lab-shared", "builtin": "shared"})
    assert gw_app._model_ready(conn)
    model = _Model()
    _wire(monkeypatch, model, [{"action": "cancel"}])
    _dispatch(conn, "What is known about DDX41 in the retina?")
    assert ("route", "classify") in model.calls


def test_a_cold_gpu_session_routes_by_the_rules(tmp_path, monkeypatch):
    conn = _conn(tmp_path, alloc=None)

    def never(conn):
        raise AssertionError("the model was asked")

    w = _wire(monkeypatch, _Model(), [{"action": "cancel"}])
    monkeypatch.setattr(gw_app, "_lab_llm", never)
    _dispatch(conn, "Summarize the published papers on Müller glia reprogramming")
    assert len(w.shown) == 1                                     # literature, by the rules


def test_the_literature_plan_waits_on_the_plan_card_like_the_labs(tmp_path):
    conn = _conn(tmp_path)
    run = conn.begin_run("conv-1")
    conn.bind_run_id(run, "r1")
    got: dict = {}
    ctx = contextvars.copy_context()
    t = threading.Thread(target=lambda: got.update(ctx.run(gw_app._literature_plan_review, conn,
                                                           lambda *a, **k: None, "**Literature search** — x")))
    t.start()
    deadline = time.time() + 5
    while not any(p.get("type") == "plan_prompt" for p in conn.pushed) and time.time() < deadline:
        time.sleep(0.01)
    prompt = next(p for p in conn.pushed if p.get("type") == "plan_prompt")
    assert prompt["agenda"] == ["**Literature search** — x"] and run.pending_plan["kind"] == "plan"
    run.plan_value = {"action": "approve", "feedback": ""}
    run.plan_event.set()
    t.join(5)
    assert got == {"action": "approve", "feedback": ""} and run.pending_plan is None


# --- the lab's corpus next to PubMed -----------------------------------------------------------------

def _corpus(monkeypatch, out=None, error=None):
    """A session that reaches the corpus: ``search_corpus`` scripted, its calls recorded."""
    calls: list = []

    def executor(args, ctx):
        calls.append(args)
        if error is not None:
            raise error
        return out if out is not None else {"status": "ok", "n_contexts": 2, "terms": [
            {"term": "DDX41", "kind": "gene", "source": "query", "n_passages": 9, "use": "filter"}], "contexts": [
            {"pmid": "777", "chunk": 2, "page": 3, "text": "DDX41 is expressed in photoreceptors.",
             "citation": "Smith J et al. DDX41 in the eye. Exp Eye Res (2024). PMID:777"},
            {"pmid": "40002", "chunk": 0, "page": 1, "text": "DDX41 loss causes cone death.",
             "citation": "Lee et al. DDX41 paper 2. PMID:40002"}]}

    monkeypatch.setattr(gw_app, "_literature_corpus_available", lambda conn: True)
    monkeypatch.setattr(gw_app, "_literature_corpus_tool",
                        lambda conn: (SimpleNamespace(name="search_corpus", executor=executor), SimpleNamespace()))
    return calls


def test_a_session_on_hpc3_also_searches_the_corpus_and_answers_from_both(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    model = _Model(queries='{"queries": ["DDX41 retina"], "terms": ["DDX41", "photoreceptor"]}',
                   answer="DDX41 is in photoreceptors [1]; its loss kills cones [2]; also [3].")
    w = _wire(monkeypatch, model, [{"action": "approve"}])
    calls = _corpus(monkeypatch)
    _dispatch(conn, "What is known about DDX41 in the retina?")

    assert w.shown == [lit.plan_step("What is known about DDX41 in the retina?", corpus=True)]
    assert calls == [{"queries": ["What is known about DDX41 in the retina?"], "top_k": lit.CORPUS_PASSAGES,
                      "terms": ["DDX41", "photoreceptor"]}]
    assert [q for q, _, _ in w.searched] == ["DDX41 retina"]                 # PubMed still searched
    answer = next(p for p in conn.pushed if p.get("type") == "chat_token")["token"]
    # [1] the corpus-only paper, [2] the corpus paper PubMed also found, [3] PubMed only
    assert "- [1] Smith J et al. DDX41 in the eye. Exp Eye Res (2024). PMID:777 (PMID 777, p. 3) https://pubmed.ncbi.nlm.nih.gov/777/" in answer
    assert "- [2] Lee et al. (2025) DDX41 paper 2." in answer and "- [3] Lee et al. (2025) DDX41 paper 1." in answer
    (run_dir,) = list(conn.workspace.iterdir())
    art = run_dir / "artifacts"
    assert "> (p. 3) DDX41 is expressed in photoreceptors." in (art / "literature" / "corpus_passages.md").read_text()
    summary = (art / "report" / "summary.md").read_text()
    assert "2 passage(s) from 2 paper(s)" in summary and "Searched PubMed" in summary
    progress = [p.get("text", "") for p in conn.pushed if p.get("type") == "lab_progress"]
    assert any("from the RetiGene corpus" in t for t in progress)


def test_a_corpus_search_that_fails_leaves_a_pubmed_answer_and_says_why(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, _Model(), [{"action": "approve"}])
    _corpus(monkeypatch, error=RuntimeError("sbatch: queue full"))
    _dispatch(conn, "What is known about DDX41 in the retina?")
    assert [q for q, _, _ in w.searched] == ["DDX41 retina", "DDX41 photoreceptor"]
    warns = [p["text"] for p in conn.pushed if p.get("type") == "lab_progress" and p.get("level") == "warning"]
    assert any("queue full" in t and "PubMed alone" in t for t in warns)
    answer = next(p for p in conn.pushed if p.get("type") == "chat_token")["token"]
    assert "[1] Lee et al. (2025) DDX41 paper 1." in answer


def test_corpus_activity_logs_all_terms_without_the_question(tmp_path):
    conn = _conn(tmp_path)
    question = "This question is displayed elsewhere. " * 30
    terms = [f"long search phrase {i:02d}" for i in range(20)] + ["dark adaptation"]
    calls = []
    tool = SimpleNamespace(executor=lambda args, ctx: calls.append(args) or {"status": "ok", "contexts": []})
    run = SimpleNamespace(monitor=None)
    gw_app._literature_corpus_logged(conn, run, (tool, None), question, terms)
    line = next(p["token"] for p in conn.pushed if p.get("type") == "chat_thinking")
    assert all(term in line for term in terms)
    assert question not in line and "This question" not in line and "queries" not in line
    logged = json.loads(line[line.index("(") + 1:line.rindex(")")])
    assert logged == {"terms": terms, "top_k": lit.CORPUS_PASSAGES}
    assert calls == [{"queries": [question], "terms": terms, "top_k": lit.CORPUS_PASSAGES}]


# --- a question about the RetiGene papers: the corpus alone (issue 46) --------------------------------

def test_a_question_about_the_retigene_papers_searches_the_corpus_and_not_pubmed(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    question = "What do the RetiGene papers say about DDX41 in the retina?"
    model = _Model(queries='{"queries": ["DDX41 retina"], "terms": ["DDX41"]}',
                   answer="DDX41 is in photoreceptors [1]; its loss kills cones [2].")
    w = _wire(monkeypatch, model, [{"action": "approve"}])
    calls = _corpus(monkeypatch)
    _dispatch(conn, question)

    assert w.shown == [lit.plan_step(question, corpus=True, pubmed=False)]
    assert "RetiGene corpus only" in w.shown[0] and "PubMed is not searched" in w.shown[0]
    assert w.searched == []                                            # no PubMed query at all
    assert calls == [{"queries": [question], "top_k": lit.CORPUS_PASSAGES, "terms": ["DDX41"]}]
    answer = next(p for p in conn.pushed if p.get("type") == "chat_token")["token"]
    assert "- [1] Smith J et al. DDX41 in the eye." in answer and "PubMed" not in answer
    (run_dir,) = list(conn.workspace.iterdir())
    summary = (run_dir / "artifacts" / "report" / "summary.md").read_text()
    assert "PubMed was not searched: the question asks about the RetiGene papers." in summary
    assert "Searched PubMed" not in summary
    progress = [p.get("text", "") for p in conn.pushed if p.get("type") == "lab_progress"]
    assert any("2 paper(s), 2 from the RetiGene corpus (2 passage(s)) —" in t for t in progress)


def test_a_note_asking_for_pubmed_adds_it_to_a_corpus_only_plan(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    question = "What do RetiGene papers say about DDX41?"
    w = _wire(monkeypatch, _Model(), [{"action": "feedback", "feedback": "also search PubMed"}, {"action": "cancel"}])
    _corpus(monkeypatch)
    _dispatch(conn, question)
    assert w.shown[0] == lit.plan_step(question, corpus=True, pubmed=False)
    assert w.shown[1] == lit.plan_step("What is known about DDX41 in the mouse retina?", corpus=True, pubmed=True)


def test_a_retigene_question_on_a_session_without_the_corpus_searches_nothing(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, _Model(), [])
    _dispatch(conn, "What are haploinsufficient genes in RetiGene papers?")
    assert w.shown == [] and w.searched == []                          # no plan, no PubMed fallback
    answer = next(p for p in conn.pushed if p.get("type") == "chat_token")["token"]
    assert "cannot reach the RetiGene corpus" in answer
    assert _types(conn)[-1] == "chat_done"


def test_a_gene_list_question_that_names_no_corpus_says_how_to_get_the_whole_corpus(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, _Model(), [{"action": "cancel"}])
    _corpus(monkeypatch)
    _dispatch(conn, "Which genes are haploinsufficient in retinal disease?")
    assert w.shown == [lit.plan_step("Which genes are haploinsufficient in retinal disease?", corpus=True)]
    progress = [p.get("text", "") for p in conn.pushed if p.get("type") == "lab_progress"]
    assert any("ask about the RetiGene papers by name" in t for t in progress)
