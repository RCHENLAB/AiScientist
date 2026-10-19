---
name: extract_haploinsufficient_genes
summary: Scans the whole indexed corpus for genes a paper explicitly reports as haploinsufficient in retinal disease, and writes genes.csv + hits.csv with verbatim quotes.
category: literature
runs_on: hpc:literature
order: 415
research: false
served_model: true
line: literature
owner: literature
cpus: 4
mem_gb: 16
time_limit: "04:00:00"
---
# extract_haploinsufficient_genes

A corpus-wide extraction, not a question answered from the top-k passages. It reads every paper in
the PaperQA corpus (the RetiGene PDFs on dfs3b), keeps the sentences that could state a disease
mechanism, has the session's served model classify each one, verifies that every quote is a
verbatim sentence of the paper, and writes two tables for human review: one row per gene and one
row per supporting sentence. Nothing in the tables comes from the model's prior knowledge: a gene
is listed only because a corpus paper says, in its own words, that one working copy is not enough.

## How it is run

- From the console: pick **Corpus scan** in the composer and press Send. The gateway
  (`_run_corpus_scan`) submits one HPC3 job with a workspace of its own and puts the two CSVs in
  the run's downloads. One pick is one scan: the composer goes back to the engine the chat was
  using once the scan is sent.
- No model can call it (`research: false`, no `chat`): the PI cannot plan it and the Scientist
  cannot run it in a Research run. It takes about an hour of the session's served model (~15,000
  sentences to judge), and a Research run's literature job has no workspace of its own, so the
  tables would be written outside the run. To offer it to the agents, give the research line's
  literature executor the run's workspace first, then drop `research: false`.

## What it is not for

- A focused question with a prose answer ("is PRPF31 haploinsufficient?"): that is
  `deep_literature`, in Chat or Research.
- Anything outside the indexed corpus: it reads only the papers on dfs3b.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `max_quotes_per_gene` | integer | `3` | how many papers (PMID + verbatim quote) genes.csv lists per gene |
| `concurrency` | integer | `16` | parallel requests to the served model while judging sentences |
| `max_candidates` | integer | `0` | judge only the first N candidate sentences (0 = all); for a smoke test |
| `pmids` | array of string | — | restrict the scan to these PMIDs (empty = the whole corpus) |
<!-- /generated:parameters -->

## Outputs

- `artifacts/haploinsufficiency/genes.csv`: one row per gene — `gene` (HGNC approved symbol
  where the table resolves it), `phenotype` (as the paper writes it), `mechanism_statement`
  (the paper's own wording), `n_papers`, `n_hits`, `hedged_hits`, `conflicting_mechanism`
  (another corpus paper calls the same gene dominant-negative / gain-of-function),
  `pmid_1..3` and `quote_1..3` (verbatim sentences), and the review flags
  `flag_only_cited_titles`, `flag_no_dominance_marker`, `flag_gene_not_in_quote`,
  `flag_gene_alias_or_unknown`, `flag_garbled` (see Known limits).
- `artifacts/haploinsufficiency/hits.csv`: one row per judged sentence, every class included
  (`haploinsufficiency`, `dominant_negative`, `gain_of_function`, `recessive_lof`,
  `not_mechanism`, `other`), with `pmid`, `page`, `gene` (the HGNC symbol where the table resolves
  it), `gene_as_written`, `gene_status`, `gene_in_quote`, `gene_in_context`, `mechanism_class`,
  `explicit`, `negated`, `human_retinal`, `phenotype`, `mechanism_statement`, `quote`,
  `quote_verified`, `cite_like`, `dominance_marker`, `garbled`, `sentence_id`, `eligible`. The
  eligible rows are the ones genes.csv is built from.
- `work/haploinsufficiency/`: the candidate sentences and the model's raw judgments (resumable:
  a re-run after a timeout continues where it stopped).
- Result: counts at each stage (papers, sentences, candidates, judged, eligible hits, genes), the
  genes with their paper counts and flags, the two file paths, and `warnings`.

## Where it runs

<!-- generated:runs-on -->
As a Slurm job inside `paperqa.sif` on HPC3 (the PubMedBERT index lives on dfs3b), through `python -m aiscientist.tools.paperqa_cli`. Its job calls the session's served model, so it is offered only to a session with a GPU allocation.

Slurm resources for its job: 4 CPUs, 16 GB, up to 04:00:00.

Needs: `paper-qa`.

Reads private data: no.

In the research agents' catalog: no. No model can call it: it runs only from its own console line.
<!-- /generated:runs-on -->

## Known limits

- The corpus cache (`mechanism_cache/` next to the papers: re-extracted page text, sentences, and
  PubMedBERT chunk vectors) is built once by `scripts/build_mechanism_cache.py`; without it the
  tool rebuilds the text in the run's `work/` (slow: minutes for the PDFs, and no semantic
  candidates unless sentence-transformers and the embedding model are available).
- A quote is kept only if it is a verbatim sentence of the paper (whitespace and hyphenation
  ignored); the model cannot invent evidence, but it can still misread a sentence. The flags mark
  the cases a curator should read first: a gene whose only quotes look like reference-list
  entries (`flag_only_cited_titles`), a quote with no heterozygous/dominant wording
  (`flag_no_dominance_marker`), a quote that does not contain the gene symbol
  (`flag_gene_not_in_quote`), a symbol that is an alias or not in HGNC
  (`flag_gene_alias_or_unknown`), and text damaged by two-column extraction (`flag_garbled`).
- "Retinal disease" is read broadly (retinal dystrophies and the ocular syndromes the corpus
  covers); the `phenotype` column lets the curator narrow it.
- The judging pass needs the session's served model for about an hour. If that model goes away
  (its Slurm job preempted or expired) the tool stops after a short streak of refused connections
  and returns `model_unreachable` with what it judged so far; the console's corpus line brings a
  model back and resubmits, and the rerun resumes from the kept judgments.
- Recessive genes can still appear when a paper discusses NMD or dosage in a heterozygous
  carrier; the model is told to class those as `recessive_lof`, and such rows stay in hits.csv
  for review.

## Code and tests

- `tool.py`: text extraction (PyMuPDF blocks, column-aware), candidate selection (keyword +
  semantic), the per-sentence judgment, quote verification, HGNC normalisation, aggregation.
- `scripts/build_mechanism_cache.py`: the one-off cache build on HPC3.
- Tests: `tests/test_extract_haploinsufficient_genes.py` (a fake model and a three-paper corpus),
  `tests/test_corpus_route.py` (the console line).

## What the model is told

Nothing while `research: false`: this is the description a model would read once the tool is
offered to the agents.

<!-- generated:model-description -->
> Scan EVERY paper of the indexed literature corpus for genes the papers explicitly report as
> HAPLOINSUFFICIENT in retinal disease (a heterozygous loss-of-function allele causes disease
> because one copy is not enough; not dominant-negative, not gain-of-function, not recessive).
> Writes genes.csv (one row per gene: HGNC symbol, phenotype and mechanism in the paper's words,
> up to 3 PMIDs with verbatim quotes, review flags) and hits.csv (every judged sentence). Use it
> when the user wants the complete list with evidence as a table; for one focused question use
> deep_literature. Slow (about an hour): call it once per run.
<!-- /generated:model-description -->
