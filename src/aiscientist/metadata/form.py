"""The owner's review form: what may be entered, how it is checked, and how the form is shown.

The form edits only the descriptive fields that cannot be measured — description, organism,
tissue, cell types, assay, source accessions, license, citation, keywords, version, and for human
data the consent and use restrictions. Files, sizes, checksums and table structure are facts and
are not editable. Each field is shown with where its current value came from and how confident
the drafting was, so the owner knows what to check first.
"""

from __future__ import annotations

import re
from typing import Any

from . import vocab
from .croissant import DRAFT_NOTE, SOURCE_OWNER, UNSURE_BELOW, suggestions
from .facts import accession_info, iso_now

_TEXT_LIMITS = {"name": 200, "description": 5000, "citation": 2000, "version": 32, "consent": 2000,
                "use_restrictions": 2000, "data_collection": 2000}
_TERM_FIELDS = {"organism": "organism", "tissue": "tissue", "cell_type": "cell_type", "assay": "assay"}
_PATH = re.compile(r"(^|[\s\"'(=:])(/(data|home|dfs\d*|pub|tmp|scratch|opt|Users|var|srv|mnt)/|~/)")
_SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|aisci_[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY)")
_VERSION = re.compile(r"\d+\.\d+\.\d+")

FIELDS: tuple[dict, ...] = (
    {"key": "name", "label": "Name", "type": "text"},
    {"key": "description", "label": "Description", "type": "textarea",
     "hint": "What the data is, how it was produced, and anything a reader needs to reuse it."},
    {"key": "organism", "label": "Organism", "type": "terms", "vocab": "organism",
     "hint": "NCBI Taxonomy. Comma-separated; pick from the list or type a name."},
    {"key": "tissue", "label": "Tissue", "type": "terms", "vocab": "tissue", "hint": "UBERON."},
    {"key": "cell_type", "label": "Cell types", "type": "terms", "vocab": "cell_type",
     "hint": "Cell Ontology. Only if the data is restricted to some cell types."},
    {"key": "assay", "label": "Assay", "type": "terms", "vocab": "assay", "hint": "EFO."},
    {"key": "accessions", "label": "Source accessions", "type": "list",
     "hint": "GEO, SRA, BioProject, BioSample or ArrayExpress, e.g. GSE123456, GSM5676874, PRJNA123456."},
    {"key": "license", "label": "License", "type": "select", "vocab": "license"},
    {"key": "citation", "label": "Citation", "type": "textarea", "hint": "How to cite it: a reference or BibTeX."},
    {"key": "keywords", "label": "Keywords", "type": "list"},
    {"key": "version", "label": "Version", "type": "text", "hint": "MAJOR.MINOR.PATCH"},
    {"key": "human_data", "label": "Derived from human subjects", "type": "bool"},
    {"key": "consent", "label": "Consent", "type": "textarea", "show_if": "human_data",
     "hint": "How consent was obtained (e.g. the IRB protocol). Never a patient's name or identifier."},
    {"key": "use_restrictions", "label": "Use restrictions", "type": "textarea", "show_if": "human_data",
     "hint": "What the data may be used for (e.g. research use only, no re-identification)."},
    {"key": "data_collection", "label": "How the data was collected", "type": "textarea", "show_if": "human_data"},
)
_KEYS = {f["key"] for f in FIELDS}


def _check_text(key: str, value: str) -> str:
    value = value.strip()
    if _PATH.search(value):
        raise ValueError(f"{key}: remove the server path; name files relative to the dataset instead.")
    if _SECRET.search(value):
        raise ValueError(f"{key}: this looks like a key or token; metadata must not hold credentials.")
    return value


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;\n]", value)
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return [str(v).strip() for v in value if str(v).strip()]


def clean_user_fields(fields: dict) -> dict:
    """Check and normalise a submitted form. Only known fields are taken; a field that is present
    is the owner's decision even when empty (an empty organism list means "none", not "unknown").
    Raises ``ValueError`` with a message for the owner."""
    out: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if key not in _KEYS:
            continue
        if key in _TEXT_LIMITS:
            text = _check_text(key, str(value or ""))
            if len(text) > _TEXT_LIMITS[key]:
                raise ValueError(f"{key}: at most {_TEXT_LIMITS[key]} characters.")
            if key == "version" and text and not _VERSION.fullmatch(text):
                raise ValueError("version: use MAJOR.MINOR.PATCH, e.g. 1.0.0.")
            if key == "name" and not text:
                raise ValueError("name: a dataset needs a name.")
            out[key] = text
        elif key in _TERM_FIELDS:
            terms = []
            for item in _list(value)[:20]:
                item = _check_text(key, item)[:120]
                term = vocab.resolve(_TERM_FIELDS[key], item)
                terms.append({"id": term.id, "label": term.label} if term else {"id": "", "label": item})
            out[key] = terms
        elif key == "accessions":
            accs = []
            for item in _list(value)[:50]:
                info = accession_info(item.upper() if not item.startswith("E-") else item)
                if info is None:
                    raise ValueError(f"accessions: {item!r} is not a GEO, SRA, ENA, BioProject, BioSample "
                                     "or ArrayExpress accession.")
                accs.append(info["id"])
            out[key] = accs
        elif key == "keywords":
            out[key] = [_check_text(key, k)[:80] for k in _list(value)[:30]]
        elif key == "license":
            lic = vocab.license_entry(str(value or "")) if value else None
            if value and lic is None:
                raise ValueError("license: choose one from the list.")
            out[key] = lic["id"] if lic else ""
        elif key == "human_data":
            out[key] = bool(value)
    out["_reviewed_at"] = iso_now()
    return out


