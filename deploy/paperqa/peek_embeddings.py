#!/usr/bin/env python3
"""Peek at a finished PaperQA embedding index — what the vectors actually look like.

Run on HPC3 inside the paperqa env, after the embed job has written the index:

    conda activate ~/.conda/envs/paperqa   # or your PQA_ENV
    python deploy/paperqa/peek_embeddings.py \
        /dfs3b/ruic20_lab/$USER/retigene/index_pubmedbert/retigene_full_pubmedbert

If you pass no path it falls back to the local 50-paper MiniLM pilot.

Each doc file is a zlib-compressed pickle of a paperqa `Docs` object:
  Docs.docs   -> {doc_id: Doc}      (paper-level metadata: citation, docname, dockey)
  Docs.texts  -> [Text, ...]        (the chunks; Text.text is the snippet,
                                     Text.embedding is the vector)
"""
import sys, glob, zlib, pickle, os
import numpy as np

# paperqa's Docs.__setstate__ assumes a live pydantic private state; when we load a
# pickle purely to inspect it (no LLM/client), that assumption trips. Neutralize it.
try:
    import paperqa.docs as _pd
    def _safe_setstate(self, state):
        self.__dict__.update(state.get("__dict__", {}))
        for k, v in state.items():
            if k != "__dict__":
                try:
                    object.__setattr__(self, k, v)
                except Exception:
                    pass
    _pd.Docs.__setstate__ = _safe_setstate
except Exception as e:  # pragma: no cover
    print("warn: could not patch paperqa Docs.__setstate__:", e)

DEFAULT = ("output/retigene_papers/paperqa_pilot_50/index_minilm/"
           "retigene_pilot_50_minilm")

def load(path):
    return pickle.loads(zlib.decompress(open(path, "rb").read()))

def main():
    index_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    docs_dir = os.path.join(index_dir, "docs")
    files = sorted(glob.glob(os.path.join(docs_dir, "*.zip")))
    if not files:
        sys.exit(f"no doc files under {docs_dir}")

    print(f"index dir : {index_dir}")
    print(f"papers    : {len(files)} doc files\n")

    # --- one chunk, in full detail ---
    d = load(files[0])
    texts = d.texts
    t = texts[0]
    emb = np.asarray(t.embedding, dtype=float)
    doc = list(d.docs.values())[0]
    print("=== one paper ===")
    print("citation :", str(getattr(doc, "citation", ""))[:100])
    print("chunks   :", len(texts))
    print("\n=== one chunk ===")
    print("name     :", t.name)
    print("text     :", t.text[:200].replace("\n", " "), "...")
    print("embedding: dim=%d dtype=%s" % (emb.shape[0], emb.dtype))
    print("first 10 :", np.round(emb[:10], 4))
    print("L2 norm  :", round(float(np.linalg.norm(emb)), 4))

    # --- aggregate over a sample of papers ---
    dims, norms, nchunks = set(), [], []
    for f in files[:25]:
        dd = load(f)
        nchunks.append(len(dd.texts))
        for x in dd.texts:
            v = np.asarray(x.embedding, dtype=float)
            dims.add(v.shape[0])
            norms.append(float(np.linalg.norm(v)))
    print("\n=== sampled %d papers ===" % min(25, len(files)))
    print("embedding dim(s) :", sorted(dims), "  (PubMedBERT=768, MiniLM=384)")
    print("chunks/paper     : min=%d mean=%.1f max=%d" %
          (min(nchunks), sum(nchunks) / len(nchunks), max(nchunks)))
    print("vector L2 norm   : mean=%.3f (≈1.0 => normalized, cosine = dot product)"
          % (sum(norms) / len(norms)))

if __name__ == "__main__":
    main()
