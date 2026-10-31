"""The corpus scan driven by a task spec (another property than haploinsufficiency), offline: a small
text corpus padded with filler chunks so that one chunk is under the 1% a usable term may fill, a
fake model, a toy embedder. What the tests pin down: a spec's requests and terms are used as given
(no model call writes them), the reading prompt is built from the spec, only the spec's target
class is listed, its terms are read in full by default, and chunks that hold a usable term but no
round read are read by the check (or counted and reported when the check is off). The built-in
rules keep their own digest; each spec gets its own."""
from __future__ import annotations

import csv
import json
import re
import types
from pathlib import Path

import numpy as np
import pytest

from aiscientist.tools.extract_haploinsufficient_genes import tool as hi

DN_SENT = ("The truncated CRX protein acts through a dominant-negative effect, interfering with the "
           "wild-type allele in cone-rod dystrophy.")
LOF_SENT = ("Heterozygous null alleles of PRPF31 cause retinitis pigmentosa because one functional copy "
            "is not enough.")
NEAR_SENT = ("A dominant-negative effect of the RHO P23H allele explains the retinitis pigmentosa in this "
             "family.")
HEDGED_SENT = "The OTX2 variant may act through a dominant-negative mechanism in this patient."

PAPERS = {
    "10000001": ["Cone-rod dystrophy is genetically heterogeneous.", DN_SENT,
                 "Patients had reduced cone responses."],
    "10000002": ["Retinitis pigmentosa families were studied.", NEAR_SENT, HEDGED_SENT, LOF_SENT],
    "10000003": ["Splicing factor genes are expressed in the retina.",
                 "The retina is the tissue most sensitive to their loss."],
}
N_FILLER = 200

SPEC = {
    "target": "dominant-negative genes in retinal disease",
    "definition": "A gene counts when a sentence states that its mutant product interferes with the "
                  "wild-type product (a dominant-negative effect) and so causes a retinal disease.",
    "counts_when": ["The sentence names the gene and states a dominant-negative effect for it."],
    "does_not_count": ["Haploinsufficiency or loss of one copy.", "Hedged statements (may, might)."],
    "classes": ["Dominant-Negative", "haploinsufficiency", "other"],
    "scope": "Human retinal disease, from any kind of evidence.",
    "requests": ["Cone-rod dystrophy patients carry a truncating allele that interferes with the normal protein.",
                 "The mutant protein interferes with the wild-type protein in the retina.",
                 "A dominant-negative effect explains the dominant retinal dystrophy in these families.",
                 "Truncated proteins that act through a dominant-negative mechanism cause disease."],
    "terms": ["dominant-negative", "interferes with the wild-type"],
}


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "retigene"
    cache = root / "mechanism_cache"
    cache.mkdir(parents=True)
    papers = dict(PAPERS)
    for k in range(N_FILLER):
        papers[f"9{k:07d}"] = [f"Filler passage {k} describes the layered anatomy of the vertebrate eye.",
                               "It names no gene and states no mechanism."]
    with (cache / "sentences.jsonl").open("w") as sf, (cache / "chunks.jsonl").open("w") as cf:
        for pmid, sents in papers.items():
            cf.write(json.dumps({"pmid": pmid, "chunk": 0, "page": 1, "text": " ".join(sents)}) + "\n")
            for k, s in enumerate(sents):
                sf.write(json.dumps({"sid": f"{pmid}:0:{k}", "pmid": pmid, "chunk": 0, "sent": k, "page": 1,
                                     "text": s, "prev": sents[k - 1] if k else "",
                                     "next": sents[k + 1] if k + 1 < len(sents) else ""}) + "\n")
    (root / "papers").mkdir()
    with (root / "paperqa_manifest.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file_location", "title", "doc_id"])
        for pmid in papers:
            w.writerow([f"{pmid}.pdf", f"Paper {pmid}", pmid])
    with (root / "hgnc_complete_set.txt").open("w") as fh:
        fh.write("hgnc_id\tsymbol\talias_symbol\tprev_symbol\n")
        for i, g in enumerate(("CRX", "PRPF31", "RHO", "OTX2")):
            fh.write(f"HGNC:{i + 1}\t{g}\t\t\n")
    return root


