"""Free clinical text -> HPO mapping: the LLM does language, the ontology owns identity.

The load-bearing property under test is that a wrong-but-well-formed HPO ID cannot get through — from
the mapper OR from a model that skips it and types IDs into run_lirical. Everything here runs offline:
the LLM is a scripted ``chat_fn``, the ontology is the bundled lexicon.
"""

from __future__ import annotations

import json

import pytest

from bioagent.tools.hpo_terms.index import get_index
from bioagent.tools.hpo_terms.mapper import (
    make_hpo_mapping_tool,
    map_phrase,
    map_text_to_hpo,
    validate_hpo_ids,
)
from bioagent.tools.phenotype_dx import run_lirical


def _chat(extract: list[dict], choices: "list[int] | None" = None):
    """A scripted two-stage LLM: the first call is the EXTRACT turn, later calls are SELECT turns
    (answered from ``choices`` in order, defaulting to candidate 1)."""
    picks = list(choices or [])
    calls: list[list[dict]] = []

    def chat_fn(messages: list[dict]) -> str:
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps(extract)
        pick = picks.pop(0) if picks else 1
        return json.dumps({"choice": pick, "reason": "scripted"})

    chat_fn.calls = calls        # type: ignore[attr-defined]
    return chat_fn


# --- the index: the closed set --------------------------------------------------------------------


def test_index_validate_separates_real_obsolete_and_invented_ids():
    ix = get_index()
    assert ix.validate("HP:0000510") == {"hpo_id": "HP:0000510", "status": "ok",
                                         "name": "Rod-cone dystrophy"}
    # an ID that does not exist is caught — this is the fabrication that would otherwise reach LIRICAL
    assert ix.validate("HP:9999999")["status"] == "unknown"
    assert ix.validate("retinitis pigmentosa")["status"] == "malformed"
    obsolete = ix.validate("HP:0000057")
    assert obsolete["status"] == "obsolete" and obsolete["replaced_by"] == "HP:0008665"
    assert ix.version.startswith("http")          # every mapping can name its ontology release


def test_index_search_ranks_the_exact_term_first_and_finds_synonyms():
    ix = get_index()
    assert ix.search("night blindness")[0].term.id == "HP:0000662"          # label
    assert ix.search("retinitis pigmentosa")[0].term.id == "HP:0000510"     # exact SYNONYM of Rod-cone
    assert ix.search("cone dystrophies")[0].term.id == "HP:0008020"         # de-pluralized
    assert ix.search("no phenotype words at all") == [] or \
        ix.search("no phenotype words at all")[0].score < 0.8
    # non-eye terms are present too — syndromic IRD needs them (Usher: hearing loss, BBS: polydactyly)
    assert ix.search("polydactyly")[0].term.id == "HP:0010442"


# --- mapping a phrase -----------------------------------------------------------------------------


def test_exact_ontology_name_wins_over_the_coarser_curated_alias():
    # the curated IRD table maps "cone dystrophy" to HP:0000548 (Cone/cone-rod dystrophy); HPO names
    # HP:0008020 (Cone dystrophy) exactly, and the precise ontology term must win
    term = map_phrase("cone dystrophies")
    assert (term.hpo_id, term.method) == ("HP:0008020", "ontology_exact")


def test_curated_alias_covers_disease_shorthand_the_ontology_has_no_name_for():
    # "Stargardt" is a disease, not an HPO term — lexical search alone finds nothing
    assert get_index().search("Stargardt") == []
    term = map_phrase("Stargardt disease")
    assert (term.hpo_id, term.name, term.method) == ("HP:0007754", "Macular dystrophy", "curated_alias")


def test_unmatchable_phrase_maps_to_nothing_rather_than_a_guess():
    assert map_phrase("the weather is nice today") is None


