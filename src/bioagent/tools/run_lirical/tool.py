"""The ``run_lirical`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/phenotype_dx.py`` by ``scripts/refactor/split_tools.py``: the code is the old
module's text, verbatim, with only the imports rewritten.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


def _f(v: Any) -> "float | None":
    """Parse a LIRICAL numeric cell; accepts a fraction ('0.967') or a percentage ('96.7%')."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    try:
        x = float(s)
    except ValueError:
        return None
    return x / 100.0 if pct else x


@dataclass
class DiseaseCandidate:
    """One candidate disease in the differential. ``posttest_prob`` is LIRICAL's calibrated posterior
    (None when the candidate came only from the literature track — we never invent a probability). The
    ``evidence_*`` fields carry the literature track's ClinGen grade + citations, kept SEPARATE from the
    probability on purpose."""

    disease_name: str
    disease_id: str = ""                 # OMIM/ORPHA curie, e.g. "OMIM:613835"
    gene: str = ""                       # gene SYMBOL — LIRICAL's TSV does NOT emit this (see below)
    entrez_gene_id: str = ""             # e.g. "NCBIGene:24" — what genotype-aware LIRICAL DOES emit;
    #                                      reconcile keys on the symbol, so entrez->symbol must be mapped
    #                                      (LIRICAL's staged hgnc_complete_set.txt) before the merge.
    variants: str = ""                   # genotype-aware only: the scored variant(s) + pathogenicity + GT
    posttest_prob: "float | None" = None
    pretest_prob: "float | None" = None
    composite_lr: "float | None" = None
    matched_hpo: list[str] = field(default_factory=list)
    evidence_tier: str = ""              # ClinGen tier (literature) — NOT a probability
    evidence_pmids: list[str] = field(default_factory=list)
    evidence_status: str = ""            # graded | contradicted | unsupported | ungraded | "" (not asked)
    #                                      distinguishes "the corpus said nothing" (ungraded — must not
    #                                      count against a candidate) from "papers came back and none
    #                                      support it" (unsupported). See phenotype_evidence.grade_evidence.
    sources: set[str] = field(default_factory=set)     # {"lirical", "literature"}
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["sources"] = sorted(self.sources)
        return d


def parse_lirical_tsv(text: str) -> list[DiseaseCandidate]:
    """Parse LIRICAL's TSV output into ranked :class:`DiseaseCandidate` rows. LIRICAL prefixes metadata
    lines with ``!``; the first non-``!``/``#`` line is the header. Columns are mapped BY NAME
    (case-insensitive) so this survives minor version differences — verify the header names against your
    LIRICAL version (``diseaseName``/``diseaseCurie``/``posttestprob``/``pretestprob``/``compositeLR``)."""
    header: "list[str] | None" = None
    out: list[DiseaseCandidate] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("!") or s.startswith("#"):
            continue
        cells = line.rstrip("\n").split("\t")
        if header is None:
            header = [c.strip().lower() for c in cells]
            continue
        rec = dict(zip(header, cells))
        out.append(DiseaseCandidate(
            disease_name=rec.get("diseasename") or rec.get("disease") or "",
            disease_id=rec.get("diseasecurie") or rec.get("diseaseid") or "",
            # LIRICAL v2.4 TSV emits ``entrezGeneId`` (+ ``variants``) in genotype-aware mode and NO gene
            # column in phenotype-only mode — verified on HPC3. Keep the ``gene`` symbol lookup as a
            # fallback for other formats/versions, but expect the identity to arrive as entrez.
            gene=(rec.get("gene") or rec.get("genesymbol") or "").strip(),
            entrez_gene_id=(rec.get("entrezgeneid") or "").strip(),
            variants=(rec.get("variants") or "").strip(),
            posttest_prob=_f(rec.get("posttestprob") or rec.get("posttestprobability")),
            pretest_prob=_f(rec.get("pretestprob")),
            composite_lr=_f(rec.get("compositelr")),
            sources={"lirical"},
        ))
    return out