def _embed(texts):
    """A toy embedder with three axes: cone-rod dystrophy (the spec's first request and paper
    10000001), the filler (where the spec's other requests land) and everything else (the other
    papers, which only the spec's terms reach)."""
    later = set(SPEC["requests"][1:])

    def axis(t: str) -> list[float]:
        tl = t.lower()
        if "cone-rod" in tl:
            return [1.0, 0.0, 0.0]
        if "filler" in tl or t in later:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]
    return np.array([axis(t) for t in texts], dtype="float32")


# The reading a careful model would give each mechanism sentence, in the spec's classes.
ANSWERS = {
    DN_SENT: {"gene": "CRX", "class": "dominant_negative", "explicit": True, "negated": False, "in_scope": True,
              "phenotype": "cone-rod dystrophy"},
    NEAR_SENT: {"gene": "RHO", "class": "dominant_negative", "explicit": True, "negated": False, "in_scope": True,
                "phenotype": "retinitis pigmentosa"},
    HEDGED_SENT: {"gene": "OTX2", "class": "dominant_negative", "explicit": False, "negated": False,
                  "in_scope": True, "phenotype": None},
    LOF_SENT: {"gene": "PRPF31", "class": "haploinsufficiency", "explicit": True, "negated": False,
               "in_scope": True, "phenotype": "retinitis pigmentosa"},
}


def _model(spec: dict, seen: list[str]):
    judge = hi.spec_judge_system(spec)
    genes_sys = hi.spec_gene_requests_system(spec)

    def model(messages):
        system = messages[0]["content"]
        if system == genes_sys:
            seen.append("round2")
            genes = [line.split(" — ")[0] for line in messages[1]["content"].splitlines()]
            return json.dumps({"requests": {g: f"{g} acts through a dominant-negative effect." for g in genes}})
        assert system == judge, "a spec scan asked the model for something the spec already gives"
        user = messages[1]["content"]
        sents = [m.group(1) for m in re.finditer(r"^\[\d+\] (.*)$", user, re.M)]
        seen.append(re.search(r"PMID (\d+)", user).group(1))
        return json.dumps({"items": [{**ANSWERS[s], "sentence": k + 1, "quote": s}
                                     for k, s in enumerate(sents) if s in ANSWERS]})
    return model


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    monkeypatch.setenv("AISCIENTIST_PAPERQA_PAPERS", str(root / "papers"))
    monkeypatch.setenv("AISCIENTIST_PAPERQA_MANIFEST", str(root / "paperqa_manifest.csv"))
    ws = tmp_path / "run"
    ws.mkdir()
    return types.SimpleNamespace(model="m", llm_base_url="http://unused:1/v1", tunnel_port=None, workspace=ws)


def _run(ctx, args, seen):
    spec = hi.parse_spec(SPEC)
    res = hi.run_extract({"spec": SPEC, **args}, ctx, judge_fn=_model(spec, seen), embed_fn=_embed,
                         log=lambda s: None)
    art = Path(ctx.workspace) / "artifacts" / hi.spec_folder(spec)
    return res, art


def _rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_a_spec_scan_uses_the_spec_and_lists_only_its_target_class(ctx):
    seen: list[str] = []
    res, art = _run(ctx, {"k_per_request": 1}, seen)
    assert res["status"] == "ok", res
    genes = {r["gene"]: r for r in _rows(art / "genes.csv")}
    # CRX and RHO are stated dominant-negative explicitly; OTX2 only hedged; PRPF31 is the near miss.
    assert set(genes) == {"CRX", "RHO"}
    assert genes["CRX"]["mechanism_statement"].lower() in ("dominant-negative", "interferes with the wild-type")
    hits = _rows(art / "hits.csv")
    assert any(h["gene"] == "PRPF31" and h["mechanism_class"] == "haploinsufficiency" and h["eligible"] == "False"
               for h in hits)                                  # read (a term reached its paper), not listed
    assert any(h["gene"] == "OTX2" and h["explicit"] == "False" and h["eligible"] == "False" for h in hits)
    requests = _rows(art / "requests.csv")
    assert [r["request"] for r in requests if r["round"] == "1" and r["id"].startswith("R")] == SPEC["requests"]
    assert not any(r["id"] == "K" for r in requests)        # no built-in dosage wording for a spec
    assert res["definition"] == SPEC["definition"]
    assert res["spec"]["classes"][0] == "dominant_negative"
    assert res["spec"]["rules"] == hi.rules_digest(hi.parse_spec(SPEC))