@pytest.mark.parametrize("phrase,expected,label", [
    # The alias table matches a KEYWORD anywhere in the phrase, so "central vision loss" used to hit its
    # generic `vision loss` keyword and return HP:0000505 Visual impairment — pre-empting the ontology's
    # own, strictly more specific terms. LIRICAL's LR rewards specificity, so that one hijack moved a
    # posterior 8x (94.78% -> 12.36%) on an identical case. Measured 2026-07-15.
    ("central vision loss", "HP:0000572", "Visual loss, not the generic Visual impairment"),
    ("progressive central vision loss", "HP:0000529", "Progressive visual loss — more specific still"),
    ("reduced visual acuity", "HP:0007663", "HPO names this exactly"),
    ("blindness", "HP:0000618", "HPO names this exactly"),
    ("scotoma", "HP:0000575", "Scotoma, not the parent Visual field defect"),
    ("macular degeneration", "HP:0000608", "degeneration is not dystrophy"),
    # ...and one where the table was clinically WRONG: raised IOP is ocular hypertension. It is NOT
    # glaucoma (which needs optic-nerve damage) — the table asserted a diagnosis the phrase never made.
    ("raised intraocular pressure", "HP:0007906", "Ocular hypertension, NOT Glaucoma"),
])
def test_the_curated_alias_never_out_votes_a_good_ontology_hit(phrase, expected, label):
    term = map_phrase(phrase)
    assert term is not None, label
    assert term.hpo_id == expected, f"{label}: got {term.hpo_id} {term.name} via {term.method}"


@pytest.mark.parametrize("phrase,expected", [
    ("Stargardt", "HP:0007754"),                 # a disease HPO does not name
    ("BBS", "HP:0000556"),                       # an abbreviation
    ("RP with macular involvement", "HP:0000510"),
    ("Pattern Dystrophy", "HP:0007963"),         # ontology best was 0.71 — under the bar
    ("Choroidal dystrophy", "HP:0001135"),       # HPO has no "choroidal dystrophy" at all
    ("tunnel vision", "HP:0001133"),             # jargon with no HPO synonym
    ("nonrecordable ERG", "HP:0000512"),
])
def test_the_alias_still_fires_where_the_ontology_has_nothing(phrase, expected):
    """The gate must not swallow the table's actual job: shorthand the ontology cannot resolve. These
    are exactly the strings the lab's own case sheet uses."""
    term = map_phrase(phrase)
    assert term is not None and term.hpo_id == expected


# --- the anti-fabrication property ----------------------------------------------------------------


def test_llm_cannot_author_an_id_only_choose_a_candidate_number():
    """The SELECT turn is a closed set: an LLM that answers with an HPO ID (rather than a number) is
    discarded. This is the whole point of the design — the model never types an identifier.

    ``bone spicule pigmentation`` is the fixture because it reaches the LLM: no exact ontology name, no
    curated alias, and its best lexical hit (0.77) sits under the lexical-accept bar."""
    def rogue(messages: list[dict]) -> str:
        if "phenotyping assistant" in messages[0]["content"]:
            return json.dumps([{"phrase": "bone spicule pigmentation", "negated": False, "source": "x"}])
        return json.dumps({"choice": "HP:9999999", "reason": "I know this one"})    # not a number

    out = map_text_to_hpo("bone spicule pigmentation", chat_fn=rogue)
    assert out["hpo_terms"] == []                                  # nothing invented got through
    assert out["unmapped"][0]["phrase"] == "bone spicule pigmentation"
    assert "HP:9999999" not in json.dumps(out)


def test_out_of_range_choice_is_rejected():
    chat = _chat([{"phrase": "bone spicule pigmentation", "negated": False, "source": ""}], choices=[99])
    out = map_text_to_hpo("bone spicule pigmentation", chat_fn=chat)
    assert out["hpo_terms"] == [] and out["n_observed"] == 0


def test_llm_may_answer_none_and_that_is_respected():
    """'None of these' is a correct answer that keeps a wrong term out of LIRICAL — it must not be
    overridden by the lexical fallback."""
    chat = _chat([{"phrase": "bone spicule pigmentation", "negated": False, "source": ""}], choices=[0])
    out = map_text_to_hpo("bone spicule pigmentation", chat_fn=chat)
    assert len(chat.calls) == 2                    # the candidates WERE put to the LLM (extract + select)
    assert out["hpo_terms"] == [] and len(out["unmapped"]) == 1