def hpo_release_drift(data_dir: str, lexicon_version: str) -> list[str]:
    """Do WE and LIRICAL speak the same HPO release? -> a note if not (``[]`` when they agree or either
    side is unknown).

    Our bundled lexicon (which resolves the free text) and LIRICAL's staged ``hp.json`` (which resolves
    the IDs into disease likelihoods) are updated by DIFFERENT hands: ours by re-running
    ``scripts/build_hpo_lexicon.py`` and committing, LIRICAL's by whoever next runs ``lirical download``.
    They match today (both 2026-06-23, byte-identical hp.json — verified on HPC3 2026-07-15), and nothing
    keeps them matched. Drift is SILENT and one-sided: a term we still map to may be obsolete in
    LIRICAL's newer ontology, and it would simply stop matching — no error, just a quieter differential.
    Cheap enough to check every run (the release stamp is in hp.json's header)."""
    if not data_dir:
        return []
    from ..map_phenotype_to_hpo.index import hp_json_release, release_date

    theirs = hp_json_release(str(Path(data_dir) / "hp.json"))
    ours = release_date(lexicon_version)
    if not theirs or not ours or theirs == ours:
        return []
    return [f"HPO release mismatch: the free-text→HPO mapper uses {ours}, LIRICAL's data dir has "
            f"{theirs}. Terms retired between the two will silently stop matching — regenerate the "
            f"lexicon with `python scripts/build_hpo_lexicon.py --hp-json {data_dir}/hp.json`."]


# --- LIRICAL input (Phenopacket) + CLI (pure, testable) ----------------------
# LIRICAL's assembly flag speaks hg19/hg38; the rest of the codebase (VEP) speaks GRCh37/GRCh38. Map so
# a caller can pass either. The Exomiser variant DB is chosen by this same axis (-e19 vs -e38).
_ASSEMBLY_ALIASES: dict[str, str] = {
    "grch37": "hg19", "hg19": "hg19", "b37": "hg19", "37": "hg19",
    "grch38": "hg38", "hg38": "hg38", "b38": "hg38", "38": "hg38",
}


def normalize_assembly(assembly: str) -> str:
    """A VCF/VEP assembly name → LIRICAL's ``hg19``/``hg38``. Unknown → ``hg38`` (LIRICAL's default)."""
    return _ASSEMBLY_ALIASES.get(str(assembly or "").strip().lower(), "hg38")


