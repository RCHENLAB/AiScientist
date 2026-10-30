"""DATASET.md: the description of a dataset that the PI writes the first time it reads it.

RCHENLAB/AiScientist issue 10, part 3. Before planning, the PI reads a profile of the dataset (the
run-start profile, ``tools/datasets.run_dataset_smoke_analysis``: shape, metadata columns and their
levels, cells per arm, species read off the gene identifiers, Cell Ranger facts). What it made of the
data used to be thrown away after the plan. This module turns it into a description that is kept
with the dataset, in the shape of a *Scientific Data* Data Descriptor with the fields MINSEQE and the
CELLxGENE schema ask for, in eight sections (:data:`SECTIONS`).

Every statement is marked by where it comes from:

- **read from the data** — built here, in code, from the profile and the registry record (files,
  sizes, checksums). Every number in the description is one of these;
- **inferred** — the PI's reading (one model call, :func:`describe_messages`). A statement that
  gives a number the profile does not contain is left out (:func:`untraced_numbers`), and the last
  section says how many were;
- **entered by the owner** — the review form of issue 9 (organism, tissue, cell type, assay,
  accessions, description);
- a field the data does not establish says **not recorded**; nothing is guessed.

One source with ``croissant.json`` (issue 9): the record keeps the profile and the PI's statements
beside the facts and the owner's fields (``MetadataRecord.description``); both documents are built
from that record, and the croissant ``description`` is this description's summary when the owner has
written none (:func:`summary_text`). The owner may edit the Markdown itself: :func:`owner_summary`
reads the summary back out of it, and an edit is never overwritten by a later run.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from . import vocab
from .facts import human_size

SECTIONS = ("Summary", "Biological source", "Experimental design", "Assay and processing",
            "Data records", "Quality and caveats", "What the data supports",
            "About this description")
DATA, INFERRED, OWNER, NONE = "data", "inferred", "owner", "none"
MARK = {DATA: "read from the data", INFERRED: "inferred", OWNER: "entered by the owner"}
#: The version of the description's structure; a stored one of another version is written again.
VERSION = 1

_CELLTYPE_COL = re.compile(
    r"(?i)^(cell[_.]?type|major[_.]?class|sub[_.]?class|cell[_.]?class|cell[_.]?ontology"
    r"(?:[_.]?class)?|cell[_.]?label|celltype[_.]?annotation|annotation|predicted[_.]?label|"
    r"cell[_.]?identity)s?$")
_QC_COL = re.compile(
    r"(?i)^(df[_.]?classification|doublet|scrublet|is[_.]?doublet|pann|percent[_.]?mt|pct[_.]?"
    r"counts?[_.]?mt|n[_.]?count|n[_.]?feature|nuclear[_.]?fraction|qc[_.]?)\w*s?$")
_FIELD_COLS = {
    "Developmental stage": re.compile(r"(?i)(^|[_.])(age|stage|dev(elopment(al)?)?[_.]?stage|"
                                      r"time[_.]?point|day|postnatal)([_.]|$)"),
    "Disease or condition": re.compile(r"(?i)(^|[_.])(disease|condition|genotype|treatment|"
                                       r"group|status|phenotype)([_.]|$)"),
    "Sex": re.compile(r"(?i)(^|[_.])(sex|gender)([_.]|$)"),
}
_PROFILE_KEYS = ("dataset_kind", "format", "cells", "genes", "obs_keys", "var_keys", "obs_categoricals",
                 "design_by_arm", "gene_symbols", "handed_on", "tissue_hint", "x_encoding", "x_shape",
                 "density", "nonzero_entries", "sampled_cells", "mean_total_counts_sample",
                 "mean_genes_detected_sample", "file_size_bytes", "tenx_h5", "n_samples",
                 "n_variants_sampled", "variant_count_truncated", "rows", "spots", "note",
                 "input_layout_hint")


# --- the profile ---------------------------------------------------------------------------------


def _scrub(value: Any, depth: int = 0) -> Any:
    """``value`` with server paths and over-long lists dropped, JSON-safe."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {str(k): _scrub(v, depth + 1) for k, v in value.items()
                if not str(k).endswith(("_path", "path")) and str(k) not in ("dataset_path",)}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in list(value)[:60]]
    if isinstance(value, str):
        return re.sub(r"(?<![\w.])/(?:data|home|dfs\d*|pub|tmp|scratch|Users|opt)/\S+", "<path>", value)[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:200]


