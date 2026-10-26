"""Controlled vocabularies the metadata links to: file formats (EDAM), organisms (NCBI Taxonomy),
tissues (UBERON), cell types (Cell Ontology), assays (EFO), and licenses (SPDX).

Every ID below was checked against the EBI Ontology Lookup Service (OLS4) when it was added; a test
pins the table's shape, and a new entry must be checked the same way before it is added — a made-up
ontology ID is worse than none, because a machine trusts it. A term the table does not hold can
still be entered in the review form: it is kept as a label, with no ID.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EDAM = "http://edamontology.org/"
OBO = "http://purl.obolibrary.org/obo/"
EFO_NS = "http://www.ebi.ac.uk/efo/"


@dataclass(frozen=True)
class Term:
    id: str        # CURIE, e.g. "NCBITaxon:9606"
    label: str
    synonyms: tuple[str, ...] = ()

    @property
    def iri(self) -> str:
        prefix, _, local = self.id.partition(":")
        if prefix == "EDAM":
            return EDAM + local
        if prefix == "EFO":
            return EFO_NS + "EFO_" + local
        return f"{OBO}{prefix}_{local}"

    @property
    def ontology(self) -> str:
        return self.id.split(":", 1)[0]

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "iri": self.iri}


# --- organisms: NCBI Taxonomy ---------------------------------------------------------------------------

ORGANISMS: tuple[Term, ...] = (
    Term("NCBITaxon:9606", "Homo sapiens", ("human", "hg19", "hg38", "grch37", "grch38", "t2t", "chm13")),
    Term("NCBITaxon:10090", "Mus musculus", ("mouse", "mm10", "mm39", "grcm38", "grcm39")),
    Term("NCBITaxon:10116", "Rattus norvegicus", ("rat", "rn6", "rn7")),
    Term("NCBITaxon:7955", "Danio rerio", ("zebrafish", "danrer11", "grcz11")),
    Term("NCBITaxon:9544", "Macaca mulatta", ("rhesus macaque", "rhesus", "mmul10")),
    Term("NCBITaxon:9541", "Macaca fascicularis", ("cynomolgus macaque", "crab-eating macaque")),
    Term("NCBITaxon:9823", "Sus scrofa", ("pig", "porcine")),
    Term("NCBITaxon:9913", "Bos taurus", ("cow", "cattle", "bovine")),
    Term("NCBITaxon:9615", "Canis lupus familiaris", ("dog", "canine")),
    Term("NCBITaxon:9986", "Oryctolagus cuniculus", ("rabbit",)),
    Term("NCBITaxon:9031", "Gallus gallus", ("chicken",)),
    Term("NCBITaxon:8355", "Xenopus laevis", ("xenopus", "african clawed frog")),
    Term("NCBITaxon:7227", "Drosophila melanogaster", ("fruit fly", "drosophila")),
    Term("NCBITaxon:6239", "Caenorhabditis elegans", ("c. elegans", "worm")),
)

# --- tissues and anatomy: UBERON (the eye first) ---------------------------------------------------------

TISSUES: tuple[Term, ...] = (
    Term("UBERON:0000966", "retina", ("retinal",)),
    Term("UBERON:0000970", "eye", ("ocular", "eyeball")),
    Term("UBERON:0001782", "pigmented layer of retina", ("retinal pigment epithelium", "rpe")),
    Term("UBERON:0000053", "macula lutea", ("macula",)),
    Term("UBERON:0001786", "fovea centralis", ("fovea",)),
    Term("UBERON:0001776", "optic choroid", ("choroid",)),
    Term("UBERON:0000964", "cornea", ("corneal",)),
    Term("UBERON:0000965", "lens of camera-type eye", ("lens",)),
    Term("UBERON:0000941", "cranial nerve II", ("optic nerve",)),
    Term("UBERON:0001773", "sclera", ("scleral",)),
    Term("UBERON:0001769", "iris", ()),
    Term("UBERON:0001775", "ciliary body", ()),
    Term("UBERON:0005969", "eye trabecular meshwork", ("trabecular meshwork",)),
    Term("UBERON:0000955", "brain", ()),
    Term("UBERON:0000948", "heart", ()),
    Term("UBERON:0002107", "liver", ()),
    Term("UBERON:0002048", "lung", ()),
    Term("UBERON:0002113", "kidney", ()),
    Term("UBERON:0000178", "blood", ()),
)

# --- cell types: Cell Ontology ------------------------------------------------------------------------

CELL_TYPES: tuple[Term, ...] = (
    Term("CL:0000604", "retinal rod cell", ("rod", "rods", "rod photoreceptor")),
    Term("CL:0000573", "retinal cone cell", ("cone", "cones")),
    Term("CL:0000740", "retinal ganglion cell", ("rgc",)),
    Term("CL:0000636", "Mueller cell", ("muller glia", "müller glia", "muller cell")),
    Term("CL:0000748", "retinal bipolar neuron", ("bipolar cell",)),
    Term("CL:0000561", "amacrine cell", ()),
    Term("CL:0000745", "retina horizontal cell", ("horizontal cell",)),
    Term("CL:0002586", "retinal pigment epithelial cell", ("rpe cell",)),
    Term("CL:0000129", "microglial cell", ("microglia",)),
    Term("CL:0000127", "astrocyte", ()),
    Term("CL:0000115", "endothelial cell", ()),
    Term("CL:0000235", "macrophage", ()),
    Term("CL:0000236", "B cell", ()),
    Term("CL:0000084", "T cell", ()),
)

# --- assays: EFO -----------------------------------------------------------------------------------------

ASSAYS: tuple[Term, ...] = (
    Term("EFO:0008913", "single-cell RNA sequencing", ("scrna-seq", "scrnaseq", "single cell rna-seq")),
    Term("EFO:0009809", "single nucleus RNA sequencing", ("snrna-seq", "snrnaseq", "single-nucleus rna-seq")),
    Term("EFO:0030003", "10x 3' transcription profiling", ("10x 3'", "chromium 3'")),
    Term("EFO:0009922", "10x 3' v3", ("10x v3", "chromium 3' v3")),
    Term("EFO:0008931", "Smart-seq2", ("smartseq2",)),
    Term("EFO:0008896", "RNA-Seq", ("bulk rna-seq", "rnaseq")),
    Term("EFO:0007045", "ATAC-seq", ()),
    Term("EFO:0010891", "scATAC-seq", ("single-cell atac-seq",)),
    Term("EFO:0010961", "Visium Spatial Gene Expression", ("visium",)),
    Term("EFO:0008994", "spatial transcriptomics", ()),
    Term("EFO:0005396", "exome sequencing", ("wes", "whole exome sequencing")),
    Term("EFO:0003744", "whole genome shotgun sequencing", ("wgs", "whole genome sequencing")),
    Term("EFO:0002692", "ChIP-seq", ()),
)

# --- licenses: SPDX ---------------------------------------------------------------------------------------

LICENSE_PRIVATE = "private"
LICENSES: tuple[dict, ...] = (
    {"id": LICENSE_PRIVATE, "label": "Private: no reuse without the owner's permission", "url": ""},
    {"id": "CC-BY-4.0", "label": "Creative Commons Attribution 4.0",
     "url": "https://creativecommons.org/licenses/by/4.0/"},
    {"id": "CC0-1.0", "label": "CC0 1.0 (public domain dedication)",
     "url": "https://creativecommons.org/publicdomain/zero/1.0/"},
    {"id": "CC-BY-NC-4.0", "label": "Creative Commons Attribution-NonCommercial 4.0",
     "url": "https://creativecommons.org/licenses/by-nc/4.0/"},
    {"id": "CC-BY-SA-4.0", "label": "Creative Commons Attribution-ShareAlike 4.0",
     "url": "https://creativecommons.org/licenses/by-sa/4.0/"},
    {"id": "ODbL-1.0", "label": "Open Data Commons Open Database License 1.0",
     "url": "https://opendatacommons.org/licenses/odbl/1-0/"},
)

VOCABULARIES: dict[str, tuple[Term, ...]] = {
    "organism": ORGANISMS, "tissue": TISSUES, "cell_type": CELL_TYPES, "assay": ASSAYS,
}

_ONTOLOGY_SETS = {
    "NCBITaxon": "http://purl.obolibrary.org/obo/ncbitaxon.owl",
    "UBERON": "http://purl.obolibrary.org/obo/uberon.owl",
    "CL": "http://purl.obolibrary.org/obo/cl.owl",
    "EFO": "http://www.ebi.ac.uk/efo/efo.owl",
    "EDAM": "http://edamontology.org",
}


def defined_term_set(term: Term) -> str:
    return _ONTOLOGY_SETS.get(term.ontology, "")


def _norm(text: str) -> str:
    return re.sub(r"[\s_]+", " ", (text or "").strip().lower().replace("’", "'"))


def resolve(vocab: str, value: str) -> "Term | None":
    """The term ``value`` names in ``vocab``: by ID (``NCBITaxon:9606``, also ``NCBITaxon_9606`` or
    the IRI), by label, or by a listed synonym. None when it names none."""
    v = _norm(value)
    raw = (value or "").strip().lower()
    if not v:
        return None
    for t in VOCABULARIES.get(vocab, ()):
        if raw in (t.id.lower(), t.id.lower().replace(":", "_"), t.iri.lower()) or v == _norm(t.label) \
                or v in (_norm(s) for s in t.synonyms):
            return t
    return None


def license_entry(value: str) -> "dict | None":
    v = _norm(value)
    for lic in LICENSES:
        if v and v in (lic["id"].lower(), _norm(lic["label"]), lic["url"].lower()):
            return lic
    return None


def mentions(vocab: str, text: str) -> list[Term]:
    """Terms of ``vocab`` whose label or synonym appears as a whole word in ``text`` (a file or folder
    name). Used only for low-confidence suggestions the owner is asked to check."""
    t = " " + re.sub(r"[^a-z0-9']+", " ", (text or "").lower()) + " "
    found = []
    for term in VOCABULARIES.get(vocab, ()):
        for word in (term.label, *term.synonyms):
            w = " " + re.sub(r"[^a-z0-9']+", " ", word.lower()).strip() + " "
            if len(w.strip()) >= 3 and w in t:
                found.append(term)
                break
    return found


def public_vocabulary() -> dict:
    return {**{k: [t.public() for t in v] for k, v in VOCABULARIES.items()},
            "license": [dict(x) for x in LICENSES]}
