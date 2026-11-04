"""A RetiGene list question for a property with no rules built in: the agent writes the curation.

The session's model decides whether a question about the RetiGene papers asks for one fact or for
every gene with a property (``request_router.corpus_ask``; the old list rule stays under it). For a
property the scan has no rules for, the session's model writes a task spec (the tool's
``SPEC_SYSTEM``), the plan card lists it, a note revises it (or a pasted spec replaces it), and the
scan job reads with it (``extract_haploinsufficient_genes``'s ``spec``). Its tables are named after
the spec's class, the spec is kept beside them, and a kept scan is keyed by the spec's digest.
Offline: the model, the job and the user's decisions are scripted.
"""
from __future__ import annotations

import asyncio
import csv
import json
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from aiscientist.agents import request_router as rr  # noqa: E402
from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway import corpus_scan  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402
from aiscientist.tools.extract_haploinsufficient_genes import tool as T  # noqa: E402

QUESTION = "Which genes do the RetiGene papers report as acting through a dominant-negative effect?"
HI_QUESTION = "What are haploinsufficient genes in RetiGene papers?"
KNOWN = corpus_scan.KNOWN_CURATIONS


def _spec(target="Dominant-negative genes in retinal disease", terms=("dominant-negative", "dominant negative")):
    return {
        "target": target,
        "definition": "A gene counts when a sentence states that its mutant protein interferes with the "
                      "wild-type protein and so causes an inherited retinal disease.",
        "counts_when": ["The sentence names the gene and says its mutant acts in a dominant-negative manner."],
        "does_not_count": ["Haploinsufficiency of the gene.", "Gain of function without interference.",
                           "Hedged statements."],
        "classes": ["dominant_negative", "haploinsufficiency", "gain_of_function", "other", "not_relevant"],
        "scope": "Inherited retinal disease in humans.",
        "requests": [f"Mutant protein of this gene acts as a dominant negative in retinal disease, angle {i}."
                     for i in range(16)],
        "terms": list(terms),
    }


# --- the router -------------------------------------------------------------------------------------

def _reply(**kw):
    return lambda messages: json.dumps(kw)


def _floor(q):
    return "haploinsufficiency" if corpus_scan.asks_for_gene_list(q) else None


def test_the_model_names_a_new_property_for_a_list_question():
    ask = rr.corpus_ask(_reply(kind="list", property="dominant-negative mechanism", known=None, confidence=0.9),
                        QUESTION, known=KNOWN, floor=_floor)
    assert ask.is_list and ask.known is None and ask.prop == "dominant-negative mechanism" and ask.source == "model"


def test_the_model_places_a_list_question_in_the_curation_built_in():
    ask = rr.corpus_ask(_reply(kind="list", property="haploinsufficiency", known="haploinsufficiency",
                               confidence=0.9), "List RetiGene genes where one copy is not enough", known=KNOWN,
                        floor=_floor)
    assert ask.is_list and ask.known == "haploinsufficiency"


def test_an_id_that_is_not_a_known_curation_is_a_new_property():
    ask = rr.corpus_ask(_reply(kind="list", property="penetrance", known="penetrance", confidence=0.9),
                        "Which RetiGene genes show incomplete penetrance?", known=KNOWN, floor=_floor)
    assert ask.is_list and ask.known is None


def test_a_fact_question_is_not_a_curation():
    ask = rr.corpus_ask(_reply(kind="fact", property="", known=None, confidence=0.9),
                        "What do the RetiGene papers say about CRX?", known=KNOWN, floor=_floor)
    assert not ask.is_list


def test_a_doubtful_list_is_a_fact():
    ask = rr.corpus_ask(_reply(kind="list", property="x", known=None, confidence=0.4),
                        "Tell me about dominant-negative genes in RetiGene", known=KNOWN, floor=_floor)
    assert not ask.is_list and ask.source == "guard"


