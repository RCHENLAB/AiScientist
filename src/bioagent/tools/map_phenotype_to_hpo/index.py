"""The HPO ontology index — the CLOSED SET every mapped phenotype term must come from.

This is the grounding layer under :mod:`bioagent.tools.hpo_terms.mapper`. An LLM asked for "the HPO ID
for night blindness" will happily emit a well-formed, WRONG ID: ``HP:0000662`` is Nyctalopia, but
``HP:0000622`` — a transposition away — is Blurred vision. Both are real eye phenotypes, so nothing
downstream errors; the patient is simply phenotyped wrong. So the LLM is never allowed to author an ID
here: it may only SELECT from candidates this index retrieved, and every selection is re-validated
against the index before it leaves the module. Same closed-set pattern the report writer uses (see
``research_lab._grounding_facts``).

Two retrieval jobs:
  * :meth:`HpoIndex.search` — a clinical phrase ("worsening night vision") -> ranked real candidates.
  * :meth:`HpoIndex.validate` — an ID from anywhere (LLM, sheet, paper) -> current / obsolete
    (with its replacement) / unknown.

Source: the bundled ``hpo_lexicon.tsv.gz`` (see ``scripts/build_hpo_lexicon.py``), overridable with
``BIOAGENT_HPO_LEXICON`` — point it at a lexicon built from the LIRICAL data dir's own ``hp.json`` to
guarantee the mapper and LIRICAL agree on an ontology version.
"""
from __future__ import annotations

import gzip
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

_LEXICON = Path(__file__).resolve().parent / "hpo_lexicon.tsv.gz"

# A name from a looser synonym class (narrow/broad/related) is real recall but a weaker claim than the
# label or an exact synonym, so it is scored down and can never outrank an exact hit.
_OTHER_SYNONYM_WEIGHT = 0.72

# Candidate generation ignores tokens this common (they are in a large share of HPO names —
# "abnormality", "of", "the"); they still COUNT in scoring, they just don't pull in postings.
_COMMON_TOKEN_DF_RATIO = 0.02

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> list[str]:
    """A clinical phrase -> comparable tokens: lowercase, split on non-alphanumerics, naive
    de-pluralize (``dystrophies``/``rods`` -> ``dystrophy``/``rod``) so "cone dystrophies" matches
    "Cone dystrophy". Deliberately crude: this only has to bring a phrase close enough for the scorer
    and the LLM's closed-set pick to land the exact term."""
    out: list[str] = []
    for tok in _WORD_RE.findall((text or "").lower()):
        if len(tok) > 3 and tok.endswith("ies"):
            tok = tok[:-3] + "y"
        elif len(tok) > 3 and tok.endswith("es") and tok[-3] in "sxzh":
            tok = tok[:-2]
        elif len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.append(tok)
    return out


# The HPO release stamp, as it appears in both our lexicon header and an obographs hp.json:
# ``http://purl.obolibrary.org/obo/hp/releases/2026-06-23/hp.json``.
_RELEASE_RE = re.compile(r"hp/releases/(\d{4}-\d{2}-\d{2})/hp\.json")


def release_date(version: str) -> str:
    """``…/hp/releases/2026-06-23/hp.json`` -> ``2026-06-23`` (``""`` if absent/unrecognized)."""
    m = _RELEASE_RE.search(version or "")
    return m.group(1) if m else ""


def hp_json_release(hp_json_path: "str | Path") -> str:
    """The HPO release date of an obographs ``hp.json`` — e.g. LIRICAL's staged copy — WITHOUT parsing
    the 23 MB file. The stamp sits in the header (byte ~2 kB), so a single 1 MB read finds it; this is
    cheap enough to run on every LIRICAL call. ``""`` on any read error or if the stamp is absent."""
    try:
        with open(hp_json_path, "rb") as fh:
            head = fh.read(1 << 20)
    except OSError:
        return ""
    m = re.search(rb"hp/releases/(\d{4}-\d{2}-\d{2})/hp\.json", head)
    return m.group(1).decode("ascii") if m else ""


