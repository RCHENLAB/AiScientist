"""Build Croissant 1.1 documents (``croissant.json``) for datasets and analysis results.

The single place metadata is written: from facts (:mod:`.facts`) and the owner's review fields
(:mod:`.form`), never by editing JSON. Each build also returns, Boulangerie-style,

* ``provenance`` — a list of ``{"path", "value", "url", "confidence", "source", "reviewed",
  "auto_reviewed", "detail"}``, one per drafted value (``path`` is a dotted path into the
  document, e.g. ``about[0]`` or ``distribution[2].sha256``), so a reader knows where each value
  came from;
* ``confidence`` — per FileObject / FileSet / RecordSet ``@id`` (and the dataset's own ``@id``):
  ``{"confidence": <lowest>, "values", "reviewed", "auto_reviewed", "unsure": [...]}``, so a
  reviewer knows which parts to check.

Conformance notes, from running mlcroissant 1.1.1 against these documents:

* ``@vocab``/``sc`` must be ``https://schema.org/``: with the ``http`` form printed in the spec's
  appendix, mlcroissant says the document "doesn't extend https://schema.org/Dataset".
* A FileObject needs ``sha256`` (or ``md5``). A file whose checksum was never measured is
  described as a FileSet whose ``includes`` is exactly its path: honest, and valid.
* Every Field needs a ``dataType``; a table's columns get the atomic type their sampled values fit.
* ``keywords`` must be text: an ontology term goes in ``about`` / ``measurementTechnique`` as a
  ``DefinedTerm`` with its IRI, never in ``keywords``.
"""

from __future__ import annotations

import re
import uuid as _uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import formats, vocab
from .facts import accession_info, human_size, iso_now

CROISSANT_1_1 = "http://mlcommons.org/croissant/1.1"
RAI_1_0 = "http://mlcommons.org/croissant/RAI/1.0"
#: Terms of AiScientist's own (a run's pipeline and parameters). Documented in
#: docs/development/metadata.md, which this IRI names.
AISCIENTIST_NS = "https://github.com/RCHENLAB/AiScientist/blob/main/docs/development/metadata.md#"
#: Results are named by UUIDv5(run id) in this namespace, so a run always has the same result UUID.
RESULT_NAMESPACE = _uuid.UUID("6f1d0c3e-5b8a-4c51-9f0e-a15c1e47d9b2")

CONTEXT: dict[str, Any] = {
    "@language": "en",
    "@vocab": "https://schema.org/",
    "sc": "https://schema.org/",
    "cr": "http://mlcommons.org/croissant/",
    "rai": "http://mlcommons.org/croissant/RAI/",
    "dct": "http://purl.org/dc/terms/",
    "prov": "http://www.w3.org/ns/prov#",
    "aiscientist": AISCIENTIST_NS,
    "annotation": "cr:annotation",
    "arrayShape": "cr:arrayShape",
    "citeAs": "cr:citeAs",
    "column": "cr:column",
    "conformsTo": "dct:conformsTo",
    "containedIn": "cr:containedIn",
    "data": {"@id": "cr:data", "@type": "@json"},
    "dataType": {"@id": "cr:dataType", "@type": "@vocab"},
    "equivalentProperty": "cr:equivalentProperty",
    "examples": {"@id": "cr:examples", "@type": "@json"},
    "excludes": "cr:excludes",
    "extract": "cr:extract",
    "field": "cr:field",
    "fileProperty": "cr:fileProperty",
    "fileObject": "cr:fileObject",
    "fileSet": "cr:fileSet",
    "format": "cr:format",
    "includes": "cr:includes",
    "isArray": "cr:isArray",
    "isLiveDataset": "cr:isLiveDataset",
    "jsonPath": "cr:jsonPath",
    "key": "cr:key",
    "md5": "cr:md5",
    "parentField": "cr:parentField",
    "path": "cr:path",
    "readLines": "cr:readLines",
    "recordSet": "cr:recordSet",
    "references": "cr:references",
    "regex": "cr:regex",
    "repeated": "cr:repeated",
    "replace": "cr:replace",
    "samplingRate": "cr:samplingRate",
    "sdVersion": "cr:sdVersion",
    "separator": "cr:separator",
    "source": "cr:source",
    "subField": "cr:subField",
    "transform": "cr:transform",
    "unArchive": "cr:unArchive",
    "value": "cr:value",
    # A run's pipeline and parameters are JSON values, kept as they are (not expanded to RDF).
    "aiscientist:pipeline": {"@type": "@json"},
    "aiscientist:parameters": {"@type": "@json"},
}

