"""A request with no dataset attached: which shared datasets and uploads it points at, the card's
answers, and the gateway's step that binds the pick before anything is planned."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from aiscientist.gateway import dataset_offers as do


def _f(name, tissue, modality, n=1000, size=2_000_000_000):
    return {"name": name, "size_bytes": size, "facets": {"tissue": tissue, "modality": modality, "n_obs": n}}


HECA = {"slug": "heca", "name": "Human Eye Cell Atlas", "label": "v2026.11.1", "files": [
    _f("HRCA_snRNA_allcells.h5ad", "retina", "snRNA"), _f("HRCA_scRNA_allcells.h5ad", "retina", "scRNA"),
    _f("Iris_snRNA.h5ad", "iris", "snRNA"), _f("ON_snRNA.h5ad", "optic nerve", "snRNA")]}
ONE = {"slug": "retigene", "name": "RetiGene table", "label": "v1", "files": [_f("retigene.h5ad", "", "")]}
OWN = [{"id": 3, "name": "demo_variants.vcf", "path": "/u/demo_variants.vcf", "size_bytes": 454, "uploaded_at": "2026-11-16T10:00:00"},
       {"id": 2, "name": "Ddx41_DEG1.h5ad", "path": "/u/Ddx41_DEG1.h5ad", "size_bytes": 3_000_000_000, "uploaded_at": "2026-11-01T10:00:00"},
       {"id": 1, "name": "Sample1_WT", "path": "/u/Sample1_WT", "size_bytes": 0, "uploaded_at": ""}]


def test_a_plain_question_points_at_nothing():
    for q in ("What does DDX41 do?", "Cluster the cells and find markers", "", "Summarise the last result"):
        assert do.offers(q, [HECA, ONE], OWN) == [], q


def test_shared_datasets_come_first_and_are_narrowed_by_the_request():
    # A tissue and an assay settle one file: one choice, with the reason.
    got = do.offers("Cluster the retina scRNA atlas and compare with my VCF", [HECA, ONE], OWN)
    assert [(o.kind, o.files or o.name) for o in got] == [("shared", ["HRCA_scRNA_allcells.h5ad"]), ("own", "demo_variants.vcf")]
    assert got[0].text.startswith("Shared · Human Eye Cell Atlas v2026.11.1 · HRCA_scRNA_allcells.h5ad")
    assert got[0].version == "v2026.11.1" and got[0].why
    # A tissue alone leaves two files: one choice each.
    two = do.shared_offers("QC of the retina atlas", [HECA])
    assert [o.files for o in two] == [["HRCA_snRNA_allcells.h5ad"], ["HRCA_scRNA_allcells.h5ad"]]
    # The dataset by name, no file narrowed: the whole version (the member card follows).
    whole = do.shared_offers("Explore HECA", [HECA])
    assert len(whole) == 1 and whole[0].files is None and "you choose the file next" in whole[0].text
    assert do.shared_offers("Use the Human Eye Cell Atlas", [HECA])[0].files is None
    # A file named in the request, and a one-file dataset named by its slug.
    assert do.shared_offers("run QC on Iris_snRNA", [HECA])[0].files == ["Iris_snRNA.h5ad"]
    assert do.shared_offers("look up genes in retigene", [ONE])[0].files == ["retigene.h5ad"]
    assert do.shared_offers("what is a retinoblastoma?", [HECA, ONE]) == []
    assert len(do.shared_offers("snRNA", [HECA])) <= do.MAX_SHARED


def test_uploads_by_name_by_type_and_as_my_data():
    assert [o.name for o in do.own_offers("DE on Ddx41_DEG1 mutant vs WT", OWN)] == ["Ddx41_DEG1.h5ad"]
    assert [o.name for o in do.own_offers("annotate the VCF", OWN)] == ["demo_variants.vcf"]
    by_name_first = do.own_offers("compare Sample1_WT with the h5ad", OWN)
    assert [o.name for o in by_name_first] == ["Sample1_WT", "Ddx41_DEG1.h5ad"]
    newest = do.own_offers("Run QC on my dataset", OWN)
    assert [o.name for o in newest] == ["demo_variants.vcf", "Ddx41_DEG1.h5ad", "Sample1_WT"]
    assert do.own_offers("分析我上传的数据", OWN)[0].name == "demo_variants.vcf"
    assert newest[0].text == "Your upload · demo_variants.vcf (0.5 kB, 2026-11-16)" and newest[0].path == "/u/demo_variants.vcf"
    assert do.own_offers("my hypothesis is that DDX41 matters", OWN) == []
    assert len(do.own_offers("my files", OWN * 3)) == do.MAX_OWN


def test_the_cards_answers():
    choices = do.offers("Cluster the retina atlas, or my VCF", [HECA], OWN)
    assert len(choices) == 3
    q = "No dataset is attached to this chat. Which one should this request use?: "
    assert do.answer(q + choices[1].text, choices) is choices[1]
    assert do.answer("2", choices) is choices[1] and do.answer("option 3.", choices) is choices[2]
    assert do.answer("the scRNA one: HRCA_scRNA_allcells", choices) is choices[1]
    assert do.answer("demo_variants", choices) is choices[2]
    for no in (q + do.NONE_OPTION, "none", "No.", "skip", "without a dataset", "4", "不用"):
        assert do.answer(no, choices) is None, no
    for unclear in ("", "hmm", "9", "the atlas"):
        assert do.answer(unclear, choices) is False, unclear


def _conn(replies):
    """A connection whose user answers each card with the next reply (None = never answers)."""
    conn = SimpleNamespace(plan_value=None, pending_plan=None, pushed=[], app_user_id=1)
    conn.plan_event = threading.Event()
    left = list(replies)

    def push(payload):
        conn.pushed.append(payload)
        if payload.get("type") == "plan_clarify" and left:
            reply = left.pop(0)
            if reply is not None:
                conn.plan_value = {"feedback": reply}
                conn.plan_event.set()

    conn.push = push
    return conn


def _run(conn, question, monkeypatch, **req):
    from aiscientist.gateway import app as gw
    saved = []
    monkeypatch.setattr(gw, "_data_offer_sources", lambda _conn: ([HECA], OWN))
    monkeypatch.setattr(gw, "_save_offered_data", lambda _conn, cid, pick: saved.append((cid, pick.kind)))
    monkeypatch.setattr(gw, "_resolve_shared_refs", lambda r, _conn: [
        {"role": "reference", "entries": [{"path": f"/shared/{n}", "name": n} for n in a["files"] or ["all"]]}
        for a in r.shared_datasets])
    real_wait = conn.plan_event.wait
    conn.plan_event.wait = lambda timeout=None: real_wait(0.05)
    request = gw.LabRequest(connection_id="c", question=question, conversation_id="7", **req)
    return asyncio.run(gw._offer_unattached_data(conn, request)), request, saved


def test_the_gateway_binds_the_pick_before_planning(monkeypatch):
    conn = _conn(["1"])
    out, request, saved = _run(conn, "Cluster the retina scRNA atlas", monkeypatch)
    assert out.shared_datasets == [{"shared": "heca", "version": "latest", "files": ["HRCA_scRNA_allcells.h5ad"],
                                    "role": "reference"}]
    assert out._shared_resolved[0]["entries"] == [{"path": "/shared/HRCA_scRNA_allcells.h5ad",
                                                   "name": "HRCA_scRNA_allcells.h5ad"}]
    assert request.shared_datasets is None                      # the request as it came is left alone
    assert saved == [("7", "shared")]
    card = next(p for p in conn.pushed if p["type"] == "plan_clarify")["questions"][0]
    assert card["options"][0].startswith("Shared · ") and card["options"][-1] == do.NONE_OPTION
    assert any(p.get("text", "").startswith("📎 Using Shared · ") for p in conn.pushed)

    conn = _conn(["demo_variants"])
    out, _, saved = _run(conn, "annotate my VCF", monkeypatch)
    assert out.dataset_path == "/u/demo_variants.vcf" and saved == [("7", "own")]
    assert out.datasets == [{"path": "/u/demo_variants.vcf", "name": "demo_variants.vcf", "role": None}]


def test_the_gateway_goes_on_without_when_told_or_unanswered(monkeypatch):
    conn = _conn(["hmm", do.NONE_OPTION])            # an unclear answer puts the card once more
    out, request, saved = _run(conn, "annotate my VCF", monkeypatch)
    assert out is request and saved == []
    assert [p["type"] for p in conn.pushed].count("plan_clarify") == 2
    # Told "without" once, the chat is not asked again in this session.
    again, request2, _ = _run(conn, "annotate my VCF", monkeypatch)
    assert again is request2 and [p["type"] for p in conn.pushed].count("plan_clarify") == 2

    silent = _conn([None])
    out, request, saved = _run(silent, "annotate my VCF", monkeypatch)
    assert out is request and saved == [] and any("in time" in p.get("text", "") for p in silent.pushed)


def test_no_card_when_data_is_attached_or_nothing_matches(monkeypatch):
    for question, extra in (("annotate my VCF", {"dataset_path": "/u/x.vcf"}),
                            ("annotate my VCF", {"shared_datasets": [{"shared": "heca"}]}),
                            ("What does DDX41 do?", {})):
        conn = _conn([])
        out, request, _ = _run(conn, question, monkeypatch, **extra)
        assert out is request and conn.pushed == [], question