@dataclass(frozen=True)
class HpoTerm:
    id: str
    label: str
    exact_synonyms: tuple[str, ...] = ()
    other_synonyms: tuple[str, ...] = ()
    status: str = "current"          # "current" | "obsolete"
    replaced_by: str = ""            # obsolete terms only

    @property
    def is_current(self) -> bool:
        return self.status == "current"


@dataclass(frozen=True)
class HpoHit:
    """One retrieved candidate: the real term, how well it matched, and WHICH of its names matched
    (surfaced to the LLM and to Diagnostics — "matched on the synonym 'Retinitis pigmentosa'" is the
    reviewable part of a mapping)."""

    term: HpoTerm
    score: float
    matched_name: str


@dataclass
class _Name:
    term_idx: int
    text: str
    tokens: tuple[str, ...]
    weight: float


class HpoIndex:
    """An in-memory lexical index over the HPO lexicon. Build cost is ~0.3 s / ~20 MB for 19k terms,
    so callers should use the process-wide :func:`get_index` rather than constructing per call."""

    def __init__(self, terms: list[HpoTerm], version: str = "") -> None:
        self.version = version
        self.terms = terms
        self._by_id: dict[str, HpoTerm] = {t.id: t for t in terms}
        self._names: list[_Name] = []
        self._exact: dict[str, list[int]] = {}          # normalized whole name -> name indices
        self._postings: dict[str, list[int]] = {}       # token -> name indices
        self._idf: dict[str, float] = {}
        self._build()

    # -- construction ---------------------------------------------------------

    def _build(self) -> None:
        for idx, term in enumerate(self.terms):
            if not term.is_current:
                continue                                # obsolete terms are validated, never suggested
            names = [(term.label, 1.0)]
            names += [(s, 1.0) for s in term.exact_synonyms]
            names += [(s, _OTHER_SYNONYM_WEIGHT) for s in term.other_synonyms]
            for text, weight in names:
                tokens = tuple(normalize(text))
                if not tokens:
                    continue
                name_idx = len(self._names)
                self._names.append(_Name(idx, text, tokens, weight))
                self._exact.setdefault(" ".join(tokens), []).append(name_idx)
                for tok in set(tokens):
                    self._postings.setdefault(tok, []).append(name_idx)
        total = max(1, len(self._names))
        for tok, posting in self._postings.items():
            self._idf[tok] = math.log(1.0 + total / (1.0 + len(posting)))
        self._common = {tok for tok, p in self._postings.items()
                        if len(p) > total * _COMMON_TOKEN_DF_RATIO}

    # -- lookup ---------------------------------------------------------------

    def validate(self, hpo_id: str) -> dict:
        """An ID from anywhere -> its real status. ``{status: ok|obsolete|unknown|malformed}``, plus the
        canonical ``name`` (ours, never the caller's) and, for an obsolete term, ``replaced_by``. This
        is the deterministic gate: an ID that does not pass never reaches LIRICAL."""
        raw = str(hpo_id or "").strip().upper()
        if not re.fullmatch(r"HP:\d{7}", raw):
            return {"hpo_id": hpo_id, "status": "malformed",
                    "note": "not an HPO ID of the form HP:0000510"}
        term = self._by_id.get(raw)
        if term is None:
            return {"hpo_id": raw, "status": "unknown",
                    "note": f"no such term in HPO {self.version or '(bundled lexicon)'}"}
        if not term.is_current:
            return {"hpo_id": raw, "status": "obsolete", "name": term.label,
                    "replaced_by": term.replaced_by,
                    "note": (f"obsolete; replaced by {term.replaced_by}" if term.replaced_by
                             else "obsolete with no replacement recorded")}
        return {"hpo_id": raw, "status": "ok", "name": term.label}

    def search(self, phrase: str, k: int = 8, min_score: float = 0.34) -> list[HpoHit]:
        """A clinical phrase -> up to ``k`` ranked real HPO terms (best per term, current terms only).

        Scoring is IDF-weighted token overlap, symmetric (the F1 of query-coverage and name-coverage) so
        neither a long label swallowing a short phrase ("Abnormal electroretinogram" vs "ERG") nor a
        verbose phrase against a terse label wins on length alone. An exact normalized name match scores
        1.0 and short-circuits ranking — that is the deterministic path the LLM never has to arbitrate.
        """
        q_tokens = normalize(phrase)
        if not q_tokens:
            return []
        q_set = set(q_tokens)
        q_idf = {t: self._idf.get(t, math.log(1.0 + len(self._names))) for t in q_set}
        q_total = sum(q_idf.values()) or 1.0

        # Candidate names: those sharing a discriminative token with the query. Falling back to the
        # common tokens only when the query is nothing BUT common words keeps recall without scanning.
        cand: set[int] = set()
        for tok in q_set - self._common:
            cand.update(self._postings.get(tok, ()))
        if not cand:
            for tok in q_set:
                cand.update(self._postings.get(tok, ())[:2000])

        exact_key = " ".join(q_tokens)
        exact_names = set(self._exact.get(exact_key, ()))

        best: dict[int, HpoHit] = {}                    # term_idx -> its best-scoring name
        for name_idx in cand | exact_names:
            name = self._names[name_idx]
            if name_idx in exact_names:
                score = name.weight
            else:
                shared = q_set & set(name.tokens)
                if not shared:
                    continue
                shared_idf = sum(q_idf[t] for t in shared)
                n_total = sum(self._idf.get(t, 0.0) for t in set(name.tokens)) or 1.0
                q_cov = shared_idf / q_total
                n_cov = sum(self._idf.get(t, 0.0) for t in shared) / n_total
                if q_cov <= 0 or n_cov <= 0:
                    continue
                score = name.weight * (2 * q_cov * n_cov) / (q_cov + n_cov)
            if score < min_score:
                continue
            prev = best.get(name.term_idx)
            if prev is None or score > prev.score:
                best[name.term_idx] = HpoHit(self.terms[name.term_idx], score, name.text)
        # Ties broken by ID so a repeated call ranks identically (a mapping must be reproducible).
        hits = sorted(best.values(), key=lambda h: (-h.score, h.term.id))
        return hits[:k]