def profile_snapshot(dataset_result: "dict | None") -> dict:
    """The part of the run-start profile the description is built from, without server paths."""
    dr = dataset_result if isinstance(dataset_result, dict) else {}
    return {k: _scrub(dr[k]) for k in _PROFILE_KEYS if k in dr and dr[k] not in (None, "", [], {})}


def profile_key(facts: dict) -> str:
    """What the description was written from: the dataset's files, sizes and checksums. A record
    whose key changed describes data that is no longer there, and the PI writes it again."""
    files = [(f.get("path"), f.get("size"), f.get("sha256")) for f in facts.get("files") or []
             if isinstance(f, dict)]
    basis = json.dumps([facts.get("size_bytes"), sorted(files, key=str)], default=str)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# --- numbers -------------------------------------------------------------------------------------

# A quantity: digits not glued to a letter (so v3, GRCh38, 10x, Smart-seq2 and CD4 are names, not
# numbers), with thousands separators and decimals; a prime, an apostrophe or a hyphenated word after
# it (3′, 5', 3-prime) makes it a chemistry name too. A sign is ignored: -1.2 is checked as 1.2.
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?"
                     r"(?![A-WYZa-wyz0-9_′'’]|[xX×][A-Za-z]|\.\d|-[A-Za-z])")
# "10x Genomics" and its products are a company's name, not a tenfold anything.
_TENX = re.compile(r"\b10[xX](?=\s*(?:Genomics|Chromium|Visium|Xenium|Multiome|single|scRNA|snRNA|3|5|v\d))")


def _canon(whole: str, frac: str = "") -> str:
    digits = whole.replace(",", "")
    frac = (frac or "").rstrip("0").rstrip(".") if frac else ""
    return digits.lstrip("0") or "0" if not frac or frac == "." else (digits.lstrip("0") or "0") + frac


def numbers_in(text: str) -> "list[str]":
    """The quantities written in ``text``, normalised (no thousands separators, no trailing zeros).
    A ratio is a quantity (``1.6x``); the company name ``10x Genomics`` is not."""
    return [_canon(m.group(1), m.group(2) or "") for m in _NUMBER.finditer(_TENX.sub("tenx", text or ""))]


def _forms(value: float) -> "set[str]":
    out = {numbers_in(f"{value:.0f}")[0]} if abs(value) < 1e15 else set()
    for places in (1, 2, 3):
        out.update(numbers_in(f"{value:.{places}f}"))
    if 0 <= value <= 1:                                  # a fraction may be written as a percent
        for places in (0, 1, 2):
            out.update(numbers_in(f"{value * 100:.{places}f}"))
    return out


def allowed_numbers(*sources: Any) -> "set[str]":
    """Every number the given profile, facts or text contain, in the forms a writer may round it to
    (a whole number, 1 to 3 decimals, a fraction as a percent), plus 0, 1 and 2."""
    out: set[str] = {"0", "1", "2"}

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            if v == v and abs(v) != float("inf"):
                out.update(_forms(float(v)))
                if isinstance(v, int):
                    out.add(str(abs(v)))
        elif isinstance(v, str):
            out.update(numbers_in(v))
        elif isinstance(v, dict):
            for k, x in v.items():
                out.update(numbers_in(str(k)))
                walk(x, depth + 1)
        elif isinstance(v, (list, tuple)):
            # The length of a list is not a fact about the data (a profile keeps the first 30 obs
            # columns); counts the profile means are stated as numbers (``n``).
            for x in v:
                walk(x, depth + 1)

    for s in sources:
        walk(s)
    return out


def untraced_numbers(text: str, allowed: "set[str]") -> "list[str]":
    """The numbers in ``text`` that are not among ``allowed``."""
    return [n for n in numbers_in(text) if n not in allowed]


# --- the PI's reading ----------------------------------------------------------------------------

_INFERRED_FIELDS = ("summary", "tissue", "cell_types", "developmental_stage", "condition", "sex",
                    "design", "technology", "reference", "processing")
_INFERRED_LISTS = ("caveats", "supports", "does_not_support")

