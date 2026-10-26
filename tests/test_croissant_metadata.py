"""Croissant metadata (RCHENLAB/AiScientist issue 9): every document AiScientist generates is valid.

The documents are generated from small fixture datasets (``tests/fixtures/croissant``) exactly as the
gateway generates them — the upload peek, then :func:`aiscientist.metadata.dataset_facts`, then
:func:`build_dataset` — and checked with mlcroissant, the reference validator. CI installs mlcroissant
(requirements-dev.txt) and :func:`test_ci_validates_with_mlcroissant` fails there if it is missing, so
a green build means mlcroissant accepted every document below. Locally, without it, the mlcroissant
tests skip and AiScientist's own checks still run.

Also pinned: what must never reach metadata (sample identifiers, server paths, credentials), the
owner's review form, the result's provenance, and that the records of a generated document load.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from aiscientist import metadata as md
from aiscientist.metadata import vocab

FIXTURES = Path(__file__).parent / "fixtures" / "croissant"
UUID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
# A made-up key in the shape of an OpenRouter one, assembled at run time: written out whole it would
# trip scripts/pr_review_gate.py, which allows no placeholder for that shape.
FAKE_KEY = "sk" + "-or-v1-" + "0123456789abcdef" * 2


def _mlc():
    return pytest.importorskip("mlcroissant")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _peek(path: Path) -> dict:
    from aiscientist.tools.api import peek_dataset
    return peek_dataset(str(path), name=path.name)


def _dataset(path: Path, *, user_fields: dict | None = None, sha: bool = True) -> md.Built:
    facts = md.dataset_facts(name=path.name, kind=path.suffix.lstrip("."), size_bytes=path.stat().st_size,
                             sha256=_sha(path) if sha else None, uploaded_at="2026-10-16T10:00:00+00:00",
                             owner="alice", peek=_peek(path))
    return md.build_dataset(UUID, facts, user_fields or {})


def _folder(root: Path, name: str = "retina_run") -> md.Built:
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            files.append({"path": p.relative_to(root).as_posix(), "size": p.stat().st_size,
                          "sha256": _sha(p), "peek": _peek(p), "verified": "upload"})
    facts = md.dataset_facts(name=name, kind="folder", size_bytes=sum(f["size"] for f in files), sha256=None,
                             uploaded_at="2026-10-16T10:00:00+00:00", owner="alice", files=files)
    return md.build_dataset(UUID, facts, {})


def _assert_valid(doc: dict) -> dict:
    report = md.validate(doc)
    assert report["ok"], report["errors"]
    return report


# --- the reference validator ----------------------------------------------------------------------------


def test_ci_validates_with_mlcroissant():
    """In CI the documents must be checked by mlcroissant itself, not only by our own rules."""
    if os.environ.get("CI"):
        assert md.mlcroissant_version(), "CI must install mlcroissant (requirements-dev.txt)"
    else:
        pytest.skip("only enforced in CI")


@pytest.mark.parametrize("name", ["retina_counts.csv", "libraries.tsv", "variants.vcf"])
def test_each_fixture_file_gets_a_valid_croissant_json(name):
    _mlc()
    built = _dataset(FIXTURES / name)
    report = _assert_valid(built.document)
    assert report["validator"].startswith("aiscientist + mlcroissant")
    assert report["warnings"] == []
    doc = built.document
    assert doc["conformsTo"] == md.CROISSANT_1_1
    assert doc["@id"] == f"urn:uuid:{UUID}"
    (entry,) = doc["distribution"]
    assert entry["@type"] == "cr:FileObject" and entry["sha256"] == _sha(FIXTURES / name)
    assert entry["contentUrl"] == name and entry["contentSize"] == f"{(FIXTURES / name).stat().st_size} B"


def test_a_folder_gets_one_document_with_every_file_and_a_record_set_per_table(tmp_path):
    _mlc()
    root = tmp_path / "retina_run"
    (root / "tables").mkdir(parents=True)
    shutil.copy(FIXTURES / "retina_counts.csv", root / "tables" / "retina_counts.csv")
    shutil.copy(FIXTURES / "libraries.tsv", root / "libraries.tsv")
    (root / "notes.txt").write_text("synthetic notes\n")
    built = _folder(root)
    _assert_valid(built.document)
    doc = built.document
    assert sorted(d["contentUrl"] for d in doc["distribution"]) == [
        "libraries.tsv", "notes.txt", "tables/retina_counts.csv"]
    sets = {rs["name"]: rs for rs in doc["recordSet"]}
    assert set(sets) == {"libraries", "tables_retina_counts"}
    types = {f["name"]: f["dataType"] for f in sets["tables_retina_counts"]["field"]}
    assert types == {"gene": "sc:Text", "cell_type": "sc:Text", "count": "sc:Integer", "mean_expr": "sc:Float",
                     "detected": "sc:Boolean", "date_measured": "sc:Date"}
    assert {f["name"]: f["dataType"] for f in sets["libraries"]["field"]}["n_cells"] == "sc:Integer"
    assert doc["description"].startswith("Folder retina_run/ with 3 files")


def test_the_records_of_a_generated_document_load_with_mlcroissant(tmp_path):
    """Generated, not just syntactically valid: a Croissant consumer reads the table back."""
    mlc = _mlc()
    root = tmp_path / "ds"
    root.mkdir()
    shutil.copy(FIXTURES / "retina_counts.csv", root / "retina_counts.csv")
    built = _folder(root, "ds")
    (root / "croissant.json").write_text(json.dumps(built.document))
    rows = list(mlc.Dataset(jsonld=str(root / "croissant.json")).records(record_set="table/retina_counts"))
    assert len(rows) == 6
    first = {k.split("/")[-1]: (v.decode() if isinstance(v, bytes) else v) for k, v in rows[0].items()}
    assert first["gene"] == "RHO" and first["count"] == 1520 and first["mean_expr"] == 12.75


def test_a_file_without_a_measured_checksum_is_an_exact_fileset_and_still_valid():
    _mlc()
    built = _dataset(FIXTURES / "retina_counts.csv", sha=False)
    (entry,) = built.document["distribution"]
    assert entry["@type"] == "cr:FileSet" and entry["includes"] == "retina_counts.csv"
    assert built.document["recordSet"][0]["field"][0]["source"] == {
        "fileSet": {"@id": entry["@id"]}, "extract": {"column": "gene"}}
    _assert_valid(built.document)


def test_a_gzip_table_gets_a_record_set_that_loads(tmp_path):
    mlc = _mlc()
    path = tmp_path / "libraries.tsv.gz"
    path.write_bytes(gzip.compress((FIXTURES / "libraries.tsv").read_bytes()))
    built = _dataset(path)
    _assert_valid(built.document)
    (tmp_path / "croissant.json").write_text(json.dumps(built.document))
    rows = list(mlc.Dataset(jsonld=str(tmp_path / "croissant.json")).records(record_set="table/libraries"))
    assert [r["table/libraries/n_cells"] for r in rows] == [5120, 4980, 3015, 5410]


def test_an_anndata_file_is_described_from_its_header(tmp_path):
    h5py = pytest.importorskip("h5py")
    import numpy as np
    path = tmp_path / "retina.h5ad"
    with h5py.File(path, "w") as f:
        f.create_dataset("X", data=np.zeros((7, 5), dtype="float32"))
        obs = f.create_group("obs")
        obs.create_dataset("cell_type", data=np.array([b"rod"] * 7))
        f.create_group("var")
    built = _dataset(path)
    _assert_valid(built.document)
    (entry,) = built.document["distribution"]
    assert entry["encodingFormat"] == ["application/x-hdf5", "http://edamontology.org/format_3590"]
    assert "7 observations × 5 variables" in entry["description"]
    assert "http://edamontology.org/data_3112" in built.document["additionalType"]
    # a cell-by-gene matrix suggests single-cell RNA-seq, at low confidence, for the owner to confirm
    (tech,) = built.document["measurementTechnique"]
    assert tech["@id"] == "http://www.ebi.ac.uk/efo/EFO_0008913"
    entry = next(p for p in built.provenance if p["path"] == "measurementTechnique[0]")
    assert entry["confidence"] < 0.7 and entry["source"] == "file-content" and not entry["reviewed"]


# --- what never reaches metadata ---------------------------------------------------------------------------


def test_vcf_sample_names_are_counted_never_kept_and_the_build_gives_the_organism():
    built = _dataset(FIXTURES / "variants.vcf")
    text = json.dumps([built.document, built.provenance, built.confidence, built.facets]) + built.search_text
    assert "SYNTHETIC_SAMPLE" not in text
    assert "2 sample(s), genome build GRCh38" in built.document["distribution"][0]["description"]
    organism = built.document["about"][0]
    assert organism["@id"] == "http://purl.obolibrary.org/obo/NCBITaxon_9606"
    entry = next(p for p in built.provenance if p["path"] == "about[0]")
    assert (entry["source"], entry["confidence"]) == ("file-content", 0.85)


def test_table_values_are_used_for_types_and_then_dropped():
    built = _dataset(FIXTURES / "retina_counts.csv")
    text = json.dumps([built.document, built.provenance]) + built.search_text
    for value in ("1520", "12.75", "POU4F2", "ganglion"):
        assert value not in text


def test_a_server_path_or_a_credential_in_a_document_is_an_error():
    doc = _dataset(FIXTURES / "retina_counts.csv").document
    leaked = {**doc, "description": "copied from /data/Users/shared/retina/x.h5ad"}
    errors, _ = md.own_checks(leaked)
    assert any("server path" in e for e in errors)
    secret = {**doc, "citeAs": f"key {FAKE_KEY}"}
    assert any("credential" in e for e in md.own_checks(secret)[0])


def test_parameters_keep_settings_and_drop_code_paths_and_keys():
    rounds = [{"step": "QC", "verdict": {"verdict": "accept"}, "scientist_result": {"steps": [
        {"tool": "run_scanpy_qc", "ok": True, "args": {"min_genes": 200, "max_pct_mito": 20.0,
                                                        "input": "/data/Users/alice/run/x.h5ad"}},
        {"tool": "run_code", "ok": True, "args": {"code": "import scanpy", "label": "plot"}},
        {"tool": "broken", "ok": False, "args": {}}]}},
        {"step": "DE", "verdict": {"verdict": "revise"}, "scientist_result": {"steps": [
            {"tool": "run_de", "ok": True, "args": {"groupby": "cell_type"}}]}}]
    params = md.run_parameters(rounds)
    assert params == [
        {"step": "QC", "tool": "run_scanpy_qc", "args": {"min_genes": 200, "max_pct_mito": 20.0, "input": "x.h5ad"}},
        {"step": "QC", "tool": "run_code", "args": {"label": "plot"}}]
    assert md.safe_value("token aisci_abcdefghijklmnopqrstuv") == "<redacted>"


# --- the owner's review ------------------------------------------------------------------------------------


def test_the_owners_review_replaces_drafts_and_is_marked_reviewed():
    _mlc()
    fields = md.clean_user_fields({
        "description": "Single-cell counts of a synthetic retina sample.",
        "organism": "mouse", "tissue": ["retina", "UBERON:0001782"], "assay": "EFO:0009922",
        "cell_type": "rods, unknown cell X", "accessions": "GSE123456, gsm5676874",
        "license": "CC-BY-4.0", "citation": "Doe J. et al. (2026) Synthetic retina.", "version": "1.2.0",
        "keywords": "retina, development", "ignored_field": "x"})
    assert "ignored_field" not in fields and fields["accessions"] == ["GSE123456", "GSM5676874"]
    built = _dataset(FIXTURES / "retina_counts.csv", user_fields=fields)
    _assert_valid(built.document)
    doc = built.document
    assert [t.get("@id") for t in doc["about"]] == [
        "http://purl.obolibrary.org/obo/NCBITaxon_10090", "http://purl.obolibrary.org/obo/UBERON_0000966",
        "http://purl.obolibrary.org/obo/UBERON_0001782", "http://purl.obolibrary.org/obo/CL_0000604", None]
    assert doc["about"][-1] == {"@type": "sc:DefinedTerm", "name": "unknown cell X"}
    assert doc["measurementTechnique"][0]["termCode"] == "EFO:0009922"
    assert doc["license"] == "https://spdx.org/licenses/CC-BY-4.0.html"
    assert doc["sameAs"] == ["https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE123456",
                             "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSM5676874"]
    assert doc["version"] == "1.2.0" and doc["citeAs"].startswith("Doe J.")
    owned = [p for p in built.provenance if p["source"] == "owner"]
    assert owned and all(p["reviewed"] and p["confidence"] == 1.0 for p in owned)
    assert built.confidence[f"urn:uuid:{UUID}"]["unsure"] == []      # nothing left to check at dataset level


def test_human_data_fills_the_responsible_ai_fields():
    _mlc()
    fields = md.clean_user_fields({"human_data": True, "consent": "IRB 2026-123, broad research consent",
                                   "use_restrictions": "Research use only; no re-identification."})
    doc = _dataset(FIXTURES / "variants.vcf", user_fields=fields).document
    assert doc["conformsTo"] == [md.CROISSANT_1_1, md.RAI_1_0]
    assert "IRB 2026-123" in doc["rai:personalSensitiveInformation"]
    assert doc["rai:dataUseCases"] == "Research use only; no re-identification."
    assert doc["conditionsOfAccess"].endswith("Use restrictions: Research use only; no re-identification.")
    _assert_valid(doc)
    unrecorded = _dataset(FIXTURES / "variants.vcf", user_fields=md.clean_user_fields({"human_data": True})).document
    assert md.validate(unrecorded)["warnings"][:1] == ["human data: how consent was obtained is not recorded"]


@pytest.mark.parametrize("fields, message", [
    ({"description": "see /data/Users/alice/x.h5ad"}, "server path"),
    ({"citation": FAKE_KEY}, "credential"),
    ({"accessions": "not-an-accession"}, "accession"),
    ({"version": "v2"}, "MAJOR.MINOR.PATCH"),
    ({"license": "WTFPL"}, "license"),
    ({"name": "  "}, "needs a name"),
])
def test_the_form_refuses_what_metadata_must_not_hold(fields, message):
    with pytest.raises(ValueError, match=message):
        md.clean_user_fields(fields)


def test_the_form_shows_what_to_check_first():
    built = _dataset(FIXTURES / "retina_counts.csv")
    facts = md.dataset_facts(name="retina_counts.csv", kind="csv", size_bytes=1, sha256=None,
                             uploaded_at="2026-10-16T10:00:00+00:00", owner="alice")
    view = md.form_view(facts, {}, built.document, built.provenance, built.confidence)
    assert [f["key"] for f in view["fields"]][:3] == ["name", "description", "organism"]
    assert "license" in view["needs_review"] and "description" in view["needs_review"]
    assert view["sources"]["license"]["source"] == "default"
    assert view["sources"]["name"]["confidence"] == 1.0


# --- results ---------------------------------------------------------------------------------------------


def _result_facts(art: Path) -> dict:
    return {"run_id": "abc123def456", "owner": "alice", "status": "done",
            "question": md.scrub_text("Cluster the rods in /data/Users/alice/uploads/retina.h5ad"),
            "started_at": "2026-10-16T09:00:00+00:00", "finished_at": "2026-10-16T10:00:00+00:00",
            "pipeline": {"route": "research", "presets": ["scrna_standard"], "plan_mode": True},
            "software": {"name": "AiScientist", "version": "0.2.0", "git_sha": "0123456789abcdef"},
            "models": ["qwen3.8-27b-int4"],
            "parameters": [{"step": "QC", "tool": "run_scanpy_qc", "args": {"min_genes": 200}}],
            "inputs": [{"uuid": UUID, "name": "retina.h5ad", "kind": "dataset", "role": None}],
            "files": md.result_files(art)}


def test_a_result_records_its_inputs_run_pipeline_version_parameters_and_models(tmp_path):
    _mlc()
    art = tmp_path / "artifacts"
    (art / "tables").mkdir(parents=True)
    shutil.copy(FIXTURES / "retina_counts.csv", art / "tables" / "markers.csv")
    (art / "report.md").write_text("# Report\n")
    built = md.build_result(md.result_uuid("abc123def456"), _result_facts(art))
    _assert_valid(built.document)
    doc = built.document
    assert doc["prov:wasDerivedFrom"] == [{"@id": f"urn:uuid:{UUID}"}] == doc["isBasedOn"]
    act = doc["prov:wasGeneratedBy"]
    assert act["@id"] == "urn:aiscientist:run:abc123def456"
    assert (act["prov:startedAtTime"], act["prov:endedAtTime"]) == ("2026-10-16T09:00:00+00:00",
                                                                    "2026-10-16T10:00:00+00:00")
    assert act["prov:used"] == [{"@id": f"urn:uuid:{UUID}"}]
    assert act["prov:wasAssociatedWith"][0]["prov:label"] == "AiScientist 0.2.0 (commit 0123456789ab)"
    assert act["prov:wasAssociatedWith"][1]["prov:label"] == "language model qwen3.8-27b-int4"
    assert act["aiscientist:parameters"][0]["args"] == {"min_genes": 200}
    assert act["aiscientist:pipeline"]["presets"] == ["scrna_standard"]
    assert {d["contentUrl"] for d in doc["distribution"]} == {"report.md", "tables/markers.csv"}
    assert all(d["sha256"] == _sha(art / d["contentUrl"]) for d in doc["distribution"])
    assert doc["recordSet"][0]["name"] == "tables_markers"
    assert "/data/" not in json.dumps(doc) and "retina.h5ad" in doc["description"]


def test_a_result_uuid_is_stable_per_run():
    assert md.result_uuid("abc") == md.result_uuid("abc") != md.result_uuid("abd")
    assert re.fullmatch(r"[0-9a-f-]{36}", md.result_uuid("abc"))


# --- previews -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("use_mlc", [True, False])
def test_preview_reads_the_first_rows_from_a_head(monkeypatch, use_mlc):
    if use_mlc:
        _mlc()
    else:
        import builtins
        real = builtins.__import__
        monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (
            (_ for _ in ()).throw(ImportError(name)) if name == "mlcroissant" else real(name, *a, **k)))
    built = _dataset(FIXTURES / "retina_counts.csv")
    rs, entry = md.pick_record_set(built.document)
    head = (FIXTURES / "retina_counts.csv").read_bytes()[:130]        # cut in the third row
    rows, how, notes = md.preview_rows(built.document, rs, entry, head, limit=10, truncated=True)
    assert how == ("mlcroissant" if use_mlc else "csv")
    assert [str(r["gene"]) for r in rows] == ["RHO", "GNAT1"]           # the cut line is dropped
    assert notes and "first part" in notes[0]


def test_preview_reads_a_gzip_head():
    built = _dataset(FIXTURES / "libraries.tsv")
    rs, entry = md.pick_record_set(built.document)
    entry = {**entry, "name": "libraries.tsv.gz"}
    head = gzip.compress((FIXTURES / "libraries.tsv").read_bytes())
    rows, _how, _ = md.preview_rows(built.document, rs, entry, head, limit=2)
    assert [str(r["library"]) for r in rows] == ["lib_A", "lib_B"]


# --- vocabularies -----------------------------------------------------------------------------------------


def test_every_term_is_a_well_formed_curie_with_its_obo_or_efo_iri():
    pattern = {"NCBITaxon": r"\d+", "UBERON": r"\d{7}", "CL": r"\d{7}", "EFO": r"\d{7}"}
    for name, terms in vocab.VOCABULARIES.items():
        ids = [t.id for t in terms]
        assert len(ids) == len(set(ids)), name
        for t in terms:
            prefix, local = t.id.split(":")
            assert re.fullmatch(pattern[prefix], local), t.id
            assert t.iri.endswith(f"{prefix}_{local}")
    assert vocab.resolve("organism", "GRCm39").id == "NCBITaxon:10090"
    assert vocab.resolve("tissue", "http://purl.obolibrary.org/obo/UBERON_0000966").label == "retina"
    assert vocab.resolve("assay", "EFO_0009922").label == "10x 3' v3"
    assert vocab.resolve("assay", "nonsense") is None


def test_every_edam_format_and_data_id_is_well_formed():
    from aiscientist.metadata import formats
    for fmt in formats.all_formats():
        assert not fmt.edam_format or re.fullmatch(r"format_\d{4}", fmt.edam_format), fmt
        assert not fmt.edam_data or re.fullmatch(r"data_\d{4}", fmt.edam_data), fmt
    assert formats.for_name("x.vcf.gz").key == "vcf.gz" and formats.for_name("x.gz").key == "gz"


def test_accessions_link_to_their_registry():
    assert md.accession_info("GSM5676874")["url"].endswith("acc=GSM5676874")
    assert md.accession_info("PRJNA123456")["registry"] == "BioProject"
    assert md.accession_info("SRR1234567")["registry"] == "SRA"
    assert md.accession_info("E-MTAB-1234")["registry"] == "ArrayExpress"
    assert md.accession_info("GSM") is None
    assert md.find_accessions("GSM5676874_GSE123456_retina.h5ad") == ["GSM5676874", "GSE123456"]