# Where a value came from. A reader can trust these in roughly this order.
SOURCE_OWNER = "owner"            # entered by the dataset's owner in the review form
SOURCE_UPLOAD = "upload-check"    # measured by the upload: size, SHA-256 checked against the copy
SOURCE_COMPUTED = "computed"      # measured when the metadata was written (a result file's SHA-256)
SOURCE_RUN = "run-record"         # the run's own record: inputs, pipeline, parameters, models
SOURCE_ACCOUNT = "account"        # the account and the registry row: owner, upload date
SOURCE_REGISTRY = "registry"      # the registry row only (a dataset older than its metadata)
SOURCE_CONTENT = "file-content"   # read from the file's head: header, columns, genome build
SOURCE_NAME = "file-name"         # guessed from a file or folder name
SOURCE_DEFAULT = "default"        # a placeholder until the owner decides (license, version)
SOURCE_PROFILE = "dataset-profile"  # read off the data by a run's profile of it (DATASET.md)
SOURCE_PI = "pi-description"      # the PI's reading of the dataset: the summary of its DATASET.md

UNSURE_BELOW = 0.7
DRAFT_NOTE = "Drafted automatically; not yet reviewed by its owner."
MAX_RECORD_SETS = 20
MAX_FIELDS = 100

PRIVATE_LICENSE_TEXT = ("Private: no license is granted. Reuse needs the permission of the dataset's "
                        "owner.")
PRIVATE_ACCESS_TEXT = ("Private to its owner's AiScientist account: shown only to that account, in the "
                       "web interface, the API and the MCP server.")


@dataclass
class Built:
    document: dict
    provenance: list[dict] = field(default_factory=list)
    confidence: dict = field(default_factory=dict)
    facets: dict = field(default_factory=dict)
    search_text: str = ""


class _Ledger:
    """Records, for every drafted value, where it came from and how sure the drafting was."""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._by_owner: dict[str, list[dict]] = {}

    def add(self, owner: str, path: str, value: Any, source: str, confidence: float,
            detail: str = "") -> None:
        reviewed = source == SOURCE_OWNER
        entry = {"path": path, "value": value if isinstance(value, (str, int, float, bool)) or value is None
                 else str(value), "url": "", "confidence": round(float(confidence), 2), "source": source,
                 "reviewed": reviewed, "auto_reviewed": False}
        if detail:
            entry["detail"] = detail
        self.entries.append(entry)
        self._by_owner.setdefault(owner, []).append(entry)

    def confidence(self) -> dict:
        out: dict[str, dict] = {}
        for owner, entries in self._by_owner.items():
            out[owner] = {
                "confidence": min(e["confidence"] for e in entries),
                "values": len(entries),
                "reviewed": sum(1 for e in entries if e["reviewed"]),
                "auto_reviewed": 0,
                "unsure": [{"path": e["path"], "value": e["value"], "confidence": e["confidence"]}
                           for e in entries if e["confidence"] < UNSURE_BELOW and not e["reviewed"]],
            }
        return out


def result_uuid(run_id: str) -> str:
    return str(_uuid.uuid5(RESULT_NAMESPACE, f"aiscientist-run:{run_id}"))


def _slug(text: str, keep_slash: bool = False) -> str:
    allowed = r"[^A-Za-z0-9._/-]+" if keep_slash else r"[^A-Za-z0-9._-]+"
    return re.sub(allowed, "_", text).strip("_") or "x"


class _Ids:
    def __init__(self) -> None:
        self.used: set[str] = set()

    def new(self, base: str) -> str:
        candidate, n = base, 2
        while candidate in self.used:
            candidate, n = f"{base}_{n}", n + 1
        self.used.add(candidate)
        return candidate