def test_a_specs_terms_are_read_in_full_by_default(ctx):
    seen: list[str] = []
    res, _ = _run(ctx, {"k_per_request": 1, "k_per_gene": 0}, seen)
    used = {t["term"]: t["use"] for t in res["terms"]}
    assert used["dominant-negative"] == "filter"            # 2 chunks of 203: under 1%, read in full
    assert {"10000001", "10000002"} <= set(seen)
    assert res["unread_check"] == {"on": True, "found": 0, "read": 0, "left": 0}


def test_the_check_reads_term_chunks_a_cap_left_out(ctx):
    seen: list[str] = []
    res, art = _run(ctx, {"k_per_request": 1, "k_per_gene": 0, "max_term_passages": 0}, seen)
    assert {t["use"] for t in res["terms"] if t["term"] == "dominant-negative"} == {"over_cap"}
    assert "10000002" in seen                                # read by the check, not by round one
    assert res["unread_check"]["read"] >= 1 and res["unread_check"]["left"] == 0
    assert any(r["id"] == "U" for r in _rows(art / "requests.csv"))
    assert "RHO" in {r["gene"] for r in _rows(art / "genes.csv")}


def test_with_the_check_off_the_unread_chunks_are_counted_and_reported(ctx):
    seen: list[str] = []
    res, art = _run(ctx, {"k_per_request": 1, "k_per_gene": 0, "max_term_passages": 0, "check_unread": 0}, seen)
    assert "10000002" not in seen
    assert res["unread_check"]["on"] is False and res["unread_check"]["left"] >= 1
    assert any("not read" in w and "check_unread is off" in w for w in res["warnings"])
    assert "RHO" not in {r["gene"] for r in _rows(art / "genes.csv")}


def test_a_spec_that_cannot_drive_a_scan_is_refused_before_any_work(ctx):
    seen: list[str] = []
    res = hi.run_extract({"spec": {**SPEC, "requests": ["too short"]}}, ctx, judge_fn=_model(hi.parse_spec(SPEC), seen),
                         embed_fn=_embed, log=lambda s: None)
    assert res["status"] == "error" and "usable search requests" in res["error"] and seen == []


def test_parse_spec_normalises_and_rejects():
    s = hi.parse_spec(json.dumps(SPEC))                       # a model reply holding the JSON
    assert s["classes"] == ["dominant_negative", "haploinsufficiency", "other", "not_relevant"]
    for broken, why in (({k: v for k, v in SPEC.items() if k != "scope"}, "no scope"),
                        ({**SPEC, "definition": "  "}, "definition is empty"),
                        ({**SPEC, "terms": "dominant-negative"}, "not a list"),
                        ({**SPEC, "counts_when": []}, "rules")):
        with pytest.raises(ValueError, match=why):
            hi.parse_spec(broken)
    with pytest.raises(ValueError):
        hi.parse_spec("no json here")


def test_each_spec_has_its_own_digest_and_the_built_in_rules_keep_theirs():
    a = hi.parse_spec(SPEC)
    b = hi.parse_spec({**SPEC, "scope": "Any disease."})
    assert hi.rules_digest() == hi.rules_digest(None)
    assert len({hi.rules_digest(), hi.rules_digest(a), hi.rules_digest(b)}) == 3
    assert hi.spec_folder(a) != hi.spec_folder(b) and hi.spec_folder(a).startswith("curation_")


def test_the_schema_offers_the_spec_and_the_check():
    props = hi._schema()["properties"]
    assert set(props["spec"]["required"]) == set(hi.SPEC_FIELDS)
    assert "default" not in props["check_unread"] and props["max_unread_passages"]["default"] == 600