DESCRIBE_SYSTEM = (
    "You are the lab's PI, reading a dataset for the first time, before any analysis is planned. "
    "Write what you can tell about it from its profile, for a data description that will be "
    "kept with the dataset and shown to its owner. The platform adds everything the profile states "
    "outright (shape, columns and their levels, cells per group, species from the gene identifiers, "
    "files and checksums): you write only your READING of it, and each of your statements is "
    "marked as inferred.\n\n"
    "Rules:\n"
    "- NUMBERS: write a number only if it appears in the profile exactly as given. Any statement "
    "with another number is deleted. Prefer no numbers at all.\n"
    "- Do not guess. A field the profile gives no evidence for is an empty string. Never invent an "
    "accession, a publication, a donor, a protocol version or a reference genome.\n"
    "- Say how you know: name the column, the gene identifiers or the file feature you read it from.\n"
    "- Technology and chemistry names are words (\"10x Chromium 3-prime\"), not numbers.\n"
    "- English, plain, one or two sentences per field.\n\n"
    "Reply with ONLY a JSON object with these keys (strings unless stated):\n"
    "summary: one paragraph — what the data is, organism, tissue, assay, and what was compared;\n"
    "tissue, cell_types, developmental_stage, condition, sex: what the data indicates, or \"\";\n"
    "design: which column holds the samples, which the condition or groups, and what that means "
    "for replication and batches;\n"
    "technology: the assay or platform the data indicates, or \"\";\n"
    "reference: the reference genome or annotation if the data indicates one, else \"\";\n"
    "processing: whether the matrix holds raw counts, normalised or log-transformed values, and "
    "what in the profile says so;\n"
    "caveats: a list of problems a user should know (missing metadata, unclear processing, a "
    "species or tissue mismatch, imbalanced depth);\n"
    "supports: a list of analyses this design allows;\n"
    "does_not_support: a list of analyses it does not allow, each with the reason.")


def describe_messages(name: str, profile: dict, facts: dict, user_fields: "dict | None" = None
                      ) -> "list[dict[str, str]]":
    """The one model call in which the PI writes its reading of the dataset."""
    files = [{"path": f.get("path"), "size": human_size(f.get("size")) if f.get("size") is not None else None}
             for f in (facts.get("files") or [])[:40] if isinstance(f, dict)]
    owner = {k: v for k, v in (user_fields or {}).items()
             if k in ("description", "organism", "tissue", "cell_type", "assay", "keywords")}
    body = {"dataset": name, "profile": profile, "files": files,
            **({"entered_by_the_owner": owner} if owner else {})}
    return [{"role": "system", "content": DESCRIBE_SYSTEM},
            {"role": "user", "content": json.dumps(body, ensure_ascii=False, default=str)[:24000]
             + "\n\nReturn the JSON object now."}]