def _glob_escape(path: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", path)


def _term_json(term: "vocab.Term | None", label: str = "") -> dict:
    if term is None:
        return {"@type": "sc:DefinedTerm", "name": label}
    out = {"@type": "sc:DefinedTerm", "@id": term.iri, "name": term.label, "termCode": term.id}
    term_set = vocab.defined_term_set(term)
    if term_set:
        out["inDefinedTermSet"] = term_set
    return out


def _date(iso: str) -> str:
    return (iso or iso_now())[:10]


def _encoding(fmt: formats.Format) -> "str | list[str]":
    return [fmt.media_type, fmt.format_iri] if fmt.format_iri else fmt.media_type


# --- files and tables -------------------------------------------------------------------------------------


def _content_description(fmt: formats.Format, content: "dict | None") -> str:
    content = content or {}
    h5 = content.get("hdf5") or {}
    if h5.get("n_obs") is not None:
        text = (f"AnnData matrix: {h5['n_obs']:,} observations × {h5['n_vars']:,} variables "
                "(cells × genes by AnnData's convention).")
        if h5.get("obs"):
            text += f" Observation columns: {', '.join(h5['obs'][:15])}" + (" …" if len(h5['obs']) > 15 else "") + "."
        if h5.get("layers"):
            text += f" Layers: {', '.join(h5['layers'][:8])}."
        return text
    if h5.get("tenx_matrix"):
        text = "10x Genomics feature-barcode matrix (HDF5)."
        if h5.get("genome"):
            text += f" Genome: {h5['genome']}."
        return text
    vcf = content.get("vcf") or {}
    if vcf:
        bits = []
        if "n_samples" in vcf:
            bits.append(f"{vcf['n_samples']} sample(s)")
        if vcf.get("assembly"):
            bits.append(f"genome build {vcf['assembly']}")
        if vcf.get("caller"):
            bits.append(f"called with {vcf['caller']}")
        return ("Variant calls: " + ", ".join(bits) + ".") if bits else ""
    table = content.get("table") or {}
    if table.get("columns"):
        return f"Table with {table.get('n_columns') or len(table['columns'])} columns."
    return ""


def _distribution(files: list[dict], ids: _Ids, ledger: _Ledger, *, size_source: str,
                  sha_source: str) -> tuple[list[dict], list[tuple[dict, dict, formats.Format]]]:
    """FileObjects (checksum known) and exact-path FileSets (checksum not known) for ``files``;
    also returns the tables among them as ``(entry, file fact, format)``."""
    out: list[dict] = []
    tables: list[tuple[dict, dict, formats.Format]] = []
    for f in files:
        rel = f["path"]
        content = f.get("content") or {}
        fmt = formats.refine(formats.for_name(rel), content)
        i = len(out)
        if f.get("sha256"):
            entry: dict[str, Any] = {"@type": "cr:FileObject", "@id": ids.new("file/" + _slug(rel, True)),
                                     "name": rel, "contentUrl": rel}
            if f.get("size") is not None:
                entry["contentSize"] = f"{int(f['size'])} B"
            entry["encodingFormat"] = _encoding(fmt)
            entry["sha256"] = f["sha256"]
            verified = f.get("verified") or ""
            how = {"upload": "SHA-256 of the uploaded bytes, checked against the stored copy",
                   "computed": "SHA-256 computed when this metadata was written"}.get(verified, "SHA-256 from the registry")
            ledger.add(entry["@id"], f"distribution[{i}].sha256", f["sha256"],
                       sha_source if verified != "computed" else SOURCE_COMPUTED,
                       1.0 if verified in ("upload", "computed") else 0.95, how)
        else:
            entry = {"@type": "cr:FileSet", "@id": ids.new("files/" + _slug(rel, True)), "name": rel,
                     "includes": _glob_escape(rel), "encodingFormat": _encoding(fmt)}
            ledger.add(entry["@id"], f"distribution[{i}].includes", rel, size_source, 0.9,
                       "No checksum was recorded for this file, so it is a one-file FileSet.")
        desc = _content_description(fmt, content)
        if fmt.label != "File":
            desc = (f"{fmt.label}. " + desc).strip()
        declared = f.get("declared") if isinstance(f.get("declared"), dict) else {}
        if declared.get("description"):
            # What the skill or pipeline that made the file declares it holds (issue 10).
            said = str(declared["description"]).strip()
            desc = (said + ("" if said.endswith(".") else ".") + " " + desc).strip()
            ledger.add(entry["@id"], f"distribution[{i}].description", said, SOURCE_RUN, 0.9,
                       "Declared in the outputs of " + (str(declared.get("source") or "")
                                                        or "the skill that wrote it"))
        if f.get("size") is not None and entry["@type"] == "cr:FileSet":
            desc = (desc + f" Size: {human_size(f['size'])}.").strip()
        if desc:
            entry["description"] = desc
        if f.get("size") is not None:
            ledger.add(entry["@id"], f"distribution[{i}].contentSize", int(f["size"]), size_source,
                       1.0 if size_source in (SOURCE_UPLOAD, SOURCE_COMPUTED) else 0.95)
        detected = str(content.get("detected_format") or "")
        agrees = bool(detected) and (detected in fmt.key or fmt.key.startswith(detected)
                                     or (detected == "tabular" and bool(fmt.delimiter))
                                     or (detected in ("h5ad", "hdf5", "loom") and "hdf5" in fmt.media_type))
        ledger.add(entry["@id"], f"distribution[{i}].encodingFormat", fmt.media_type,
                   SOURCE_CONTENT if agrees else SOURCE_NAME, 0.95 if agrees else 0.8,
                   f"{fmt.label}" + (", confirmed by the file's content" if agrees else ", from the file name"))
        out.append(entry)
        if content.get("table") and fmt.delimiter in (",", "\t"):
            tables.append((entry, f, fmt))
    return out, tables


def _record_sets(tables: list[tuple[dict, dict, formats.Format]], ids: _Ids, ledger: _Ledger,
                 base_index: int = 0) -> list[dict]:
    out: list[dict] = []
    for entry, f, _fmt in tables[:MAX_RECORD_SETS]:
        table = f["content"]["table"]
        stem = re.sub(r"(\.(csv|tsv|tab|txt))?(\.gz)?$", "", f["path"], flags=re.IGNORECASE)
        rs_id = ids.new("table/" + _slug(stem, True))
        ref_key = "fileObject" if entry["@type"] == "cr:FileObject" else "fileSet"
        fields: list[dict] = []
        seen: set[str] = set()
        skipped = 0
        truncated = set(table.get("maybe_truncated") or [])
        sampled = int(table.get("sampled_rows") or 0)
        type_conf = 0.6 if sampled >= 5 else 0.4
        for j, (col, typ) in enumerate(zip(table.get("columns") or [], table.get("types") or [])):
            if len(fields) >= MAX_FIELDS:
                break
            if not col or col in seen or j in truncated:
                skipped += 1
                continue
            seen.add(col)
            fid = ids.new(f"{rs_id}/{_slug(col)}")
            fields.append({"@type": "cr:Field", "@id": fid, "name": col, "dataType": typ,
                           "source": {ref_key: {"@id": entry["@id"]}, "extract": {"column": col}}})
            ledger.add(rs_id, f"recordSet[{base_index + len(out)}].field[{len(fields) - 1}].dataType", typ,
                       SOURCE_CONTENT, type_conf, f"inferred from {sampled} sampled row(s)")
        if not fields:
            continue
        n_cols = int(table.get("n_columns") or len(fields))
        desc = (f"Rows of {f['path']}. Column names are from its header; each column's type was "
                f"inferred from {sampled} sampled row(s).")
        if n_cols > len(fields):
            desc += f" {len(fields)} of its {n_cols} columns are described."
        out.append({"@type": "cr:RecordSet", "@id": rs_id, "name": _slug(stem), "description": desc,
                    "field": fields})
    return out


# --- datasets -----------------------------------------------------------------------------------------------


def _terms_from(user_fields: dict, key: str, vocab_name: str) -> "list[tuple[vocab.Term | None, str]] | None":
    """The owner's terms for ``key`` (``None`` when the owner has not set the field)."""
    if key not in user_fields:
        return None
    out = []
    for item in user_fields.get(key) or []:
        if isinstance(item, dict):
            term = vocab.resolve(vocab_name, str(item.get("id") or "")) or vocab.resolve(vocab_name, str(item.get("label") or ""))
            out.append((term, str(item.get("label") or (term.label if term else ""))))
        else:
            term = vocab.resolve(vocab_name, str(item))
            out.append((term, term.label if term else str(item)))
    return [(t, label) for t, label in out if t or label]


def suggestions(facts: dict) -> dict[str, list[tuple[vocab.Term, str, float, str]]]:
    """Terms drafted from the facts: ``{vocab: [(term, source, confidence, why)]}``. Content beats
    names; a name only ever gives a low-confidence suggestion for the owner to confirm."""
    out: dict[str, list[tuple[vocab.Term, str, float, str]]] = {"organism": [], "tissue": [], "assay": []}
    names = " ".join([str(facts.get("name") or "")] + [f["path"] for f in facts.get("files") or []][:50])

    def put(kind: str, term: "vocab.Term | None", source: str, conf: float, why: str) -> None:
        if term is not None and all(t.id != term.id for t, *_ in out[kind]):
            out[kind].append((term, source, conf, why))

    for f in facts.get("files") or []:
        content = f.get("content") or {}
        vcf = content.get("vcf") or {}
        assembly = str(vcf.get("assembly") or "")          # e.g. "GRCh38 (from chr1 length)"
        for token in re.split(r"[^A-Za-z0-9]+", assembly):
            put("organism", vocab.resolve("organism", token), SOURCE_CONTENT, 0.85,
                f"genome build {assembly} in the VCF header of {f['path']}")
        h5 = content.get("hdf5") or {}
        genome = str(h5.get("genome") or "")
        for token in re.split(r"[^A-Za-z0-9]+", genome):
            put("organism", vocab.resolve("organism", token), SOURCE_CONTENT, 0.85,
                f"genome {genome} recorded in {f['path']}")
        if h5.get("n_obs") is not None or h5.get("tenx_matrix"):
            put("assay", vocab.resolve("assay", "EFO:0008913"), SOURCE_CONTENT, 0.3,
                f"{f['path']} is a cell-by-gene matrix: single-cell or single-nucleus RNA-seq, or spatial")
    for kind in ("organism", "tissue", "assay"):
        for term in vocab.mentions(kind, names):
            put(kind, term, SOURCE_NAME, 0.45 if kind != "organism" else 0.5, "named in a file or folder name")
    return out


def _license(user_fields: dict, ledger: _Ledger, owner: str) -> str:
    chosen = str(user_fields.get("license") or "")
    if chosen:
        lic = vocab.license_entry(chosen)
        if lic is None or lic["id"] == vocab.LICENSE_PRIVATE:
            ledger.add(owner, "license", "private", SOURCE_OWNER, 1.0)
            return PRIVATE_LICENSE_TEXT
        url = f"https://spdx.org/licenses/{lic['id']}.html"
        ledger.add(owner, "license", url, SOURCE_OWNER, 1.0)
        return url
    ledger.add(owner, "license", "private", SOURCE_DEFAULT, 0.3,
               "No license chosen yet: private until the owner picks one.")
    return PRIVATE_LICENSE_TEXT


def _accessions(user_fields: dict, facts: dict, ledger: _Ledger, owner: str) -> list[dict]:
    if "accessions" in user_fields:
        accs = [str(a) for a in user_fields.get("accessions") or []]
        source, conf, why = SOURCE_OWNER, 1.0, ""
    else:
        accs = list(facts.get("accessions") or [])
        source, conf, why = SOURCE_NAME, 0.7, "found in the dataset's name"
    out = []
    for a in accs:
        info = accession_info(a)
        if info is not None and info not in out:
            out.append(info)
            ledger.add(owner, f"sameAs[{len(out) - 1}]", info["url"], source, conf, why)
    return out


def _facets(about_terms: dict[str, list[tuple["vocab.Term | None", str]]], accessions: list[dict],
            fmts: list[formats.Format]) -> dict:
    facets: dict[str, list[str]] = {}
    for kind, terms in about_terms.items():
        facets[kind] = [x for t, label in terms for x in ((t.id, t.label) if t else (label,))] + \
            [s for t, _ in terms if t for s in t.synonyms]
        facets[f"{kind}_label"] = [t.label if t else label for t, label in terms]
    facets["accession"] = [a["id"] for a in accessions]
    facets["format"] = sorted({f.label for f in fmts if f.label != "File"})
    return facets


def build_dataset(uuid: str, facts: dict, user_fields: "dict | None" = None, *, base_url: str = "",
                  description: "dict | None" = None) -> Built:
    """The ``croissant.json`` of one dataset, from its facts and its owner's review, and from the
    record of its ``DATASET.md`` when a run has written one (``description``: the profile the PI
    read, its statements and the owner's edit; RCHENLAB/AiScientist issue 10). The organism and
    tissue the profile establishes, and the description's summary, are the same in both documents."""
    from . import description as dataset_md
    uf = dict(user_fields or {})
    rec = description if isinstance(description, dict) else {}
    ledger = _Ledger()
    did = f"urn:uuid:{uuid}"
    ids = _Ids()
    files = list(facts.get("files") or [])
    size_source = {"upload": SOURCE_UPLOAD, "result": SOURCE_UPLOAD}.get(str(facts.get("source")), SOURCE_REGISTRY)

    name = str(uf.get("name") or facts.get("name") or "dataset")
    ledger.add(did, "name", name, SOURCE_OWNER if uf.get("name") else SOURCE_ACCOUNT, 1.0)

    # the subject: organism, tissue, cell type, assay
    drafted = suggestions(facts)
    for kind, found in dataset_md.profile_terms(rec.get("profile") or {}).items():
        for term, why in found:
            if all(t.id != term.id for t, *_ in drafted[kind]):
                drafted[kind].insert(0, (term, SOURCE_PROFILE, 0.8 if kind == "organism" else 0.6, why))
    about_terms: dict[str, list[tuple["vocab.Term | None", str]]] = {}
    about: list[dict] = []
    techniques: list[dict] = []
    for key, vocab_name, target in (("organism", "organism", about), ("tissue", "tissue", about),
                                    ("cell_type", "cell_type", about), ("assay", "assay", techniques)):
        owned = _terms_from(uf, key, vocab_name)
        if owned is not None:
            about_terms[key] = owned
            for term, label in owned:
                target.append(_term_json(term, label))
                path = f"{'measurementTechnique' if target is techniques else 'about'}[{len(target) - 1}]"
                ledger.add(did, path, term.id if term else label, SOURCE_OWNER, 1.0)
        else:
            about_terms[key] = []
            for term, source, conf, why in drafted.get(vocab_name, []):
                about_terms[key].append((term, term.label))
                target.append(_term_json(term))
                path = f"{'measurementTechnique' if target is techniques else 'about'}[{len(target) - 1}]"
                ledger.add(did, path, term.id, source, conf, why)

    accessions = _accessions(uf, facts, ledger, did)
    fmts = [formats.refine(formats.for_name(f["path"]), f.get("content")) for f in files]

    # description
    owner_md = dataset_md.owner_summary(str((rec.get("owner") or {}).get("markdown") or ""))
    pi_summary = (dataset_md.summary_text(dataset_md.from_record(rec, facts, uf, name=name))
                  if rec.get("written_at") else "")
    if uf.get("description"):
        description = str(uf["description"])
        ledger.add(did, "description", "(the owner's text)", SOURCE_OWNER, 1.0)
    elif owner_md:
        description = owner_md
        ledger.add(did, "description", "(the summary of DATASET.md as its owner edited it)", SOURCE_OWNER, 1.0)
    elif pi_summary:
        description = pi_summary
        ledger.add(did, "description", "(the summary of DATASET.md)", SOURCE_PI, 0.6,
                   "Written by the PI the first time a run read this dataset: the counts are read "
                   "from the data, the rest is its reading of them.")
    else:
        description = _draft_description(facts, fmts)
        ledger.add(did, "description", description, SOURCE_CONTENT if files else SOURCE_REGISTRY, 0.5,
                   "Drafted from the files; the owner has not described the dataset yet.")

    keywords = _keywords(uf, about_terms, fmts)
    uploaded = str(facts.get("uploaded_at") or iso_now())
    owner_name = str(facts.get("owner") or "")
    doc: dict[str, Any] = {
        "@context": CONTEXT,
        "@type": "sc:Dataset",
        "@id": did,
        "conformsTo": CROISSANT_1_1,
        "name": name,
        "description": description,
        "url": f"{base_url}/api/datasets/{uuid}/croissant.json" if base_url else did,
        "identifier": [did, *[a["id"] for a in accessions]],
        "creator": {"@type": "sc:Person", "name": owner_name or "unknown"},
        "datePublished": _date(uploaded),
        "dateCreated": _date(uploaded),
        "version": str(uf.get("version") or "1.0.0"),
        "license": _license(uf, ledger, did),
        "conditionsOfAccess": PRIVATE_ACCESS_TEXT,
        "isAccessibleForFree": False,
    }
    ledger.add(did, "creator.name", owner_name, SOURCE_ACCOUNT, 1.0, "the uploading account")
    ledger.add(did, "datePublished", doc["datePublished"], SOURCE_ACCOUNT, 1.0, "the upload date")
    ledger.add(did, "version", doc["version"], SOURCE_OWNER if uf.get("version") else SOURCE_DEFAULT,
               1.0 if uf.get("version") else 0.9)
    if uf.get("_reviewed_at"):
        doc["dateModified"] = _date(str(uf["_reviewed_at"]))
    if keywords:
        doc["keywords"] = keywords
    if about:
        doc["about"] = about
    if techniques:
        doc["measurementTechnique"] = techniques
    data_types = sorted({f.data_iri for f in fmts if f.data_iri})
    if data_types:
        doc["additionalType"] = data_types
    if accessions:
        doc["sameAs"] = [a["url"] for a in accessions]
    if uf.get("citation"):
        doc["citeAs"] = str(uf["citation"])
        ledger.add(did, "citeAs", "(the owner's citation)", SOURCE_OWNER, 1.0)
    else:
        year = _date(uploaded)[:4]
        doc["citeAs"] = f"{owner_name or 'AiScientist user'} ({year}). {name}. AiScientist dataset {did}."
        ledger.add(did, "citeAs", doc["citeAs"], SOURCE_DEFAULT, 0.5, "A placeholder citation.")
    _responsible_ai(doc, uf, ledger, did)
    derived = facts.get("derived_from") or {}
    if derived.get("uuid"):
        doc["isBasedOn"] = [{"@id": f"urn:uuid:{derived['uuid']}"}]
        doc["prov:wasDerivedFrom"] = [{"@id": f"urn:uuid:{derived['uuid']}"}]
        ledger.add(did, "prov:wasDerivedFrom[0]", derived["uuid"], SOURCE_RUN, 1.0,
                   f"saved from the result of run {derived.get('run_id') or '?'}")

    distribution, tables = _distribution(files, ids, ledger, size_source=size_source,
                                         sha_source=SOURCE_UPLOAD if size_source == SOURCE_UPLOAD else SOURCE_REGISTRY)
    if facts.get("kind") == "folder" and (not files or facts.get("more_files")):
        rest = facts.get("more_files") or {}
        desc = ("Every file of the folder." if not files else
                "The folder's other files, not listed one by one: "
                + ", ".join(f"{n} × {k}" for k, n in sorted(rest.items())) + ".")
        distribution.append({"@type": "cr:FileSet", "@id": ids.new("files/all"), "name": "all files",
                             "includes": "**", "encodingFormat": "application/octet-stream",
                             "description": desc})
    if not distribution:
        distribution.append({"@type": "cr:FileSet", "@id": ids.new("files/all"), "name": name,
                             "includes": _glob_escape(name), "encodingFormat": "application/octet-stream"})
    doc["distribution"] = distribution
    record_sets = _record_sets(tables, ids, ledger)
    if record_sets:
        doc["recordSet"] = record_sets

    facets = _facets(about_terms, accessions, fmts)
    columns = [c for _, f, _ in tables for c in (f["content"]["table"].get("columns") or [])][:400]
    search_text = " ".join([name, description, *keywords, *(str(a["id"]) for a in accessions),
                            *(x for v in facets.values() for x in v), *(f["path"] for f in files[:200]),
                            *columns, str(uf.get("citation") or ""), uuid]).lower()
    return Built(doc, ledger.entries, ledger.confidence(), facets, search_text)


def _keywords(uf: dict, about_terms: dict, fmts: list[formats.Format]) -> list[str]:
    words: list[str] = [str(k) for k in uf.get("keywords") or []]
    for terms in about_terms.values():
        words += [t.label if t else label for t, label in terms]
    words += [f.label for f in fmts if f.label != "File"]
    out: list[str] = []
    for w in words:
        w = w.strip()
        if w and w.lower() not in (x.lower() for x in out):
            out.append(w)
    return out[:40]


def _draft_description(facts: dict, fmts: list[formats.Format]) -> str:
    files = facts.get("files") or []
    name = str(facts.get("name") or "")
    size = human_size(facts.get("size_bytes"))
    if facts.get("kind") == "folder":
        count = facts.get("file_count")
        kinds = Counter(f.label for f in fmts)
        made = ", ".join(f"{n} {k}" for k, n in kinds.most_common(6))
        text = (f"Folder {name}/ with {count} files ({size})" if count else f"Folder {name}/ ({size})")
        text += f": {made}." if made else "."
    else:
        fmt = fmts[0] if fmts else formats.for_name(name)
        text = f"{fmt.label if fmt.label != 'File' else 'File'} {name} ({size})."
        if files:
            extra = _content_description(fmt, files[0].get("content"))
            if extra:
                text += " " + extra
    derived = facts.get("derived_from") or {}
    if derived.get("run_id"):
        text += f" Saved from the result of AiScientist run {derived['run_id']}."
    return f"{text} {DRAFT_NOTE}"


def _responsible_ai(doc: dict, uf: dict, ledger: _Ledger, did: str) -> None:
    """The RAI fields for data about people. Filled only when the owner says the data is human."""
    if not uf.get("human_data"):
        return
    doc["conformsTo"] = [CROISSANT_1_1, RAI_1_0]
    consent = str(uf.get("consent") or "").strip()
    doc["rai:personalSensitiveInformation"] = (
        "Derived from human subjects. " + (f"Consent: {consent}" if consent else
                                           "How consent was obtained is not recorded."))
    ledger.add(did, "rai:personalSensitiveInformation", "human data", SOURCE_OWNER, 1.0)
    restrictions = str(uf.get("use_restrictions") or "").strip()
    if restrictions:
        doc["rai:dataUseCases"] = restrictions
        doc["conditionsOfAccess"] = f"{PRIVATE_ACCESS_TEXT} Use restrictions: {restrictions}"
        ledger.add(did, "rai:dataUseCases", "(the owner's restrictions)", SOURCE_OWNER, 1.0)
    collection = str(uf.get("data_collection") or "").strip()
    if collection:
        doc["rai:dataCollection"] = collection
        ledger.add(did, "rai:dataCollection", "(the owner's text)", SOURCE_OWNER, 1.0)


# --- results ------------------------------------------------------------------------------------------------


def build_result(uuid: str, facts: dict, *, base_url: str = "") -> Built:
    """The ``croissant.json`` of a run's result: its files, and the provenance that links it to its
    inputs and its run (pipeline, version, parameters, models, timestamps)."""
    ledger = _Ledger()
    did = f"urn:uuid:{uuid}"
    ids = _Ids()
    run_id = str(facts.get("run_id") or "")
    question = " ".join(str(facts.get("question") or "").split())
    status = str(facts.get("status") or "")
    inputs = [i for i in facts.get("inputs") or [] if isinstance(i, dict) and i.get("uuid")]
    software = facts.get("software") or {}
    finished = str(facts.get("finished_at") or iso_now())
    files = list(facts.get("files") or [])
    fmts = [formats.refine(formats.for_name(f["path"]), f.get("content")) for f in files]

    name = f"Result of AiScientist run {run_id}" + (f": {question[:90]}" if question else "")
    desc = f"Analysis result of AiScientist run {run_id}"
    desc += f" ({status})." if status else "."
    if question:
        desc += f" Question: {question[:600]}"
        desc += "" if desc.endswith((".", "?", "!")) else "."
    if inputs:
        desc += " Inputs: " + "; ".join(f"{i.get('name') or i['uuid']} ({i.get('kind') or 'dataset'} {i['uuid']})"
                                        for i in inputs) + "."
    handed = facts.get("handed_on") or []
    if handed:
        where = ("Kept with the run on the server" if all(h.get("location") == "server" for h in handed)
                 else "Kept beside the run")
        desc += f" {where} (not in this bundle): " + "; ".join(
            f"{h.get('what')}: {h.get('name')} ({human_size(h.get('size_bytes'))})" for h in handed) + "."
    sw_label = f"{software.get('name') or 'AiScientist'} {software.get('version') or ''}".strip()
    if software.get("git_sha"):
        sw_label += f" (commit {str(software['git_sha'])[:12]})"
    activity: dict[str, Any] = {
        "@type": "prov:Activity",
        "@id": f"urn:aiscientist:run:{run_id}",
        "prov:label": f"AiScientist run {run_id}",
        "prov:used": [{"@id": f"urn:uuid:{i['uuid']}"} for i in inputs],
        "prov:wasAssociatedWith": [{"@type": "prov:SoftwareAgent", "prov:label": sw_label,
                                    "softwareVersion": str(software.get("version") or "")}]
        + [{"@type": "prov:SoftwareAgent", "prov:label": f"language model {m}"}
           for m in facts.get("models") or []],
        "aiscientist:pipeline": facts.get("pipeline") or {},
        "aiscientist:parameters": facts.get("parameters") or [],
    }
    if facts.get("started_at"):
        activity["prov:startedAtTime"] = str(facts["started_at"])
    activity["prov:endedAtTime"] = finished
    if status:
        activity["aiscientist:status"] = status
    owner_name = str(facts.get("owner") or "")
    doc: dict[str, Any] = {
        "@context": CONTEXT,
        "@type": "sc:Dataset",
        "@id": did,
        "conformsTo": CROISSANT_1_1,
        "name": name,
        "description": desc,
        "url": f"{base_url}/api/results/{run_id}/croissant.json" if base_url else did,
        "identifier": [did, f"urn:aiscientist:run:{run_id}"],
        "creator": {"@type": "sc:Person", "name": owner_name or "unknown"},
        "datePublished": _date(finished),
        "dateCreated": _date(str(facts.get("started_at") or finished)),
        "version": "1.0.0",
        "license": PRIVATE_LICENSE_TEXT,
        "conditionsOfAccess": PRIVATE_ACCESS_TEXT,
        "isAccessibleForFree": False,
        "keywords": _result_keywords(facts, fmts),
        "citeAs": f"{owner_name or 'AiScientist user'} ({_date(finished)[:4]}). {name}. AiScientist result {did}.",
        "prov:wasGeneratedBy": activity,
    }
    if inputs:
        doc["isBasedOn"] = [{"@id": f"urn:uuid:{i['uuid']}"} for i in inputs]
        doc["prov:wasDerivedFrom"] = [{"@id": f"urn:uuid:{i['uuid']}"} for i in inputs]
    data_types = sorted({f.data_iri for f in fmts if f.data_iri})
    if data_types:
        doc["additionalType"] = data_types
    for n, i in enumerate(inputs):
        ledger.add(did, f"prov:wasDerivedFrom[{n}]", i["uuid"], SOURCE_RUN, 1.0,
                   f"{i.get('kind') or 'dataset'} the run read: {i.get('name') or ''}".strip())
    ledger.add(did, "prov:wasGeneratedBy", run_id, SOURCE_RUN, 1.0)
    ledger.add(did, "prov:wasGeneratedBy.prov:wasAssociatedWith[0]", sw_label, SOURCE_RUN, 1.0)
    for n, m in enumerate(facts.get("models") or []):
        ledger.add(did, f"prov:wasGeneratedBy.prov:wasAssociatedWith[{n + 1}]", m, SOURCE_RUN, 1.0,
                   "a model the run called (from the run's LLM call log)")
    ledger.add(did, "license", "private", SOURCE_DEFAULT, 0.3,
               "Results are private to their owner; no license is granted.")

    distribution, tables = _distribution(files, ids, ledger, size_source=SOURCE_COMPUTED,
                                         sha_source=SOURCE_COMPUTED)
    if not distribution:
        distribution.append({"@type": "cr:FileSet", "@id": ids.new("files/all"), "name": "bundle",
                             "includes": "**", "encodingFormat": "application/octet-stream",
                             "description": "The run's bundle."})
    doc["distribution"] = distribution
    record_sets = _record_sets(tables, ids, ledger)
    if record_sets:
        doc["recordSet"] = record_sets

    facets = {"organism": [], "tissue": [], "assay": [], "accession": [],
              "format": sorted({f.label for f in fmts if f.label != "File"}),
              "input": [i["uuid"] for i in inputs]}
    tools = sorted({str(p.get("tool")) for p in facts.get("parameters") or [] if isinstance(p, dict)})
    search_text = " ".join([name, desc, run_id, uuid, *doc["keywords"], *tools,
                            *(str(i.get("name") or "") for i in inputs), *(f["path"] for f in files[:200])]).lower()
    return Built(doc, ledger.entries, ledger.confidence(), facets, search_text)


def _result_keywords(facts: dict, fmts: list[formats.Format]) -> list[str]:
    words = ["analysis result"]
    pipeline = facts.get("pipeline") or {}
    words += [str(p) for p in pipeline.get("presets") or []] + [str(s) for s in pipeline.get("skills") or []]
    words += sorted({str(p.get("tool")) for p in facts.get("parameters") or [] if isinstance(p, dict)})
    words += [f.label for f in fmts if f.label != "File"]
    out: list[str] = []
    for w in words:
        if w and w not in out:
            out.append(w)
    return out[:40]
