#!/usr/bin/env python3
"""Generate the bundled HPO lexicon (``hpo_lexicon.tsv.gz``) from the official ``hp.json``.

WHY a bundled lexicon: the free-text -> HPO mapper must pick IDs from the REAL ontology (a closed set
it can validate against), and it runs on the eye server / in tests, where the LIRICAL data directory
(which carries hp.json on HPC3) is not mounted. hp.json itself is ~23 MB of obographs JSON; the slice
we need — id, label, synonyms, obsoletion — is ~1.5 MB raw, ~400 KB gzipped, so it ships in the repo
and the mapper works offline with no HPC3 and no network.

Scope: the ``HP:0000118`` (Phenotypic abnormality) subtree — the terms LIRICAL/Exomiser score on —
plus every obsolete HP term, so a stale ID (from an old sheet, a paper, or an LLM) is reported as
obsolete WITH its replacement instead of silently missing. Inheritance / clinical-modifier / frequency
subtrees are deliberately excluded: they are not phenotypic features.

Usage:
    python scripts/build_hpo_lexicon.py                     # download the current hp.json, regenerate
    python scripts/build_hpo_lexicon.py --hp-json hp.json   # use a local copy (e.g. LIRICAL's staged one)

Re-run when pinning a new HPO release; commit the regenerated .tsv.gz. HPO is CC BY 4.0 — the release
version is recorded in the file header so any mapping can be traced to an exact ontology version.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

HP_JSON_URL = "https://purl.obolibrary.org/obo/hp.json"
PHENOTYPIC_ABNORMALITY = "HP:0000118"
REPLACED_BY_PRED = "http://purl.obolibrary.org/obo/IAO_0100001"   # obo "term replaced by"
OUT_DEFAULT = Path(__file__).resolve().parents[1] / "src/bioagent/tools/hpo_terms/hpo_lexicon.tsv.gz"

# Synonym classes. EXACT synonyms are alternative names for the SAME concept ("Retinitis pigmentosa"
# for HP:0000510) and are safe to match at full weight. NARROW/BROAD/RELATED are looser (a narrow
# synonym names a sub-concept), so they are kept in a separate column the index scores lower — useful
# recall, but they must never outrank an exact hit.
EXACT = "hasExactSynonym"
OTHER = ("hasNarrowSynonym", "hasBroadSynonym", "hasRelatedSynonym")


def _curie(iri: str) -> str:
    """``http://purl.obolibrary.org/obo/HP_0000510`` -> ``HP:0000510`` (non-HP IRIs pass through)."""
    return iri.rsplit("/", 1)[-1].replace("_", ":")


def _clean(s: str) -> str:
    """Collapse whitespace and drop tab/pipe, the field/list separators of the output TSV."""
    return re.sub(r"\s+", " ", (s or "").replace("\t", " ").replace("|", " ")).strip()


def load_graph(hp_json: "Path | None") -> dict:
    if hp_json:
        return json.loads(hp_json.read_text(encoding="utf-8"))["graphs"][0]
    print(f"downloading {HP_JSON_URL} ...", file=sys.stderr)
    with urllib.request.urlopen(HP_JSON_URL, timeout=300) as resp:   # noqa: S310 - pinned obo purl
        return json.loads(resp.read().decode("utf-8"))["graphs"][0]


def phenotypic_abnormality_ids(graph: dict) -> set[str]:
    """Every ``is_a`` descendant of HP:0000118 (the root itself included)."""
    children: dict[str, list[str]] = defaultdict(list)
    for edge in graph.get("edges", []):
        if edge.get("pred") == "is_a":
            children[_curie(edge["obj"])].append(_curie(edge["sub"]))
    seen: set[str] = set()
    stack = [PHENOTYPIC_ABNORMALITY]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(children[node])
    return seen


def build_rows(graph: dict) -> tuple[list[tuple[str, ...]], str]:
    """-> ([(id, label, exact_synonyms, other_synonyms, status, replaced_by)], hpo_version)."""
    keep = phenotypic_abnormality_ids(graph)
    version = graph.get("meta", {}).get("version", "")
    rows: list[tuple[str, ...]] = []
    for node in graph.get("nodes", []):
        hp_id = _curie(node.get("id", ""))
        if not hp_id.startswith("HP:"):
            continue
        meta = node.get("meta", {}) or {}
        obsolete = bool(meta.get("deprecated"))
        if not obsolete and hp_id not in keep:
            continue                                    # inheritance / modifier / frequency subtrees
        label = _clean(node.get("lbl", ""))
        if obsolete:
            # Obsolete rows exist only to explain a stale ID, so we keep just the pointer to the
            # replacement; their names are not offered as mapping candidates (label is left in for
            # the message, prefixed "obsolete ..." by HPO itself).
            replaced = next((_clean(p.get("val", "")) for p in meta.get("basicPropertyValues", [])
                             if p.get("pred") == REPLACED_BY_PRED), "")
            rows.append((hp_id, label, "", "", "obsolete", replaced))
            continue
        syns = meta.get("synonyms", []) or []
        exact = [_clean(s["val"]) for s in syns if s.get("pred") == EXACT and s.get("val")]
        other = [_clean(s["val"]) for s in syns if s.get("pred") in OTHER and s.get("val")]
        rows.append((hp_id, label, "|".join(exact), "|".join(other), "current", ""))
    rows.sort(key=lambda r: r[0])
    return rows, version


def write_lexicon(rows: list[tuple[str, ...]], version: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8", newline="\n") as fh:
        fh.write("# HPO lexicon for free-text -> HPO mapping. GENERATED by scripts/build_hpo_lexicon.py\n")
        fh.write("# — do not hand-edit; re-run the script to pin a new HPO release.\n")
        fh.write(f"# hpo_version\t{version}\n")
        fh.write("# source\thttps://purl.obolibrary.org/obo/hp.json (HPO, CC BY 4.0)\n")
        fh.write("# scope\tHP:0000118 (Phenotypic abnormality) subtree + obsolete terms\n")
        fh.write("#id\tlabel\texact_synonyms\tother_synonyms\tstatus\treplaced_by\n")
        for row in rows:
            fh.write("\t".join(row) + "\n")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hp-json", type=Path, default=None,
                    help="local hp.json (default: download the current release from the OBO purl)")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT, help=f"output lexicon (default: {OUT_DEFAULT})")
    args = ap.parse_args(argv)

    rows, version = build_rows(load_graph(args.hp_json))
    current = sum(1 for r in rows if r[4] == "current")
    write_lexicon(rows, version, args.out)
    print(f"wrote {args.out} — {current} current + {len(rows) - current} obsolete terms; "
          f"hpo_version={version or 'unknown'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
