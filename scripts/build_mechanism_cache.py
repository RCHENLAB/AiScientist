"""Build the corpus cache ``extract_haploinsufficient_genes`` reads: re-extracted page text and
sentences for every PDF, and the PubMedBERT vector of every chunk. One-off maintenance on HPC3,
inside ``paperqa.sif`` (PyMuPDF and sentence-transformers live there), by whoever owns the corpus:

    apptainer exec --bind /dfs3b /dfs3b/ruic20_lab/software/AiScientist/retigene/paperqa.sif \\
        python3 scripts/build_mechanism_cache.py \\
            --papers   /dfs3b/ruic20_lab/software/AiScientist/retigene/papers \\
            --manifest /dfs3b/ruic20_lab/software/AiScientist/retigene/paperqa_manifest.csv

The cache lands in ``<papers>/../mechanism_cache/`` (``--out`` to change it) and is read-only at
query time; a run without it extracts the text into its own ``work/`` instead (slow). Re-run after
papers are added: extraction is per paper and skips the ones already cached, the vectors are
rebuilt when the chunk count changed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.tools.extract_haploinsufficient_genes.tool import (  # noqa: E402
    build_text_cache, chunk_vectors, load_chunks, sentence_transformer_embed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--papers", required=True, help="the PDF corpus directory")
    ap.add_argument("--manifest", default="", help="paperqa_manifest.csv (file_location, title, doc_id)")
    ap.add_argument("--out", default="", help="cache directory (default: <papers>/../mechanism_cache)")
    ap.add_argument("--no-vectors", action="store_true", help="text only; skip the embedding pass")
    ns = ap.parse_args(argv)
    papers = Path(ns.papers)
    out = Path(ns.out) if ns.out else papers.parent / "mechanism_cache"
    log = lambda s: print(s, flush=True)  # noqa: E731
    _, chunk_path = build_text_cache(papers, Path(ns.manifest) if ns.manifest else None, out, log=log)
    if ns.no_vectors:
        return 0
    hf = papers.parent / "hf_cache"
    embed = sentence_transformer_embed(hf if hf.is_dir() else None)
    if embed is None:
        print("sentence-transformers or the PubMedBERT model is not available; vectors not built", file=sys.stderr)
        return 1
    chunk_vectors(load_chunks(chunk_path), out, embed, writable=True, log=log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
