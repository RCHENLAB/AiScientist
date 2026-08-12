# How the RetiGene PaperQA index works (operations)

Plain-language walkthrough of what actually happens when we "embed the corpus" and
then "ask a question," and why each design choice was made. Written for the RetiGene
setup (local models, on HPC3, no cloud).

## The one-sentence version

We turn every paper's **text** into searchable number-vectors (embeddings) with a
**local** model, store them in an index, and at question time we pull back the most
relevant passages by vector similarity — a retrieval-augmented QA (RAG) pipeline that
never sends paper text to any cloud API.

## The pipeline, step by step

```
PDFs + manifest ──► parse text ──► chunk ──► embed each chunk ──► vector index
                                                                      │
question ──► embed question ──► similarity search (top-k chunks) ─────┘ ──► LLM writes answer
```

1. **Input.** A folder of PDFs (`papers/`) plus a `paperqa_manifest.csv` that gives each
   file its citation metadata (title, DOI, year, journal). The manifest means PaperQA
   does **not** need an LLM to guess citations — it reads them from the CSV.

2. **Parse.** For each PDF, PaperQA extracts the **text layer** (via pypdf/pymupdf). This
   is why file quality mattered so much upstream:
   - image-only scans yield no text ⇒ we OCR'd them first (added a text layer);
   - "figure-only" or "supplementary-only" files have no real body text ⇒ we replaced
     them with the full article.
   Figures/images themselves are **not** used — `multimodal = OFF` (see "Design choices").

3. **Chunk.** The text is split into overlapping passages ("chunks"), on the order of a
   few thousand characters each with a small overlap so a sentence isn't cut across a
   boundary. Chunk size and overlap are tunable in `ParsingSettings`. Figure captions and
   text tables ride along as text.

4. **Embed.** Each chunk is converted to a fixed-length vector by a **local
   sentence-transformers** model — default `st-NeuML/pubmedbert-base-embeddings`
   (biomedical PubMedBERT, 768 dimensions; slower than a generic MiniLM but tuned for
   medical text, and still CPU-feasible). "Local" is the whole point: the model runs on
   HPC3, so paper text never leaves UCI and no per-call cloud cost is incurred.

5. **Index.** All chunk vectors (plus the manifest citation data) are written to an index
   directory (`index_pubmedbert/`). Building this index over the whole corpus is the
   "embedding job" we run on HPC3.

6. **Ask.** At query time the question is embedded with the same model, and the index is
   searched by cosine similarity to retrieve the top-k most relevant chunks. Those chunks
   (with their citations) are handed to a language model that writes the answer with
   references. Retrieval is local; only this final answer step uses an LLM.

## Design choices (and why)

- **Local embedding model, no cloud.** Paper text is only ever seen by a local ST model
  on HPC3. Good for licensing/privacy and cost. Default is the biomedical
  `st-NeuML/pubmedbert-base-embeddings` (per advisor: go straight to the domain model).
  `st-multi-qa-MiniLM-L6-cos-v1` is a faster generic fallback — but **changing the model
  means the index must be rebuilt** (different vector space), and the query tool
  (`tools/paperqa_search.py`) must use the *same* model, or retrieval silently fails.
- **No cloud LLM during indexing.** The indexer points any accidental LLM call at a dead
  local endpoint (`http://127.0.0.1:9`), so a misconfigured run fails locally instead of
  silently calling a paid cloud model.
- **Multimodal OFF.** Text-only RAG. Images/figures are not embedded; figure *captions*
  (which are text) are. Turning multimodal on would require a vision model and would send
  image data out — not wanted here.
- **Manifest-driven citations.** Metadata comes from the CSV, not from an LLM guessing, so
  citations are exact and reproducible.
- **CPU job.** PubMedBERT embeds fine on CPU (slower than MiniLM, but the slow part is
  still PDF parsing, not the math). A GPU only helps for an even larger embedding model.

## What "one run" produces

- `index_pubmedbert/` — the built vector index (the deliverable).
- A JSON report printed at the end with `indexed_file_count` (should ≈ the number of
  selected papers in the manifest).

## Where each piece lives (RetiGene repo)

- `scripts/build_retigene_paperqa_manifest.py` — builds `paperqa_manifest.csv` from the
  master manifest (one row per selected paper: file, citation, title, doi, year, journal).
- `scripts/build_paperqa_directory_index.py` — the indexer (parse ► chunk ► embed ► index).
- `scripts/query_paperqa_index.py` — a retrieval smoke test (no cloud LLM needed).
- `deploy/paperqa/embed_corpus.slurm` — the HPC3 Slurm job that runs the indexer.
- `deploy/paperqa/README.md` — one-time setup + how to submit the job.

## The knobs you can tune later

| Knob | Where | Effect |
|---|---|---|
| `chunk_size`, `overlap` | `ParsingSettings` in the indexer | passage size / context bleed |
| `embedding` | `--embedding` flag / `EMBEDDING` env | MiniLM (fast) vs PubMedBERT (domain) |
| `concurrency` | `--concurrency` flag | how many PDFs parsed in parallel |
| top-k | query settings | how many chunks retrieved per question |