def test_canonical_name_comes_from_the_ontology_not_the_model():
    chat = _chat([{"phrase": "bone spicule pigmentation", "negated": False, "source": "骨细胞样色素沉着"}])
    out = map_text_to_hpo("骨细胞样色素沉着", chat_fn=chat)
    term = out["observed"][0]
    # the LLM picked candidate 1; the ID *and* the wording both come from HPO, not from the model
    assert term["hpo_id"] == "HP:0007737" and term["name"] == "Spicular pigmentation of the retina"
    assert term["method"] == "llm_closed_set" and term["source"] == "骨细胞样色素沉着"   # auditable trail


# --- a whole note ---------------------------------------------------------------------------------


def test_maps_a_chinese_note_into_observed_and_excluded_terms():
    # what the LLM is really for: 中文 -> English clinical phrases + negation. The extraction is
    # scripted here; the mapping from those phrases to real IDs is the code under test.
    chat = _chat([
        {"phrase": "night blindness", "negated": False, "source": "自幼夜盲"},
        {"phrase": "constricted visual field", "negated": False, "source": "视野缩窄"},
        {"phrase": "nonrecordable electroretinogram", "negated": False, "source": "ERG 呈熄灭型"},
        {"phrase": "hearing impairment", "negated": True, "source": "无听力障碍"},
    ])
    out = map_text_to_hpo("10 岁男孩，自幼夜盲，视野缩窄，ERG 呈熄灭型，无听力障碍", chat_fn=chat)

    assert out["mode"] == "llm"
    assert "HP:0000662" in out["hpo_terms"]                     # 夜盲 -> Nyctalopia
    assert out["excluded_hpo"] == ["HP:0000365"]                # 无听力障碍 -> Hearing impairment, ABSENT
    assert all(get_index().validate(h)["status"] == "ok" for h in out["hpo_terms"])
    assert out["hpo_version"].startswith("http")


def test_a_term_both_present_and_absent_stays_observed_and_is_flagged():
    # a spurious exclusion pushes LIRICAL AWAY from the right disease, so on contradiction we keep the
    # observed term and drop the exclusion — loudly
    chat = _chat([{"phrase": "night blindness", "negated": False, "source": "夜盲"},
                  {"phrase": "nyctalopia", "negated": True, "source": "无夜盲"}])
    out = map_text_to_hpo("夜盲 ... 无夜盲", chat_fn=chat)
    assert out["hpo_terms"] == ["HP:0000662"] and out["excluded_hpo"] == []
    assert any("both present and absent" in w for w in out["warnings"])


def test_unmapped_phrases_are_reported_never_silently_dropped():
    chat = _chat([{"phrase": "night blindness", "negated": False, "source": ""},
                  {"phrase": "grandmother lives in Xi'an", "negated": False, "source": ""}], choices=[0])
    out = map_text_to_hpo("...", chat_fn=chat)
    assert out["hpo_terms"] == ["HP:0000662"]
    assert len(out["unmapped"]) == 1 and any("could not be mapped" in w for w in out["warnings"])


@pytest.mark.parametrize("diagnosis,expected", [
    # The eight distinct Diagnosis strings from the lab's solved-IRD-case sheet — how the clinicians
    # actually write it. Every one must map with NO LLM (these are the exact strings we will be handed);
    # a regression here means a real case silently loses its phenotype.
    ("Choroidal dystrophy", "HP:0001135"),          # HPO has no "choroidal dystrophy" → chorioretinal
    ("Pattern Dystrophy", "HP:0007963"),
    ("Stargardt", "HP:0007754"),
    ("Achromatopsia", "HP:0011516"),
    ("Cone dystrophy", "HP:0008020"),
    ("BBS", "HP:0000556"),                          # abbreviation, and a disease HPO does not name
    ("RP with macular involvement", "HP:0000510"),
    ("Macular dystrophy", "HP:0007754"),
])
def test_maps_every_diagnosis_in_the_real_case_sheet(diagnosis, expected):
    term = map_phrase(diagnosis)
    assert term is not None and term.hpo_id == expected