def parse_inferred(raw: str) -> dict:
    """The PI's reply as ``{field: text}`` and ``{list field: [text]}``; ``{}`` when unreadable."""
    text = (raw or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        obj = json.loads(match.group(0) if match else text)
    except (ValueError, AttributeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _INFERRED_FIELDS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = " ".join(value.split())[:1200]
    for key in _INFERRED_LISTS:
        value = obj.get(key)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            items = [" ".join(str(v).split())[:600] for v in value if str(v).strip()]
            if items:
                out[key] = items[:8]
    return out


# --- the description -------------------------------------------------------------------------------


def _st(text: str, source: str, field: str = "") -> dict:
    return {"field": field, "text": text, "source": source}


def _fmt(n: Any) -> str:
    return f"{n:,}" if isinstance(n, int) and not isinstance(n, bool) else str(n)


def _cats(profile: dict) -> "dict[str, dict]":
    cats = profile.get("obs_categoricals") or {}
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in cats.items()} if isinstance(cats, dict) else {}


def _levels(info: dict, limit: int = 8) -> str:
    vals = [str(v) for v in (info.get("values") or [])]
    n = info.get("n")
    word = "level" if n == 1 else "levels"
    if not vals:
        return f"{_fmt(n)} {word}" if isinstance(n, int) else "levels not listed"
    shown = ", ".join(vals[:limit])
    more = f", … ({_fmt(n)} in all)" if isinstance(n, int) and n > limit else ""
    return f"{_fmt(n) + ' ' + word + ': ' if isinstance(n, int) else ''}{shown}{more}"


def _term_text(term: "vocab.Term | None", label: str = "") -> str:
    """``label (Ontology:ID)`` for a resolved term, the bare label otherwise."""
    if term is None:
        return label
    shown = label if label and label.lower() != term.label.lower() else term.label
    return f"{shown} ({term.label}, {term.id})" if shown != term.label else f"{term.label} ({term.id})"


def _owner_terms(user_fields: dict, key: str) -> "list[str]":
    vocab_name = {"cell_type": "cell_type"}.get(key, key)
    out = []
    for item in user_fields.get(key) or []:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            term = vocab.resolve(vocab_name, str(item.get("id") or "")) or vocab.resolve(vocab_name, label)
        else:
            label = str(item or "").strip()
            term = vocab.resolve(vocab_name, label)
        if label or term:
            out.append(_term_text(term, label))
    return out


def profile_terms(profile: dict) -> "dict[str, list[tuple[vocab.Term, str]]]":
    """Ontology terms the profile establishes: the organism from the gene identifiers, the tissue
    from the most abundant genes. ``{"organism": [(term, why)], "tissue": [...]}``. The croissant
    draft reads these too, so the two documents name the same organism and tissue."""
    out: dict[str, list[tuple[vocab.Term, str]]] = {"organism": [], "tissue": []}
    gs = profile.get("gene_symbols") if isinstance(profile.get("gene_symbols"), dict) else {}
    term = vocab.resolve("organism", str(gs.get("species_hint") or ""))
    if term is not None:
        out["organism"].append((term, f"species read off the gene identifiers ({gs.get('evidence') or 'profile'})"))
    hint = str(profile.get("tissue_hint") or "")
    for t in vocab.mentions("tissue", hint)[:2]:
        out["tissue"].append((t, "named by the most abundant genes in the run-start profile"))
    return out


def _with_terms(values: "list[str]", vocab_name: str, limit: int = 8) -> str:
    """Category values, each with its ontology term when one resolves (``Rod → CL:0000604``)."""
    shown = []
    for v in values[:limit]:
        term = vocab.resolve(vocab_name, v)
        shown.append(f"{v} ({term.id})" if term is not None else v)
    return ", ".join(shown)


def _filter(statements: "list[dict]", allowed: "set[str]", dropped: "list[dict]") -> "list[dict]":
    keep = []
    for s in statements:
        if s["source"] == INFERRED:
            bad = untraced_numbers(s["text"], allowed)
            if bad:
                dropped.append({"field": s.get("field") or "", "numbers": bad[:6]})
                continue
        keep.append(s)
    return keep


def build(*, name: str, profile: dict, facts: dict, user_fields: "dict | None" = None,
          inferred: "dict | None" = None, model: str = "", written_at: str = "",
          run_id: str = "") -> dict:
    """The structured description: ``{"version", "name", "sections": [{"title", "statements"}],
    "dropped", "model", "written_at", "run_id", "profile_key"}``. Each statement is
    ``{"field", "text", "source"}`` with ``source`` one of data / inferred / owner / none."""
    uf = dict(user_fields or {})
    inf = dict(inferred or {})
    allowed = allowed_numbers(profile, facts, uf)
    dropped: list[dict] = []
    cats = _cats(profile)
    design = profile.get("design_by_arm") if isinstance(profile.get("design_by_arm"), dict) else {}
    gs = profile.get("gene_symbols") if isinstance(profile.get("gene_symbols"), dict) else {}
    tenx = profile.get("tenx_h5") if isinstance(profile.get("tenx_h5"), dict) else {}
    handed = profile.get("handed_on") if isinstance(profile.get("handed_on"), dict) else {}
    label_cols = [c for c in cats if _CELLTYPE_COL.match(c)]
    design_cols = {c: i for c, i in cats.items() if not _CELLTYPE_COL.match(c) and not _QC_COL.match(c)
                   and isinstance(i.get("n"), int) and i["n"] <= 30}

    def inferred_st(key: str, field: str = "") -> "list[dict]":
        return [_st(inf[key], INFERRED, field)] if inf.get(key) else []

    # 1. Summary
    shape = []
    if profile.get("cells") and profile.get("genes"):
        shape.append(f"{_fmt(profile['cells'])} cells × {_fmt(profile['genes'])} genes")
    if profile.get("n_samples"):
        shape.append(f"{_fmt(profile['n_samples'])} sample(s)")
    if profile.get("n_variants_sampled"):
        shape.append(f"{_fmt(profile['n_variants_sampled'])}{'+' if profile.get('variant_count_truncated') else ''} "
                     "variants counted in a bounded read")
    n_files = len(facts.get("files") or [])
    where = (f"{n_files} file(s), {human_size(facts.get('size_bytes'))}" if facts.get("size_bytes")
             else f"{n_files} file(s)")
    kind = str(profile.get("dataset_kind") or profile.get("format") or "").replace("_", " ")
    summary = [_st(f"{name}: " + "; ".join([*shape, *( [kind] if kind else []), where]) + ".", DATA, "Shape")]
    if uf.get("description"):
        summary.append(_st(str(uf["description"]).strip(), OWNER, "Owner's description"))
    summary += inferred_st("summary")

    # 2. Biological source
    source: list[dict] = []
    terms = profile_terms(profile)
    owner_org = _owner_terms(uf, "organism")
    if owner_org:
        source.append(_st(", ".join(owner_org), OWNER, "Organism"))
    elif gs.get("species_hint"):
        how = ("Ensembl gene IDs" if gs.get("identifiers") == "ensembl"
               else f"{gs.get('symbol_case') or 'gene'} symbols")
        named = (_term_text(terms["organism"][0][0], str(gs["species_hint"])) if terms["organism"]
                 else str(gs["species_hint"]))
        source.append(_st(f"{named} — from the gene identifiers ({how}; {gs.get('evidence') or ''})"
                          .replace("; )", ")"), DATA, "Organism"))
    else:
        source.append(_st("not recorded", NONE, "Organism"))
    owner_tissue = _owner_terms(uf, "tissue")
    if owner_tissue:
        source.append(_st(", ".join(owner_tissue), OWNER, "Tissue"))
    elif profile.get("tissue_hint"):
        named = "; ".join(_term_text(t) for t, _ in terms["tissue"])
        hint = str(profile["tissue_hint"])
        # The profile's hint is written for the planner; the description keeps its evidence only.
        m = re.search(r"include (.+?), markers in the curated (\S+) reference", hint)
        why = (f"most likely, from abundance: the most abundant genes include {m.group(1)}, markers "
               f"of the curated {m.group(2)} reference; no tissue label came with the data"
               if m else hint.strip())
        source.append(_st((named + " — " if named else "") + why, DATA, "Tissue"))
    elif inf.get("tissue"):
        source += inferred_st("tissue", "Tissue")
    else:
        source.append(_st("not recorded", NONE, "Tissue"))
    owner_ct = _owner_terms(uf, "cell_type")
    if owner_ct:
        source.append(_st(", ".join(owner_ct), OWNER, "Cell types"))
    if label_cols:
        def labelled(c: str) -> str:
            info = cats[c]
            vals = [str(v) for v in (info.get("values") or [])]
            n = info.get("n")
            if not vals:
                return f"`{c}` ({_levels(info)})"
            more = f", … ({_fmt(n)} in all)" if isinstance(n, int) and n > 8 else ""
            return f"`{c}` ({_fmt(n)} labels: {_with_terms(vals, 'cell_type')}{more})"
        source.append(_st("labelled in " + "; ".join(labelled(c) for c in label_cols), DATA, "Cell types"))
    elif not owner_ct:
        source += inferred_st("cell_types", "Cell types") or [_st("not labelled in the data", NONE, "Cell types")]
    for field, pattern in _FIELD_COLS.items():
        cols = [c for c in cats if pattern.search(c) and c not in label_cols and not _QC_COL.match(c)]
        key = {"Developmental stage": "developmental_stage", "Disease or condition": "condition",
               "Sex": "sex"}[field]
        if not cols and field == "Disease or condition" and design.get("condition_column") in cats:
            cols = [str(design["condition_column"])]       # the column the profile compares by
        if cols:
            source.append(_st("; ".join(f"`{c}` ({_levels(cats[c])})" for c in cols), DATA, field))
            source += inferred_st(key, field)
        else:
            source += inferred_st(key, field) or [_st("not recorded", NONE, field)]

    # 3. Experimental design
    exp: list[dict] = []
    if design_cols:
        exp.append(_st("Metadata columns with few levels: " + "; ".join(
            f"`{c}` ({_levels(i, 6)})" for c, i in design_cols.items()) + ".", DATA, "Columns"))
        single = [c for c, i in design_cols.items() if i.get("n") == 1]
        if single:
            exp.append(_st(f"{', '.join(f'`{c}`' for c in single)} "
                           + ("holds a single value, so it separates" if len(single) == 1
                              else "hold a single value each, so they separate")
                           + " no samples or groups.", DATA, "Replication"))
    elif cats:
        exp.append(_st("No metadata column has few enough levels to be a sample or group column.",
                       DATA, "Columns"))
    else:
        exp.append(_st("not recorded: the profile lists no categorical metadata", NONE, "Columns"))
    arms = design.get("cells_by_arm") if isinstance(design.get("cells_by_arm"), dict) else {}
    if arms:
        exp.append(_st(f"Cells per level of `{design.get('condition_column')}`: "
                       + ", ".join(f"{a} {_fmt(n)}" for a, n in arms.items())
                       + (f" (of the first {_fmt(design.get('cells_scanned'))} cells read)"
                          if design.get("cells_scanned") and design.get("cells_total")
                          and design["cells_scanned"] < design["cells_total"] else "") + ".",
                       DATA, "Groups"))
    exp += inferred_st("design", "Design")

    # 4. Assay and processing
    assay: list[dict] = []
    owner_assay = _owner_terms(uf, "assay")
    if owner_assay:
        assay.append(_st(", ".join(owner_assay), OWNER, "Technology"))
    elif tenx:
        assay.append(_st("10x Genomics Cell Ranger feature-barcode matrix (HDF5)", DATA, "Technology"))
    elif profile.get("input_layout_hint"):
        assay.append(_st(str(profile["input_layout_hint"]), DATA, "Technology"))
    else:
        assay += inferred_st("technology", "Technology") or [_st("not recorded", NONE, "Technology")]
    genome = tenx.get("genome") or tenx.get("genomes")
    if genome:
        assay.append(_st(f"{genome} (recorded in the matrix file)", DATA, "Reference"))
    else:
        assay += inferred_st("reference", "Reference") or [_st("not recorded", NONE, "Reference")]
    if handed.get("processed"):
        counts = handed.get("counts_layer")
        assay.append(_st("quality-controlled and normalised by an earlier AiScientist run"
                         + (f" (run {handed.get('run_id')})" if handed.get("run_id") else "")
                         + ("; raw counts in `layers['counts']`" if "counts" in (handed.get("layers") or [])
                            else "") + (f" ({counts})" if counts else ""), DATA, "Processing"))
    elif tenx:
        assay.append(_st(str(profile.get("note") or "integer UMI counts as Cell Ranger writes them"),
                         DATA, "Processing"))
    else:
        if profile.get("mean_total_counts_sample") is not None and profile.get("sampled_cells"):
            assay.append(_st(f"Mean total of `X` per cell over the first {_fmt(profile['sampled_cells'])} "
                             f"cells read: {round(float(profile['mean_total_counts_sample']), 1)}"
                             + (f"; mean genes detected: {round(float(profile['mean_genes_detected_sample']), 1)}"
                                if profile.get("mean_genes_detected_sample") is not None else "") + ".",
                             DATA, "Processing"))
        assay += inferred_st("processing", "Processing") or [
            _st("not established from the profile", NONE, "Processing")]

    # 5. Data records
    records: list[dict] = []
    for f in (facts.get("files") or [])[:30]:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        bits = [human_size(f.get("size"))] if f.get("size") is not None else []
        if f.get("sha256"):
            bits.append(f"SHA-256 {f['sha256']}")
        records.append(_st(f"`{f['path']}`" + (f" — {', '.join(bits)}" if bits else ""), DATA, "File"))
    if len(facts.get("files") or []) > 30:
        records.append(_st(f"and {len(facts['files']) - 30} more files", DATA, "File"))
    contents = []
    if profile.get("x_encoding"):
        contents.append(f"`X` stored {profile['x_encoding']}"
                        + (f" (density {round(float(profile['density']), 4)})" if profile.get("density") is not None else ""))
    obs_keys = [k for k in (profile.get("obs_keys") or []) if not str(k).startswith("_")]
    if obs_keys:
        contents.append("obs columns: " + ", ".join(f"`{k}`" for k in obs_keys[:20]))
    for part in ("layers", "obsm"):
        if handed.get(part):
            contents.append(f"{part}: " + ", ".join(f"`{k}`" for k in handed[part][:12]))
    if contents:
        records.append(_st("; ".join(contents) + ".", DATA, "Contents"))
    if not records:
        records.append(_st("not recorded", NONE, "File"))

    # 6. Quality and caveats
    quality: list[dict] = []
    qc = design.get("qc_median_by_arm") if isinstance(design.get("qc_median_by_arm"), dict) else {}
    for col, per in list(qc.items())[:6]:
        if isinstance(per, dict) and per:
            quality.append(_st(f"Median `{col}` per group: " + ", ".join(f"{a} {v}" for a, v in per.items()) + ".",
                               DATA, "QC"))
    if design.get("depth_imbalance"):
        quality.append(_st(str(design["depth_imbalance"]), DATA, "Depth"))
    if design.get("snrna_hint"):
        quality.append(_st(str(design["snrna_hint"]), DATA, "Protocol"))
    if not design_cols and cats:
        quality.append(_st("No sample or condition column was found in the metadata.", DATA, "Missing metadata"))
    for item in inf.get("caveats") or []:
        quality.append(_st(item, INFERRED, "Caveat"))
    if not quality:
        quality.append(_st("no QC metrics are recorded in the data", NONE, "QC"))

    # 7. What the data supports
    supports: list[dict] = []
    if design_cols and all(i.get("n") == 1 for i in design_cols.values()):
        supports.append(_st("No column separates samples or groups, so there is no condition "
                            "contrast: no differential expression between conditions, and no "
                            "pseudobulk test.", DATA, "Does not support"))
    if label_cols:
        supports.append(_st(f"Analyses per labelled cell type (`{label_cols[0]}`) without clustering "
                            "the cells again.", DATA, "Supports"))
    for item in inf.get("supports") or []:
        supports.append(_st(item, INFERRED, "Supports"))
    for item in inf.get("does_not_support") or []:
        supports.append(_st(item, INFERRED, "Does not support"))
    if not supports:
        supports.append(_st("not established", NONE, "Supports"))

    sections = [summary, source, exp, assay, records, quality, supports]
    sections = [_filter(s, allowed, dropped) for s in sections]
    for i, sec in enumerate(sections):
        if not sec:
            sections[i] = [_st("not established", NONE, "")]
    return {"version": VERSION, "name": name, "model": model, "written_at": written_at,
            "run_id": run_id, "profile_key": profile_key(facts), "dropped": dropped,
            "sections": [{"title": t, "statements": s} for t, s in zip(SECTIONS, sections)],
            "evidence": _evidence(profile)}


# What each part of the profile is, in words, for "About this description".
_EVIDENCE_WORDS = (
    (("cells", "genes", "x_shape", "rows", "spots"), "the matrix's shape"),
    (("obs_keys", "obs_categoricals"), "the metadata columns and their levels"),
    (("design_by_arm",), "cells and QC medians per group"),
    (("gene_symbols",), "the gene identifiers"),
    (("tissue_hint", "top_genes"), "the most abundant genes"),
    (("x_encoding", "density", "nonzero_entries", "sampled_cells", "mean_total_counts_sample",
      "mean_genes_detected_sample"), "a sample of the matrix's values"),
    (("handed_on",), "the record of an earlier AiScientist run in the file"),
    (("tenx_h5",), "the Cell Ranger matrix's own facts"),
    (("n_samples", "n_variants_sampled"), "the VCF's samples and a bounded count of its variants"),
)


def _evidence(profile: dict) -> "list[str]":
    return [words for keys, words in _EVIDENCE_WORDS if any(k in profile for k in keys)]


def from_record(rec: dict, facts: dict, user_fields: "dict | None" = None, *, name: str = "") -> dict:
    """The structured description of a stored record (``MetadataRecord.description``): built again
    from the profile and the PI's statements kept there, with the dataset's facts and its owner's
    fields as they are now, so a change in the review form shows in DATASET.md too."""
    return build(name=name or str(facts.get("name") or "dataset"), profile=rec.get("profile") or {},
                 facts=facts, user_fields=user_fields, inferred=rec.get("inferred") or {},
                 model=str(rec.get("model") or ""), written_at=str(rec.get("written_at") or ""),
                 run_id=str(rec.get("run_id") or ""))


# --- Markdown ------------------------------------------------------------------------------------


def _line(s: dict) -> str:
    text = str(s.get("text") or "").strip()
    field = str(s.get("field") or "")
    source = s.get("source")
    head = f"**{field}:** " if field and field not in ("Shape",) else ""
    if source == NONE:
        return f"- {head}_{text or 'not recorded'}_"
    return f"- {head}{text} _({MARK.get(source, source)})_"


def render(desc: dict) -> str:
    """DATASET.md from a structured description (:func:`build`)."""
    name = str(desc.get("name") or "dataset")
    out = [f"# {name}", "",
           "Each statement is marked _read from the data_ (from the dataset's profile and its "
           "record: every number is one of these), _inferred_ (the PI's reading, to be checked) or "
           "_entered by the owner_. A field the data does not establish is marked as such.", ""]
    for i, sec in enumerate(desc.get("sections") or [], 1):
        out.append(f"## {i}. {sec.get('title')}")
        out.append("")
        statements = sec.get("statements") or []
        if sec.get("title") == "Summary":
            for s in statements:
                tag = "" if s.get("source") == NONE else f" _({MARK.get(s.get('source'), s.get('source'))})_"
                out += [f"{str(s.get('text') or '').strip()}{tag}", ""]
            continue
        out += [_line(s) for s in statements] + [""]
    n = len(SECTIONS)
    out.append(f"## {n}. {SECTIONS[-1]}")
    out.append("")
    who = f"the PI ({desc['model']})" if desc.get("model") else "the PI"
    when = f" on {str(desc.get('written_at'))[:10]}" if desc.get("written_at") else ""
    run = f", in run {desc['run_id']}" if desc.get("run_id") else ""
    out.append(f"- Written by {who}{when}{run}, the first time a run read this dataset.")
    ev = [str(e) for e in desc.get("evidence") or []]
    out.append("- Evidence: the run-start profile of the dataset"
               + (f" ({'; '.join(ev)})" if ev else "")
               + ", and the registry record (file names, sizes, SHA-256 checksums).")
    dropped = desc.get("dropped") or []
    if dropped:
        out.append(f"- Left out: {len(dropped)} inferred statement(s) that gave numbers the profile "
                   "does not contain.")
    out.append("- The owner can edit this description; an edit is kept, and later runs read it "
               "instead of describing the dataset again.")
    return "\n".join(out).rstrip() + "\n"


def summary_text(desc: dict) -> str:
    """The summary paragraph, without marks: the croissant ``description`` when the owner wrote
    none, so the two documents say the same thing."""
    for sec in desc.get("sections") or []:
        if sec.get("title") == "Summary":
            parts = [str(s.get("text") or "").strip() for s in sec.get("statements") or []
                     if s.get("source") in (DATA, INFERRED)]
            return " ".join(p for p in parts if p)[:2000]
    return ""


def owner_summary(markdown: str) -> str:
    """The text of the owner's edited Summary section (marks removed), '' when there is none."""
    m = re.search(r"^##\s*(?:1\.\s*)?Summary\s*$(.*?)(?=^##\s|\Z)", markdown or "", re.M | re.S)
    if not m:
        return ""
    text = re.sub(r"[*_]\((?:read from the data|inferred|entered by the owner)\)[*_]", "", m.group(1))
    return " ".join(text.split())[:2000]


def statements(desc: dict, sources: "Iterable[str]" = (DATA, OWNER)) -> "list[str]":
    """The description's statements from ``sources``, one line each, for a planner prompt."""
    want = set(sources)
    return [f"{sec.get('title')}: {s.get('field') + ': ' if s.get('field') else ''}{s.get('text')}"
            for sec in desc.get("sections") or [] for s in sec.get("statements") or []
            if s.get("source") in want]