# --- loading -----------------------------------------------------------------


def parse_lexicon(text: str) -> tuple[list[HpoTerm], str]:
    """Parse the lexicon TSV -> (terms, hpo_version). The version rides in a ``# hpo_version <TAB> …``
    header line so every mapping can name the exact ontology release it came from."""
    terms: list[HpoTerm] = []
    version = ""
    for raw in text.splitlines():
        if raw.startswith("#"):
            if raw.startswith("# hpo_version\t"):
                version = raw.split("\t", 1)[1].strip()
            continue
        if not raw.strip():
            continue
        cells = raw.rstrip("\n").split("\t")
        if len(cells) < 6:
            cells += [""] * (6 - len(cells))
        terms.append(HpoTerm(
            id=cells[0].strip(),
            label=cells[1].strip(),
            exact_synonyms=tuple(s for s in cells[2].split("|") if s),
            other_synonyms=tuple(s for s in cells[3].split("|") if s),
            status=(cells[4].strip() or "current"),
            replaced_by=cells[5].strip(),
        ))
    return terms, version


def load_index(path: "str | Path | None" = None) -> HpoIndex:
    """Build an index from a lexicon file (bundled by default; ``BIOAGENT_HPO_LEXICON`` overrides)."""
    p = Path(path or os.environ.get("BIOAGENT_HPO_LEXICON") or _LEXICON)
    opener = gzip.open if str(p).endswith(".gz") else open
    with opener(p, "rt", encoding="utf-8") as fh:       # type: ignore[operator]
        terms, version = parse_lexicon(fh.read())
    return HpoIndex(terms, version=version)


_INDEX: "HpoIndex | None" = None


def get_index() -> HpoIndex:
    """The process-wide index (built once, ~0.3 s). Every mapper call goes through this."""
    global _INDEX
    if _INDEX is None:
        _INDEX = load_index()
    return _INDEX