def test_the_old_list_rule_is_the_floor_whatever_the_model_says():
    for verdict in (_reply(kind="fact", confidence=0.95), _reply(kind="list", known=None, property="x", confidence=0.9)):
        ask = rr.corpus_ask(verdict, HI_QUESTION, known=KNOWN, floor=_floor)
        assert ask.is_list and ask.known == "haploinsufficiency" and ask.source == "guard"


def test_without_a_usable_model_the_rule_decides():
    def broken(messages):
        raise RuntimeError("down")
    for complete in (None, broken, lambda m: "no json here"):
        assert rr.corpus_ask(complete, HI_QUESTION, known=KNOWN, floor=_floor).known == "haploinsufficiency"
        assert not rr.corpus_ask(complete, QUESTION, known=KNOWN, floor=_floor).is_list


def test_the_known_curations_are_listed_in_the_prompt():
    system = rr.corpus_ask_messages(QUESTION, KNOWN)[0]["content"]
    assert '"haploinsufficiency":' in system and "{known}" not in system and '{"kind"' in system


# --- the console route ------------------------------------------------------------------------------

class _Model:
    """The session's model: says list or fact, writes the spec, revises it."""

    def __init__(self, verdict=None, drafts=None):
        self.verdict = verdict or {"kind": "list", "property": "dominant-negative mechanism", "known": None,
                                   "confidence": 0.9}
        self.drafts = list(drafts if drafts is not None else [json.dumps(_spec())])
        self.calls: list[tuple[str, str, str]] = []      # (role, what, user message)

    def __call__(self, messages, *, role=None):
        system, user = messages[0]["content"], messages[-1]["content"]
        if system == gw_app._NEW_STUDY_ROUTER_SYS:
            return '{"intent": "study", "confidence": 0.95}'
        if system.startswith(rr.CORPUS_ASK_SYSTEM[:60]):
            self.calls.append((role, "ask", user))
            return json.dumps(self.verdict)
        if system == T.SPEC_SYSTEM:
            self.calls.append((role, "spec", user))
            return self.drafts.pop(0)
        raise AssertionError(f"unexpected model call: {system[:80]}")