def vcf_uses_chr_prefix(vcf_path: str) -> bool:
    """True if the VCF's contigs / records use a ``chr`` prefix (``chr1``) rather than bare Ensembl names
    (``1``). Reads only the header + the first data record (never the whole WGS file): the first
    ``##contig`` decides, else the first record's CHROM. False on any read error (treat as bare)."""
    opener = gzip.open if str(vcf_path).endswith(".gz") else open
    try:
        with opener(vcf_path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("##contig="):
                    m = re.match(r"##contig=<ID=([^,>]+)", line)
                    if m:
                        return m.group(1).lower().startswith("chr")
                elif not line.startswith("#"):
                    return line[:1].isalnum() and line.lower().startswith("chr")
    except OSError:
        return False
    return False


def strip_chr_prefix(in_path: str, out_path: str) -> int:
    """Stream-rewrite a VCF removing a leading ``chr`` from BOTH the ``##contig`` header IDs and the CHROM
    column of each record (so header and records stay consistent for htsjdk). Memory-bounded (line by
    line — safe on a WGS-size VCF); returns the number of records rewritten. ``chrM`` → ``M`` (Exomiser
    hg19 mito is ``MT``; an unmatched ``M`` just drops the handful of mito calls, never IRD drivers)."""
    n = 0
    in_open = gzip.open if str(in_path).endswith(".gz") else open
    with in_open(in_path, "rt", encoding="utf-8", errors="replace") as fin, \
            open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            if line.startswith("##contig=<ID=chr"):
                fout.write("##contig=<ID=" + line[len("##contig=<ID=chr"):])
            elif line.startswith("#"):
                fout.write(line)
            elif line.startswith("chr"):
                fout.write(line[3:])
                n += 1
            else:
                fout.write(line)
    return n


def build_phenopacket(hpo_terms: Iterable[str], *, sample_id: str = "sample-1",
                      excluded: Iterable[str] = (), labels: "dict[str, str] | None" = None,
                      case_id: str = "bioagent-case", created: str = "2024-01-01T00:00:00Z",
                      hpo_version: str = "2023-10-09") -> dict[str, Any]:
    """A minimal GA4GH **Phenopacket v2** carrying the patient's observed (and negated) HPO terms — the
    stable, self-describing input we feed LIRICAL (concentrating the version-fragile surface into a few
    CLI flags instead of the phenotype list). ``labels`` optionally names each term (cosmetic; LIRICAL
    resolves labels from ``hp.json`` regardless). Excluded terms carry ``excluded: true`` so LIRICAL
    treats them as *absent* findings (which lower a disease's likelihood), not missing data."""
    labels = labels or {}
    feats: list[dict[str, Any]] = []
    for hp in hpo_terms:
        hp = str(hp).strip()
        if hp:
            feats.append({"type": {"id": hp, "label": labels.get(hp, "")}})
    for hp in excluded:
        hp = str(hp).strip()
        if hp:
            feats.append({"type": {"id": hp, "label": labels.get(hp, "")}, "excluded": True})
    return {
        "id": case_id,
        "subject": {"id": sample_id},
        "phenotypicFeatures": feats,
        "metaData": {
            "created": created,
            "createdBy": "bioagent",
            "resources": [{
                "id": "hp", "name": "human phenotype ontology", "namespacePrefix": "HP",
                "url": "http://purl.obolibrary.org/obo/hp.owl", "version": hpo_version,
                "iriPrefix": "http://purl.obolibrary.org/obo/HP_",
            }],
            "phenopacketSchemaVersion": "2.0",
        },
    }


def build_lirical_cmd(*, observed: Iterable[str], data_dir: str, output_dir: str,
                      prefix: str = "lirical", negated: Iterable[str] = (), assembly: str = "hg38",
                      vcf_path: str = "", exomiser_dir: str = "", sample_id: str = "",
                      output_format: str = "tsv", base_cmd: tuple[str, ...] = ("lirical",)) -> list[str]:
    """A LIRICAL v2.4 ``prioritize`` argv (CLI-args mode). ``observed`` / ``negated`` are the patient's
    HPO term IDs, passed comma-separated (``-p`` / ``-n``). Runs PHENOTYPE-ONLY (posterior from symptoms
    alone) unless BOTH a ``vcf_path`` AND the matching Exomiser ``exomiser_dir`` are given, in which case
    the variants are scored too (the genotype-aware posterior). LIRICAL runs phenotype-only when
    ``--assembly`` is unset, so we withhold ``--vcf``/``--assembly``/``-ed*`` together when the Exomiser
    data is absent rather than let it error.

    Flags are pinned to the LIRICAL version baked in ``deploy/lirical/lirical.def`` and were VERIFIED
    against ``lirical prioritize --help`` on HPC3 (2026-07-14, v2.4.1): ``-p`` observed / ``-n`` negated /
    ``-d`` data / ``-o`` outdir / ``-x`` prefix / ``-f`` format / ``--vcf`` / ``--assembly`` / ``-ed19``
    / ``-ed38`` Exomiser data DIRECTORY / ``--sample-id``. This is the SINGLE place to tune them if a
    future LIRICAL renames one (a runtime arg; no image rebuild). Kept pure so it is unit-tested without
    a live LIRICAL, exactly like ``vcf_offline.build_vep_cmd``."""
    obs = ",".join(h for h in (str(x).strip() for x in observed) if h)
    cmd = [*base_cmd, "prioritize",
           "-p", obs,
           "-d", data_dir,
           "-o", output_dir,
           "-x", prefix,
           "-f", output_format]
    neg = ",".join(h for h in (str(x).strip() for x in negated) if h)
    if neg:
        cmd += ["-n", neg]
    if vcf_path and exomiser_dir:
        asm = normalize_assembly(assembly)
        cmd += ["--vcf", vcf_path, "--assembly", asm,
                ("-ed19" if asm == "hg19" else "-ed38"), exomiser_dir]
        if sample_id:
            cmd += ["--sample-id", sample_id]
    return cmd


def run_lirical(*, hpo_terms: Iterable[str], vcf_path: str = "", data_dir: str = "",
                workspace: str = "", assembly: str = "hg38", excluded_hpo: Iterable[str] = (),
                sample_id: str = "sample-1", exomiser_hg19: str = "", exomiser_hg38: str = "",
                output_prefix: str = "lirical", base_cmd: tuple[str, ...] = ("lirical",),
                labels: "dict[str, str] | None" = None,
                exec_fn: "Callable[[list[str]], Any] | None" = None) -> dict[str, Any]:
    """Run LIRICAL (the PRIMARY track): write a phenopacket from the patient's HPO terms, invoke
    ``prioritize``, and parse the ranked differential. Injectable (``exec_fn``) so the build/parse are
    testable without a live LIRICAL — the real ``exec_fn`` runs inside ``lirical.sif`` on HPC3
    (:mod:`bioagent.tools.phenotype_cli`), the fallback is ``subprocess.run``.

    GATED: returns ``status='not_installed'`` until both ``data_dir`` (the staged LIRICAL data) and an
    ``exec_fn`` are supplied — i.e. until ``deploy/lirical/build_and_stage.sh`` has run. Scores
    GENOTYPE-AWARE when a ``vcf_path`` + the assembly-matched Exomiser DB are present, else
    PHENOTYPE-ONLY (still a valid per-disease posterior from the symptoms alone)."""
    hpo_terms = [str(h).strip() for h in hpo_terms if str(h).strip()]
    if not hpo_terms:
        return {"status": "error", "error": "LIRICAL needs at least one observed HPO term.",
                "candidates": []}

    # GATE: every incoming ID is checked against the real HPO release before it can define a patient's
    # phenotype. The IDs usually arrive from map_phenotype_to_hpo (already grounded), but nothing stops
    # the model from typing them itself — and a fabricated-but-well-formed ID (HP:0000622 for HP:0000662)
    # would silently score the WRONG phenotype and yield a confident, wrong differential. Unknown/
    # malformed IDs are dropped, obsolete ones forwarded to their replacement, and both are reported.
    phenotype_notes: list[str] = []
    try:
        from ..map_phenotype_to_hpo.tool import validate_hpo_ids

        checked = validate_hpo_ids(hpo_terms)
        checked_excl = validate_hpo_ids([str(h).strip() for h in excluded_hpo if str(h).strip()])
    except Exception:                                   # noqa: BLE001 - a lexicon problem must not block a run
        labels = labels or {}
    else:
        for r in checked["rejected"] + checked_excl["rejected"]:
            phenotype_notes.append(f"dropped {r['hpo_id']}: {r['note']}")
        for r in checked["remapped"] + checked_excl["remapped"]:
            phenotype_notes.append(f"{r['from']} is obsolete → used {r['to']} ({r['name']})")
        if not checked["valid"]:
            return {"status": "error", "candidates": [],
                    "error": ("none of the supplied HPO terms exist in "
                              f"{checked['hpo_version'] or 'the HPO release'}: "
                              + "; ".join(phenotype_notes)
                              + " — use map_phenotype_to_hpo on the clinical text instead of writing IDs."),
                    "phenotype_notes": phenotype_notes}
        hpo_terms = checked["valid"]
        excluded_hpo = checked_excl["valid"]
        # Label every term from the ONTOLOGY (it overrides the caller's wording) so the phenopacket
        # records what the ID actually means, not what the model thought it meant.
        labels = {**(labels or {}), **checked["labels"], **checked_excl["labels"]}
        phenotype_notes += hpo_release_drift(data_dir, checked["hpo_version"])

    if not data_dir or exec_fn is None:
        return {"status": "not_installed",
                "note": "LIRICAL not staged on HPC3 yet — run deploy/lirical/build_and_stage.sh.",
                "phenotype_notes": phenotype_notes, "candidates": []}

    asm = normalize_assembly(assembly)
    exomiser_dir = exomiser_hg19 if asm == "hg19" else exomiser_hg38
    genotype_aware = bool(vcf_path and exomiser_dir)

    ws = Path(workspace) if workspace else Path(".")
    out_dir = ws / "artifacts" / "phenotype"
    work = ws / "work"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"status": "error", "error": f"cannot create workspace dirs: {exc}", "candidates": []}

    # Genotype-aware only: DETECT a chr contig prefix and strip it just for LIRICAL when present (a bare
    # VCF is used as-is). Exomiser matches on bare Ensembl names, so a chr-prefixed WGS VCF would score
    # nothing without this. A strip failure is non-fatal — fall back to the original VCF.
    vcf_for_lirical = vcf_path if genotype_aware else ""
    chr_stripped = False
    if genotype_aware:
        try:
            if vcf_uses_chr_prefix(vcf_path):
                nochr = work / "nochr.vcf"
                strip_chr_prefix(vcf_path, str(nochr))
                vcf_for_lirical = str(nochr)
                chr_stripped = True
        except OSError:
            vcf_for_lirical = vcf_path

    # Write the case's HPO terms as a GA4GH Phenopacket for PROVENANCE (a standard, self-describing record
    # of exactly what defined the case) — LIRICAL v2.4 itself is driven by the -p/-n CLI args below, not
    # this file. Best-effort: a write failure must not abort the analysis.
    excluded_list = [str(h).strip() for h in excluded_hpo if str(h).strip()]
    try:
        pp = build_phenopacket(hpo_terms, sample_id=sample_id, excluded=excluded_list, labels=labels)
        (out_dir / "phenopacket.json").write_text(json.dumps(pp, indent=2), encoding="utf-8")
    except OSError:
        pass

    cmd = build_lirical_cmd(observed=hpo_terms, negated=excluded_list, data_dir=data_dir,
                            output_dir=str(out_dir), prefix=output_prefix, assembly=asm,
                            vcf_path=vcf_for_lirical,
                            exomiser_dir=exomiser_dir if genotype_aware else "",
                            sample_id=sample_id, base_cmd=base_cmd)
    proc = exec_fn(cmd)
    if getattr(proc, "returncode", 1) != 0:
        return {"status": "error", "error": f"LIRICAL failed: {_proc_tail(proc)}",
                "cmd": " ".join(cmd), "candidates": []}

    tsv_path = _find_lirical_tsv(out_dir, output_prefix)
    if tsv_path is None:
        return {"status": "error", "error": "LIRICAL produced no TSV output",
                "cmd": " ".join(cmd), "candidates": []}
    try:
        candidates = parse_lirical_tsv(tsv_path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return {"status": "error", "error": f"cannot read LIRICAL TSV: {exc}", "candidates": []}

    # LIRICAL's genotype-aware TSV names the gene by Entrez id (NCBIGene:24), not a symbol, and the
    # two-track reconcile keys on the SYMBOL — fill it from LIRICAL's staged hgnc_complete_set.txt so the
    # differential carries gene symbols (for the report + the reconcile join). Best-effort: an
    # absent/unreadable map just leaves ``gene`` empty.
    apply_entrez_symbols(candidates, str(Path(data_dir) / "hgnc_complete_set.txt"))

    return {
        "status": "ok", "tool": "run_lirical",
        "mode": "genotype_aware" if genotype_aware else "phenotype_only",
        "assembly": asm, "n_hpo_terms": len(hpo_terms), "n_excluded_hpo": len(excluded_list),
        "hpo_terms": hpo_terms,             # the VALIDATED terms the posterior is actually conditioned on
        "phenotype_notes": phenotype_notes,  # IDs dropped/forwarded by the ontology gate (for Diagnostics)
        "chr_stripped": chr_stripped,   # was the VCF de-chr'd for Exomiser? (genotype-aware only)
        "n_candidates": len(candidates), "tsv_path": str(tsv_path),
        "candidates": [c.as_dict() for c in candidates],
        "note": ("" if genotype_aware else
                 "phenotype-only: no VCF+Exomiser DB, so the posterior weighs symptoms alone — stage an "
                 "Exomiser DB (deploy/lirical/build_and_stage.sh EXOMISER=1) for genotype-aware scoring."),
        "raw_data_to_llm": False,
    }


def _proc_tail(proc: Any, n: int = 1500) -> str:
    return ((getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "")[-n:]).strip()


def _find_lirical_tsv(out_dir: Path, prefix: str) -> "Path | None":
    """Locate LIRICAL's TSV output under ``out_dir``. Prefers the exact ``<prefix>.tsv``; falls back to
    the first ``<prefix>*.tsv`` (LIRICAL versions vary the exact suffix, e.g. ``.tsv`` vs a dated name)."""
    exact = out_dir / f"{prefix}.tsv"
    if exact.exists():
        return exact
    matches = sorted(out_dir.glob(f"{prefix}*.tsv"))
    return matches[0] if matches else None


def entrez_to_symbol_map(hgnc_path: str) -> dict[str, str]:
    """Parse LIRICAL's staged ``hgnc_complete_set.txt`` into ``{entrez_id: symbol}`` (both from the named
    header columns, so column-order changes are tolerated). Returns ``{}`` if the file is absent/malformed
    — the caller then leaves gene symbols empty rather than failing."""
    p = Path(hgnc_path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            try:
                i_sym, i_entrez = header.index("symbol"), header.index("entrez_id")
            except ValueError:
                return {}
            for line in fh:
                cells = line.rstrip("\n").split("\t")
                if len(cells) > max(i_sym, i_entrez):
                    entrez, sym = cells[i_entrez].strip(), cells[i_sym].strip()
                    if entrez and sym:
                        out[entrez] = sym
    except OSError:
        return {}
    return out


def apply_entrez_symbols(candidates: list[DiseaseCandidate], hgnc_path: str) -> int:
    """Fill each candidate's ``gene`` SYMBOL from its ``entrez_gene_id`` (``NCBIGene:24`` → ``ABCA4``)
    using :func:`entrez_to_symbol_map`. Only fills a blank ``gene``; returns how many were resolved. A
    no-op (returns 0) when no candidate carries an Entrez id or the HGNC map is unavailable."""
    if not any(c.entrez_gene_id for c in candidates if not c.gene):
        return 0
    mapping = entrez_to_symbol_map(hgnc_path)
    if not mapping:
        return 0
    n = 0
    for c in candidates:
        if c.gene or not c.entrez_gene_id:
            continue
        entrez = c.entrez_gene_id.split(":")[-1].strip()   # "NCBIGene:24" -> "24"
        sym = mapping.get(entrez)
        if sym:
            c.gene = sym
            n += 1
    return n


def make_phenotype_differential_tool() -> Any:
    """The ``run_lirical`` tool: phenotype-driven differential diagnosis. Defaults to the in-process path
    (which reports ``not_installed`` — LIRICAL runs only on HPC3); the gateway swaps in the HPC3
    ``lirical.sif`` line via the tool router when ``phenotype_on_hpc`` is set. Mirrors
    :func:`bioagent.tools.variant_annotation.make_variant_annotation_tool`."""
    from ..sdk import HarnessTool

    def _exec(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # In-process (no LIRICAL on the eyeserver): data_dir is empty → run_lirical returns not_installed.
        # The gateway's SlurmAnalysisExecutor injects data_dir/exomiser/assembly + the VCF and runs it in
        # lirical.sif on HPC3, where this SAME function executes with a real exec_fn (phenotype_cli).
        return run_lirical(
            hpo_terms=args.get("hpo_terms") or [],
            excluded_hpo=args.get("excluded_hpo") or (),
            vcf_path=str(args.get("vcf_path", "")),
            workspace=str(getattr(ctx, "workspace", "") or ""),
            data_dir=str(args.get("data_dir", "")),
            assembly=str(args.get("assembly", "hg38") or "hg38"),
            sample_id=str(args.get("sample_id", "sample-1") or "sample-1"),
            exomiser_hg19=str(args.get("exomiser_hg19", "")),
            exomiser_hg38=str(args.get("exomiser_hg38", "")),
        )

    return HarnessTool(
        "run_lirical",
        "Phenotype-driven DIFFERENTIAL DIAGNOSIS: rank candidate DISEASES by a calibrated post-test "
        "probability, given the patient's phenotype (as HPO terms) and — when a VCF is loaded — the "
        "variant findings. Runs LIRICAL (HPO/OMIM likelihood-ratio model) on HPC3, DOWNSTREAM of "
        "annotate_variants. Use it to answer 'how confident is each candidate disease' (e.g. 'RP 70% / "
        "LCA 20%') when overlapping symptoms can't pin the diagnosis alone. Get `hpo_terms` and "
        "`excluded_hpo` from map_phenotype_to_hpo — run it on the patient's clinical description FIRST "
        "and pass its output through. Do NOT write HPO IDs from memory: IDs one digit apart are "
        "different real phenotypes, and every ID here is checked against the HPO release (unknown ones "
        "are dropped). If a VCF + the Exomiser database are "
        "available it scores GENOTYPE-AWARE (variants sharpen the ranking); otherwise PHENOTYPE-ONLY (a "
        "valid posterior from symptoms alone). Returns a ranked list of diseases (name, OMIM/ORPHA id, "
        "gene, post-test probability, composite likelihood ratio). Reports ONLY LIRICAL's output; never "
        "invents a disease or probability. Returns status 'not_installed' if LIRICAL is not staged — then "
        "continue without the differential.",
        {"type": "object", "properties": {
            "hpo_terms": {"type": "array", "items": {"type": "string"},
                          "description": "observed HPO term IDs for the patient's phenotype (required), "
                                         "e.g. ['HP:0000510','HP:0000662']"},
            "excluded_hpo": {"type": "array", "items": {"type": "string"},
                             "description": "HPO term IDs the patient explicitly does NOT have (optional)"},
            "sample_id": {"type": "string",
                          "description": "the proband's sample id in the VCF (optional)"},
            "vcf_path": {"type": "string",
                         "description": "VCF for genotype-aware scoring (defaults to the run's dataset)"}}},
        _exec,
        reads_private_data=True, category="annotation",
    )