@pytest.mark.parametrize("text", ["RPE atrophy", "the corp study", "RPGR variant", "the third eyelid"])
def test_short_aliases_do_not_fire_inside_unrelated_words(text):
    """'rp'/'ird'/'bbs' are real clinical abbreviations but dangerous substrings — matching is
    word-boundary anchored so 'RPE'/'RPGR'/'third' cannot be read as a phenotype."""
    from bioagent.tools.hpo_terms import infer_hpo_terms
    assert infer_hpo_terms(text, default=False) == []


def test_no_llm_degrades_to_the_curated_table_and_says_so():
    out = map_text_to_hpo("Patient with retinitis pigmentosa and night blindness")
    assert out["mode"] == "lexical_only"
    assert {"HP:0000510", "HP:0000662"} <= set(out["hpo_terms"])
    assert any("No LLM was available" in w for w in out["warnings"])


@pytest.mark.parametrize("text,cue", [
    ("Fundus shows macular dystrophy. Denies night blindness.", "denied finding"),
    ("Macular dystrophy. Family history: a cousin has retinitis pigmentosa.", "someone else's disease"),
    ("Macular dystrophy. No hearing impairment.", "negation"),
    ("黄斑营养不良。否认夜盲。", "Chinese negation"),
    ("黄斑营养不良。其表姐患视网膜色素变性。", "Chinese family history"),
])
def test_without_an_llm_prose_with_negation_or_family_history_maps_to_nothing(text, cue):
    """The fallback substring-scans the WHOLE text, so it cannot tell a DENIED finding, or a RELATIVE's
    disease, from the patient's own — it read 'denies night blindness' as Nyctalopia OBSERVED and a
    cousin's RP as the proband's. That is not low recall, it is a wrong phenotype, which is the exact
    silent failure this module exists to prevent. So it refuses instead ({cue})."""
    out = map_text_to_hpo(text)
    assert out["mode"] == "needs_llm"
    assert out["hpo_terms"] == [] and out["excluded_hpo"] == []
    assert any("NOT MAPPED" in w for w in out["warnings"])


@pytest.mark.parametrize("diagnosis", ["Stargardt", "BBS", "RP with macular involvement", "Achromatopsia"])
def test_a_bare_diagnosis_label_is_still_mapped_without_an_llm(diagnosis):
    """The guard must not swallow how the lab's sheet actually writes a case: a bare diagnosis carries
    no negation and no family history, so the lexical scan is sound on it."""
    out = map_text_to_hpo(diagnosis)
    assert out["mode"] == "lexical_only" and len(out["hpo_terms"]) == 1


def test_the_guard_does_not_fire_when_an_llm_did_the_extraction():
    """The cue guard is only about the LLM-less fallback: with an LLM, negation is handled properly and
    'no hearing impairment' becomes an EXCLUDED term rather than a reason to refuse."""
    chat = _chat([{"phrase": "night blindness", "negated": False, "source": ""},
                  {"phrase": "hearing impairment", "negated": True, "source": "no hearing impairment"}])
    out = map_text_to_hpo("Night blindness. No hearing impairment.", chat_fn=chat)
    assert out["mode"] == "llm"
    assert out["hpo_terms"] == ["HP:0000662"] and out["excluded_hpo"] == ["HP:0000365"]


# --- validate_hpo_ids: the gate for IDs that skipped the mapper -------------------------------------


def test_validate_hpo_ids_forwards_obsolete_and_rejects_invented():
    out = validate_hpo_ids(["HP:0000510", "HP:0000057", "HP:9999999", "nonsense", "hp:0000662"])
    assert out["valid"] == ["HP:0000510", "HP:0008665", "HP:0000662"]      # obsolete -> replacement
    assert out["remapped"] == [{"from": "HP:0000057", "to": "HP:0008665", "name": "Clitoral hypertrophy"}]
    assert {r["status"] for r in out["rejected"]} == {"unknown", "malformed"}
    assert out["labels"]["HP:0000510"] == "Rod-cone dystrophy"


def test_run_lirical_drops_a_fabricated_id_before_it_can_define_the_phenotype():
    # a real term + an invented one: the gate runs BEFORE the staging check, so it reports even here
    out = run_lirical(hpo_terms=["HP:0000510", "HP:9999999"], data_dir="", exec_fn=None)
    assert out["status"] == "not_installed"
    assert any("HP:9999999" in n and "dropped" in n for n in out["phenotype_notes"])