class _Job:
    """The scan's HPC3 job: writes a dominant-negative scan's tables where the tool would, for the spec
    it was given."""

    def __init__(self, workspace: Path):
        self.workspace, self.args, self.notify = workspace, [], None

    def run_tool(self, tool, args, ctx):
        self.args.append(args)
        spec = T.parse_spec(args["spec"])
        out = self.workspace / "artifacts" / T.spec_folder(spec)
        out.mkdir(parents=True, exist_ok=True)
        cols = ("pmid", "page", "gene", "gene_as_written", "gene_status", "gene_in_quote", "mechanism_class",
                "explicit", "negated", "human_retinal", "phenotype", "mechanism_statement", "quote",
                "quote_verified", "cite_like", "dominance_marker", "garbled", "sentence_id", "found_by", "eligible")
        rows = [
            ("101", 3, "RHO", "dominant_negative", True, False, "Mutant RHO acts as a dominant negative in adRP."),
            ("102", 5, "RHO", "dominant_negative", True, False, "P23H RHO exerts a dominant-negative effect."),
            ("103", 2, "CRX", "dominant_negative", False, False, "CRX frameshift alleles may act as dominant negatives."),
            ("104", 4, "PRPF31", "haploinsufficiency", True, False, "PRPF31 causes RP through haploinsufficiency."),
        ]
        with (out / "hits.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for pmid, page, gene, cls, explicit, negated, quote in rows:
                w.writerow({"pmid": pmid, "page": page, "gene": gene, "gene_as_written": gene, "gene_status": "ok",
                            "gene_in_quote": True, "mechanism_class": cls, "explicit": explicit,
                            "negated": negated, "human_retinal": True, "phenotype": "RP",
                            "mechanism_statement": cls, "quote": quote, "quote_verified": True, "cite_like": False,
                            "dominance_marker": True, "garbled": False, "sentence_id": f"{pmid}:{page}",
                            "found_by": "T", "eligible": cls == "dominant_negative" and explicit})
        (out / "genes.csv").write_text("gene,n_papers,pmid_1,quote_1\nRHO,2,101,x\n")
        (out / "requests.csv").write_text("id,round,gene,request,n_passages\nR1,1,,x,5\n")
        return {"status": "ok", "n_papers": 1739, "n_passages": 1550, "n_requests": 16, "n_gene_requests": 1,
                "n_hits_eligible": 2, "n_genes": 1, "genes": [], "warnings": [], "definition": spec["definition"],
                "unread_check": {"on": True, "found": 3, "read": 3, "left": 0},
                "spec": {"target": spec["target"], "classes": spec["classes"], "folder": T.spec_folder(spec)}}


def _conn(tmp_path):
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    conn.selected_model = "qwen"
    conn.pushed = []
    conn.push = lambda payload, **kw: conn.pushed.append(payload)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _wire(monkeypatch, decisions, model):
    monkeypatch.setattr(gw_app, "_lab_llm",
                        lambda conn: types.SimpleNamespace(complete_fn=model, scientist_chat=None, model="claude-haiku-5-5"))
    monkeypatch.setattr(gw_app, "_literature_corpus_available", lambda c: True)
    monkeypatch.setattr(gw_app, "_literature_corpus_on_hpc", lambda c: True)
    monkeypatch.setattr(gw_app, "_corpus_papers", lambda c: {})
    jobs: list[_Job] = []

    def build(c, run_id=None, own_gpu=False):
        jobs.append(_Job(c.workspace / run_id))
        return jobs[-1]

    monkeypatch.setattr(gw_app, "_build_literature_executor", build)
    shown: list[str] = []
    script = list(decisions)

    def review(conn, emit, step, stage="literature"):
        shown.append(step)
        return script.pop(0)

    monkeypatch.setattr(gw_app, "_literature_plan_review", review)
    return types.SimpleNamespace(jobs=jobs, shown=shown)


def _dispatch(conn, question=QUESTION):
    req = gw_app.LabRequest(connection_id=conn.id, conversation_id="conv-1", question=question, route="auto")
    asyncio.run(gw_app._dispatch_lab(conn, gw_app._fixed_run_options(req)))


def _texts(conn):
    return [str(p.get("text") or p.get("token") or "") for p in conn.pushed]


def test_a_new_property_gets_a_drafted_spec_on_the_plan_and_the_scan_reads_with_it(tmp_path, monkeypatch):
    conn, model = _conn(tmp_path), _Model()
    w = _wire(monkeypatch, [{"action": "approve"}], model)
    _dispatch(conn)

    # The model said list (classify), then wrote the spec from the question alone (plan).
    assert [(r, what) for r, what, _ in model.calls] == [("classify", "ask"), ("plan", "spec")]
    assert model.calls[1][2] == QUESTION
    (step,) = w.shown
    assert step.startswith("**RetiGene curation**") and "No rules for this are built in" in step
    assert "claude-haiku-5-5 wrote a task spec" in step and "`dominant-negative`" in step
    assert "`dominant_negative` (listed)" in step and "paste the whole spec as JSON" in step
    # The job read with that spec, exactly as the tool checks it.
    ((args,),) = [j.args for j in w.jobs]
    assert args == {"spec": T.parse_spec(_spec())}

    (run_dir,) = [p for p in conn.workspace.iterdir() if p.is_dir()]
    cur = run_dir / "artifacts" / "curation"
    with (cur / "dominant_negative_genes.csv").open() as fh:
        genes = {r["gene"]: r["claim"] for r in csv.DictReader(fh)}
    assert genes == {"RHO": "stated", "CRX": "suggested"}              # PRPF31's class is not the target
    kept = json.loads((cur / gw_app.SPEC_FILE).read_text())
    assert kept["written_by"] == "claude-haiku-5-5" and kept["spec"]["target"] == _spec()["target"]
    # The run's record (Runs, "Save tables to my data") names the curation and saves the spec too.
    rec = json.loads((run_dir / corpus_scan.CURATION_POINTER).read_text())
    assert rec["curation"] == _spec()["target"] and rec["n_genes"] == 1
    assert rec["counts"]["stated"] == 1 and "haploinsufficient" not in rec["counts"]
    assert [f["name"] for f in rec["files"]] == ["dominant_negative_genes.csv", "dominant_negative_evidence.csv",
                                                 gw_app.SPEC_FILE]
    answer = next(t for t in _texts(conn) if t.startswith("**“Dominant-negative"))
    assert "in the RetiGene corpus: 1 gene**" in answer and "claude-haiku-5-5 wrote" in answer
    assert "| RHO |" in answer and "dominant_negative_genes.csv" in answer
    assert "Chunks with a search term that no round had read: 3, read now: 3" in answer


def test_a_note_revises_the_spec_and_the_plan_is_shown_again(tmp_path, monkeypatch):
    revised = _spec(target="Dominant-negative genes in retinitis pigmentosa")
    model = _Model(drafts=[json.dumps(_spec()), json.dumps(revised)])
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "only count retinitis pigmentosa"},
                            {"action": "approve"}], model)
    _dispatch(conn)
    assert [what for _, what, _ in model.calls] == ["ask", "spec", "ask", "spec"]
    revise_msg = model.calls[3][2]
    assert "The current spec:" in revise_msg and "only count retinitis pigmentosa" in revise_msg
    assert len(w.shown) == 2 and "retinitis pigmentosa”" in w.shown[1]
    assert w.jobs[0].args == [{"spec": T.parse_spec(revised)}]


