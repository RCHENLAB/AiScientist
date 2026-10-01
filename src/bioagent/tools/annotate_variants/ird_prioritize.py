"""Disease-model, gene-level variant tiering — the IRD prioritization layer.

Ports the lab pipeline's per-GENE rollup (see ``docs/ird_filter_spec.md``): after the base rarity
filter (population AF < 0.5%), variants are grouped by gene and the gene is assigned an inheritance
model from how many rare alleles it carries:

* **dominant**   — the gene has ≥1 variant at population AF ≤ ``dominant_af`` (default 1e-4), on an
  autosome. One ultra-rare hit is enough for a dominant model.
* **recessive**  — the gene has ≥2 variants at AF ≤ ``recessive_af`` (default 5e-3), on an autosome
  (a compound-heterozygous / homozygous-recessive candidate set). Phasing (cis/trans) is a later
  refinement; this is the frequency+count gate the lab applies first.
* **x_linked**   — a variant on chrX at AF ≤ ``dominant_af`` (hemizygous males need only one hit).

A variant "fits a disease model" if its gene qualifies for ≥1 model **and** the variant itself is at
or below that model's frequency threshold. This is a TIER/annotation used to rank the shortlist — NOT
a hard filter that drops everything else — so a rare protein-altering variant in a gene with no model
still survives, just ranked below the model-fitting ones.

Pure, dependency-free, and deterministic so it is trivially testable and reusable from the variant
post-processing step / ``clinical_variant_prioritization``. A missing/novel population AF (``None`` or
"") is treated as the RAREST possible (0.0) — absence from gnomAD is not evidence of commonness, and
the lab keeps novel variants.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

DOMINANT_AF = 1e-4
RECESSIVE_AF = 5e-3


def parse_af(value: Any) -> float:
    """Population AF as a float; a missing / non-numeric / novel value ⇒ 0.0 (treated as rarest)."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s in (".", "NA", "na", "None"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _chrom_of(v: dict) -> str:
    """Normalise a variant's chromosome to 'chrN' from a `chrom` field or a `location` 'chr:pos'."""
    c = v.get("chrom") or v.get("chromosome") or ""
    if not c:
        loc = str(v.get("location") or v.get("position") or "")
        c = loc.split(":", 1)[0] if ":" in loc else loc
    c = str(c).strip()
    if c and not c.lower().startswith("chr"):
        c = "chr" + c
    return c


def _gene_of(v: dict) -> str:
    return str(v.get("gene") or v.get("gene_symbol") or v.get("Gene") or "").strip()


def _is_x(chrom: str) -> bool:
    return chrom.lower() in ("chrx",)


def _is_autosome(chrom: str) -> bool:
    body = chrom[3:] if chrom.lower().startswith("chr") else chrom
    return body.isdigit()


def gene_models(
    variants: Iterable[dict],
    *,
    dominant_af: float = DOMINANT_AF,
    recessive_af: float = RECESSIVE_AF,
) -> dict[str, set[str]]:
    """Map each gene symbol → the set of inheritance models it qualifies for
    (subset of {"dominant", "recessive", "x_linked"}). See the module docstring for the rules.
    """
    by_gene: dict[str, list[float]] = defaultdict(list)
    gene_chrom: dict[str, str] = {}
    for v in variants:
        g = _gene_of(v)
        if not g:
            continue
        by_gene[g].append(parse_af(v.get("max_af", v.get("gnomAD_AF", v.get("filt_freq")))))
        gene_chrom.setdefault(g, _chrom_of(v))

    models: dict[str, set[str]] = {}
    for g, afs in by_gene.items():
        chrom = gene_chrom.get(g, "")
        m: set[str] = set()
        n_dominant = sum(1 for a in afs if a <= dominant_af)
        n_recessive = sum(1 for a in afs if a <= recessive_af)
        if _is_x(chrom):
            if n_dominant >= 1:
                m.add("x_linked")
        elif _is_autosome(chrom) or chrom == "":
            if n_dominant >= 1:
                m.add("dominant")
            if n_recessive >= 2:
                m.add("recessive")
        if m:
            models[g] = m
    return models


def annotate_disease_model(
    variants: list[dict],
    *,
    dominant_af: float = DOMINANT_AF,
    recessive_af: float = RECESSIVE_AF,
) -> list[dict]:
    """Return a NEW list of the variants, each with two added keys:

    * ``disease_model``     — sorted list of the models THIS variant fits (its gene qualifies for the
      model AND the variant's own AF is at/below that model's threshold); ``[]`` if none.
    * ``fits_disease_model``— bool, whether ``disease_model`` is non-empty.

    Does not drop anything; the caller ranks model-fitting variants ahead of the rest.
    """
    models = gene_models(variants, dominant_af=dominant_af, recessive_af=recessive_af)
    out: list[dict] = []
    for v in variants:
        g = _gene_of(v)
        af = parse_af(v.get("max_af", v.get("gnomAD_AF", v.get("filt_freq"))))
        gm = models.get(g, set())
        fit: list[str] = []
        if "dominant" in gm and af <= dominant_af:
            fit.append("dominant")
        if "recessive" in gm and af <= recessive_af:
            fit.append("recessive")
        if "x_linked" in gm and af <= dominant_af:
            fit.append("x_linked")
        nv = dict(v)
        nv["disease_model"] = sorted(fit)
        nv["fits_disease_model"] = bool(fit)
        out.append(nv)
    return out