def _entry_for(provenance: list[dict], *prefixes: str) -> "dict | None":
    for p in provenance:
        if any(p.get("path") == pre or str(p.get("path", "")).startswith(pre + "[") for pre in prefixes):
            return p
    return None


def form_view(facts: dict, user_fields: dict, doc: dict, provenance: list, confidence: dict) -> dict:
    """The review form as the web page draws it: every field with its current value and, for a value
    the owner has not set, where it came from and how sure the drafting was."""
    prov = provenance if isinstance(provenance, list) else []
    drafted = suggestions(facts)
    values: dict[str, Any] = {
        "name": doc.get("name") or "",
        # the draft, without the line that says it is one: saving the form makes it the owner's text
        "description": user_fields.get("description", "") if "description" in user_fields
        else str(doc.get("description", "")).replace(DRAFT_NOTE, "").strip(),
        "accessions": user_fields.get("accessions") if "accessions" in user_fields
        else [s for s in (facts.get("accessions") or [])],
        "license": user_fields.get("license", ""),
        "citation": user_fields.get("citation", ""),
        "keywords": user_fields.get("keywords", []),
        "version": user_fields.get("version", "") or doc.get("version", ""),
        "human_data": bool(user_fields.get("human_data")),
        "consent": user_fields.get("consent", ""),
        "use_restrictions": user_fields.get("use_restrictions", ""),
        "data_collection": user_fields.get("data_collection", ""),
    }
    for key, vname in _TERM_FIELDS.items():
        if key in user_fields:
            values[key] = [t.get("label") or t.get("id") for t in user_fields.get(key) or []]
        else:
            values[key] = [t.label for t, *_ in drafted.get(vname, [])]
    sources: dict[str, dict] = {}
    for f in FIELDS:
        key = f["key"]
        if key in user_fields and key != "name":
            sources[key] = {"source": SOURCE_OWNER, "confidence": 1.0, "reviewed": True}
            continue
        if key == "name" and user_fields.get("name"):
            sources[key] = {"source": SOURCE_OWNER, "confidence": 1.0, "reviewed": True}
            continue
        if key in _TERM_FIELDS and drafted.get(_TERM_FIELDS[key]):
            best = drafted[_TERM_FIELDS[key]]
            sources[key] = {"source": best[0][1], "confidence": min(c for _, _, c, _ in best),
                            "detail": "; ".join(why for _, _, _, why in best), "reviewed": False}
            continue
        entry = {"description": _entry_for(prov, "description"), "license": _entry_for(prov, "license"),
                 "accessions": _entry_for(prov, "sameAs"), "citation": _entry_for(prov, "citeAs"),
                 "version": _entry_for(prov, "version"), "name": _entry_for(prov, "name")}.get(key)
        if entry:
            sources[key] = {"source": entry.get("source"), "confidence": entry.get("confidence"),
                            "detail": entry.get("detail", ""), "reviewed": bool(entry.get("reviewed"))}
    files = facts.get("files") or []
    measured = {
        "kind": facts.get("kind"), "size_bytes": facts.get("size_bytes"),
        "file_count": facts.get("file_count"),
        "files_with_sha256": sum(1 for f in files if f.get("sha256")),
        "files_listed": len(files),
        "record_sets": [{"name": rs.get("name"), "fields": len(rs.get("field") or [])}
                        for rs in doc.get("recordSet") or []],
        "source": facts.get("source"),
    }
    # a drafted value below the confidence line, or a placeholder (the license) the owner must decide
    needs_review = [f["key"] for f in FIELDS if sources.get(f["key"], {}).get("confidence") is not None
                    and not sources[f["key"]].get("reviewed")
                    and float(sources[f["key"]]["confidence"]) < UNSURE_BELOW]
    return {"fields": [dict(f) for f in FIELDS], "values": values, "sources": sources,
            "measured": measured, "needs_review": needs_review}


def planner_note(facts: dict, user_fields: dict, doc: dict, confidence: dict, *, reviewed: bool) -> dict:
    """What the research planner is told about one bound dataset: a few lines, each marked as the
    owner's or as drafted, so the model weighs a guess as a guess."""
    def labels(key: str) -> list[str]:
        return [t.get("name", "") for t in doc.get(key) or [] if isinstance(t, dict)]
    owned = {k for k in ("description", "organism", "tissue", "cell_type", "assay", "accessions", "citation")
             if k in user_fields}
    return {
        "name": doc.get("name"), "uuid": str(doc.get("@id", "")).replace("urn:uuid:", ""),
        "reviewed": reviewed,
        "description": doc.get("description") if "description" in owned else "",
        "about": labels("about"), "assay": labels("measurementTechnique"),
        "terms_source": "owner" if owned & {"organism", "tissue", "cell_type", "assay"} else "drafted",
        "accessions": [str(x) for x in doc.get("identifier") or []][1:],
        "citation": doc.get("citeAs") if "citation" in owned else "",
        "formats": sorted({str(k) for k in doc.get("keywords") or []} & {
            "AnnData (h5ad)", "Loom", "HDF5", "Matrix Market", "CSV", "TSV", "VCF", "VCF (bgzip)", "BAM",
            "CRAM", "FASTQ", "BED", "GTF", "GFF3"}),
        "record_sets": [rs.get("name") for rs in doc.get("recordSet") or []][:10],
    }