def test_a_pasted_spec_is_used_as_written_and_a_broken_one_changes_nothing(tmp_path, monkeypatch):
    mine = _spec(target="My own dominant-negative list")
    model = _Model()
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": '{"definition": "too short"}'},
                            {"action": "feedback", "feedback": json.dumps(mine)},
                            {"action": "approve"}], model)
    _dispatch(conn)
    assert [what for _, what, _ in model.calls] == ["ask", "spec"]     # no model call for a pasted spec
    assert any("That spec cannot drive the scan" in t for t in _texts(conn))
    assert w.shown[1] == w.shown[0] and "you wrote a task spec" in w.shown[2]
    assert w.jobs[0].args == [{"spec": T.parse_spec(mine)}]


def test_without_a_usable_spec_the_question_goes_to_the_literature_line(tmp_path, monkeypatch):
    model = _Model(drafts=["not a spec", '{"target": "x"}'])
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [], model)
    routed: list[str] = []

    def route(c, req, prior=""):
        routed.append(req.question)
        return rr.Route(rr.LITERATURE, 0.9, "papers", "model")

    async def literature(c, req, r=None, **kw):
        routed.append("literature")

    monkeypatch.setattr(gw_app, "_route_request", route)
    monkeypatch.setattr(gw_app, "_run_literature", literature)
    _dispatch(conn)
    assert [what for _, what, _ in model.calls] == ["ask", "spec", "spec"]    # one retry with the reason
    assert w.shown == [] and w.jobs == [] and routed == [QUESTION, "literature"]
    assert any("No task spec for a curation could be written" in t for t in _texts(conn))


def test_a_note_on_the_built_in_plan_makes_a_spec_of_its_own(tmp_path, monkeypatch):
    model = _Model(verdict={"kind": "list", "property": "haploinsufficiency", "known": "haploinsufficiency",
                            "confidence": 0.9},
                   drafts=[json.dumps(_spec(target="Haploinsufficient genes in RP only"))])
    conn = _conn(tmp_path)
    w = _wire(monkeypatch, [{"action": "feedback", "feedback": "count retinitis pigmentosa only"},
                            {"action": "approve"}], model)
    _dispatch(conn, HI_QUESTION)
    assert "No rules for this are built in" not in w.shown[0]               # the built-in rules first
    assert model.calls[-1][1] == "spec" and model.calls[-1][2].startswith(HI_QUESTION)
    assert "The user added: count retinitis pigmentosa only" in model.calls[-1][2]
    assert "Haploinsufficient genes in RP only" in w.shown[1] and len(w.jobs[0].args) == 1