def test_run_lirical_refuses_when_no_supplied_term_is_real():
    out = run_lirical(hpo_terms=["HP:9999999", "HP:1234567"], data_dir="/data", exec_fn=lambda cmd: None)
    assert out["status"] == "error"
    assert "map_phenotype_to_hpo" in out["error"]                  # points at the right tool


def test_run_lirical_passes_only_validated_terms_to_the_cli(tmp_path):
    captured: dict = {}

    class _Proc:
        returncode = 1
        stderr = "stopped after argv capture"

    def fake_exec(cmd):
        captured["cmd"] = cmd
        return _Proc()

    run_lirical(hpo_terms=["HP:0000510", "HP:9999999"], excluded_hpo=["HP:0000365"],
                data_dir="/data", exec_fn=fake_exec, workspace=str(tmp_path))
    argv = " ".join(captured["cmd"])
    assert "HP:0000510" in argv and "HP:9999999" not in argv
    assert "HP:0000365" in argv


# --- ontology-release drift (our lexicon vs LIRICAL's own hp.json) ----------------------------------


def _hp_json(tmp_path, release: str):
    """A stub hp.json carrying only the release stamp — hp_json_release must not need to parse 23 MB."""
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    (d / "hp.json").write_text(
        '{"graphs":[{"meta":{"version":"http://purl.obolibrary.org/obo/hp/releases/'
        + release + '/hp.json"},"nodes":[]}]}', encoding="utf-8")
    return d


def test_hp_json_release_is_read_without_parsing_the_whole_file(tmp_path):
    from bioagent.tools.hpo_terms.index import hp_json_release, release_date

    assert hp_json_release(_hp_json(tmp_path, "2026-06-23") / "hp.json") == "2026-06-23"
    assert hp_json_release(tmp_path / "nope.json") == ""              # unreadable → no claim
    assert release_date(get_index().version) == "2026-06-23"          # what we actually ship
    assert release_date("not a version url") == ""


def test_no_drift_note_when_we_and_lirical_agree(tmp_path):
    from bioagent.tools.phenotype_dx import hpo_release_drift

    assert hpo_release_drift(str(_hp_json(tmp_path, "2026-06-23")), get_index().version) == []
    assert hpo_release_drift("", get_index().version) == []           # no data dir → nothing to compare


def test_drift_is_reported_when_lirical_ontology_moves_ahead(tmp_path):
    """The silent failure this guards: someone re-runs `lirical download`, LIRICAL's ontology advances,
    our committed lexicon does not, and terms retired in between just stop matching — no error."""
    from bioagent.tools.phenotype_dx import hpo_release_drift

    notes = hpo_release_drift(str(_hp_json(tmp_path, "2027-01-15")), get_index().version)
    assert len(notes) == 1
    assert "2026-06-23" in notes[0] and "2027-01-15" in notes[0]
    assert "build_hpo_lexicon.py" in notes[0]                          # says how to fix it


def test_run_lirical_surfaces_the_drift_note(tmp_path):
    data_dir = _hp_json(tmp_path, "2027-01-15")
    out = run_lirical(hpo_terms=["HP:0000510"], data_dir=str(data_dir), exec_fn=None)
    assert out["status"] == "not_installed"
    assert any("HPO release mismatch" in n for n in out["phenotype_notes"])


# --- the tool ---------------------------------------------------------------------------------------


def test_tool_reports_an_error_on_empty_text():
    tool = make_hpo_mapping_tool()
    assert tool.name == "map_phenotype_to_hpo" and tool.category == "annotation"
    out = tool.executor({"text": "  "}, None)
    assert out["status"] == "error"


def test_tool_runs_without_a_served_model():
    """ctx with no tunnel_port (tests, or a session before the GPU is up) -> lexical degrade, not a crash."""
    class _Ctx:
        tunnel_port = None
        workspace = None

    out = make_hpo_mapping_tool().executor({"text": "retinitis pigmentosa"}, _Ctx())
    assert out["status"] == "ok" and out["hpo_terms"] == ["HP:0000510"]
    assert out["mode"] == "lexical_only"