def test_a_fact_about_the_retigene_papers_is_no_curation(monkeypatch):
    conn = types.SimpleNamespace()
    monkeypatch.setattr(gw_app, "_literature_corpus_on_hpc", lambda c: True)
    monkeypatch.setattr(gw_app, "_session_llm", lambda c, role: (_reply(kind="fact", confidence=0.9), "m"))
    assert gw_app._corpus_curation(conn, "What do the RetiGene papers say about CRX?") is None
    monkeypatch.setattr(gw_app, "_session_llm", lambda c, role: (
        _reply(kind="list", property="dominant negative", known=None, confidence=0.9), "m"))
    ask = gw_app._corpus_curation(conn, QUESTION)
    assert ask.is_list and ask.known is None


# --- kept scans ---------------------------------------------------------------------------------------

def test_a_kept_scan_is_keyed_by_the_spec(tmp_path, monkeypatch):
    conn = types.SimpleNamespace(executor=object(), mock=False)
    monkeypatch.setenv("AISCIENTIST_PAPERQA_ON_HPC", "1")
    monkeypatch.setenv("AISCIENTIST_PAPERQA_PAPERS", "/dfs/retigene/papers")
    monkeypatch.setattr(corpus_scan, "corpus_fingerprint", lambda *a, **k: {"root": "/dfs/retigene"})
    monkeypatch.setattr(gw_app, "_corpus_scan_model", lambda c: "claude-haiku-5-5")
    spec = T.parse_spec(_spec())
    built_in = gw_app._corpus_scan_identity(conn)
    one, again = gw_app._corpus_scan_identity(conn, spec), gw_app._corpus_scan_identity(conn, dict(spec))
    other = gw_app._corpus_scan_identity(conn, T.parse_spec(_spec(target="Something else")))
    assert built_in[1]["rules"] == T.rules_digest() and one[1]["rules"] == T.rules_digest(spec)
    assert len({built_in[0], one[0], other[0]}) == 3 and one[0] == again[0]


def test_a_spec_scan_is_stored_and_read_back_from_its_own_folder(tmp_path, monkeypatch):
    from aiscientist.gateway import dataset_storage
    monkeypatch.setattr(dataset_storage, "data_root", lambda: tmp_path / "data")
    spec = T.parse_spec(_spec())
    folder = T.spec_folder(spec)
    tables = tmp_path / "run" / folder
    tables.mkdir(parents=True)
    for name in corpus_scan.TABLES:
        (tables / name).write_text("gene\nRHO\n")
    corpus_scan.store("k-spec", {"status": "ok"}, tables, {"spec": spec}, folder)
    kept = corpus_scan.load("k-spec")
    assert kept is not None and kept.subdir == folder and (kept.tables / "genes.csv").is_file()
    corpus_scan.copy_into(kept, tmp_path / "art")
    assert (tmp_path / "art" / folder / "hits.csv").is_file()
    # A scan kept before folders were recorded is the built-in rules' folder.
    corpus_scan.store("k-old", {"status": "ok"}, tables, {})
    assert corpus_scan.load("k-old").subdir == corpus_scan.OUT_SUBDIR


def test_a_curation_from_a_spec_names_its_tables_after_the_target_class():
    spec = T.parse_spec(_spec())
    cur = gw_app._spec_curation(spec)
    assert (cur.target, cur.stated, cur.genes_file, cur.subdir) == (
        "dominant_negative", "stated", "dominant_negative_genes.csv", T.spec_folder(spec))
    assert cur.conflicting == ("haploinsufficiency", "gain_of_function") and not cur.built_in
    assert corpus_scan.HAPLOINSUFFICIENCY.genes_file == corpus_scan.CURATION_GENES
