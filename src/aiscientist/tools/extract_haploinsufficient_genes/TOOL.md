---
name: extract_haploinsufficient_genes
summary: Finds, by model-written search requests, the genes the corpus papers explicitly report as haploinsufficient in retinal disease, and writes genes.csv + hits.csv with verbatim quotes.
category: literature
runs_on: hpc:literature
order: 415
research: false
own_gpu: true
line: literature
owner: literature
cpus: 8
mem_gb: 48
time_limit: "06:00:00"
---
# extract_haploinsufficient_genes

A list extraction over the PaperQA corpus (the RetiGene PDFs on dfs3b), done the way
`search_corpus` answers a question, in two rounds:

1. **Requests.** The model writes 16 search requests from the definition: full sentences, each from
   another angle (the mechanism's wording, the allele types, the evidence, the phenotypes). It is
   told to name no gene, so the genes come from the papers and not from the model.
2. **Retrieve.** PubMedBERT embeds each request and takes its 50 nearest chunks of the corpus text
   cache (at most 3 per paper). Every chunk whose text states a dosage mechanism in words
   (haploinsufficiency, gene dosage, dosage sensitivity, one functional copy or allele, loss of one
   copy, half the normal amount; PDF ligatures such as "ﬁ" read as their letters) is read too: a
   chunk's vector is mostly its topic, so the requests alone reached 50 of the corpus' 337 chunks
   that say "haploinsufficiency". The model also writes the exact words such a passage would use;
   each is counted in the corpus, and the chunks holding one that is in at most 1% of the chunks
   are read as well (at most `max_term_passages` more): a safety net for wordings the built-in list
   lacks. The word matching, the counting and the nearest-chunk search are the shared corpus layer
   `search_corpus` uses (`../_lib/corpus.py`).
3. **Locate.** Each chunk is read back as its paper, page and numbered sentences.
4. **Read.** The model reads each chunk once and returns, per gene whose disease mechanism a
   sentence states, the mechanism class, whether it is explicit, negated and about a human retinal
   disease, the phenotype as written, and that sentence verbatim with its number.
5. **Verify.** Every quote must be a sentence of the paper; gene symbols are normalised through
   HGNC; the results become two tables for human review, one row per gene and one row per
   supporting sentence.

Round two repeats steps 1-4 for each gene round one read as haploinsufficient whose symbol HGNC
knows: the model writes one request about that gene, and only chunks that name the gene are searched (15 per gene), so the other
papers that state the same gene's mechanism are read too. Nothing in the tables comes from the
model's prior knowledge: a gene is listed only because a corpus paper says, in its own words, that
one working copy is not enough.

## How it is run

- From the console: a list question about the RetiGene papers ("What are haploinsufficient genes
  in RetiGene papers?": it names the corpus, asks for the genes and not about the user's data) is
  routed to the corpus curation (`gateway/app._corpus_curation`). Its plan card says what is read
  and how long it takes on the session; once the user runs it, the gateway (`_run_corpus_scan`)
  submits one HPC3 job with a workspace of its own and puts the CSVs in the run's results. A
  client can also send `route: "corpus"` to `/api/lab`, which runs it without a plan.
- The job brings its own model (`gateway/corpus_scan.py`): it gets one GPU on the lab's paid
  `gpu` partition (the first free A30 or A100), starts vLLM there on the node's loopback, runs the scan
  against it and exits, which frees the card. Any session can run it, whatever model it uses
  itself, and the session's own model is never called. The scan's model is fixed
  (`AISCIENTIST_CORPUS_SCAN_MODEL`, else the cluster sessions' model), so the tables do not
  depend on who asked.
- A session on the user's own API key (for example Claude Haiku) scans with that model instead,
  on the user's bill: the job runs on a CPU node with no model of its own, and every prompt it
  needs comes back to the Eye Server through files in `work/relay/` (`gateway/llm_relay.py`),
  where the gateway calls the model with the key, records each call in the user's API usage and
  stops at the scan's cap (`AISCIENTIST_CORPUS_SCAN_MAX_COST_USD`, default $20, lowered to what
  is left of the user's budget). The key never leaves the Eye Server.
- Before reading all of round one, the scan reads a pilot of about 1% of its passages (at least
  5, at most 25, spread over the list). If half of them cannot be read, it stops there and says
  why; on a priced model, if their cost projects past `max_cost_usd`, it stops as well. The pilot's
  passages are kept, so the full pass does not read them again.
- A finished scan is kept on the Eye Server (`<data root>/corpus_scans/<key>/`), keyed by the
  corpus files, the scan's rules (`rules_digest()`) and the model. The next request for the same
  corpus, from a scan request or from a `route: "chat"` message asking for the haploinsufficient
  genes, is answered from it at once; adding papers changes the key, and the next request scans again.
- No model can call it (`research: false`, no `chat`): the PI cannot plan it and the Scientist
  cannot run it in a Research run. It holds a GPU while it reads every retrieved passage, and a
  Research run's literature job has no workspace of its own, so the tables would be written outside
  the run. To offer it to the agents, give the research line's literature executor the run's
  workspace first, then drop `research: false`.

## What it is not for

- A focused question with a prose answer ("is PRPF31 haploinsufficient?"): that is
  `search_corpus` or `deep_literature`, in Chat or Research.
- Anything outside the indexed corpus: it reads only the papers on dfs3b.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `n_requests` | integer | `16` | how many search requests the model writes for the first round |
| `k_per_request` | integer | `50` | passages (chunks) each first-round request retrieves |
| `keyword_round` | integer | `1` | 1 = round one also reads every passage that states a dosage mechanism in words (haploinsufficiency, gene dosage, one functional copy, ...); 0 = the requests alone |
| `term_round` | integer | `1` | 1 = the model also writes the exact words a qualifying passage would use; each is counted in the corpus, and every passage holding one that is in at most 1% of the passages is read too; 0 = the built-in dosage wording only |
| `max_term_passages` | integer | `300` | at most this many passages the model's words add (rarest words first) |
| `k_per_gene` | integer | `15` | passages each second-round gene request retrieves, among the chunks that name the gene |
| `max_quotes_per_gene` | integer | `3` | how many papers (PMID + verbatim quote) genes.csv lists per gene |
| `concurrency` | integer | `16` | parallel requests to the model while it reads the passages |
| `max_passages` | integer | `0` | read only the first N retrieved passages (0 = all); for a smoke test |
| `pilot_fraction` | number | `0.01` | share of round one's passages read first as a pilot (at least 5, at most 25): the scan stops before the rest when half of them fail, or when the cost they project is above max_cost_usd (0 = no pilot) |
| `max_cost_usd` | number | `0.0` | stop before the full pass when the pilot projects a model cost above this, in USD (0 = no cap; only a model the gateway calls for the scan has a price) |
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
  `quote_verified`, `cite_like`, `dominance_marker`, `garbled`, `sentence_id`, `found_by` (the
  requests that retrieved the passage: `R3` is round-one request 3, `K` the keyword round, `T` the
  model's words, `G:PRPF31` the gene's own request), `eligible`. The eligible rows are the ones genes.csv is built from.
- `artifacts/haploinsufficiency/requests.csv`: every search request (`id`, `round`, `gene`,
  `request`, `n_passages` it retrieved), so a curator can see what was searched for; row `T` lists
  the model's words, each with its passage count and use (`filter` = read, `rank`/`absent`/`over_cap`
  = not used).
- `artifacts/curation/`, written by the gateway from `hits.csv` after the job (or from a kept
  scan), so it needs no new scan (`gateway/corpus_scan.write_curation`): `haploinsufficient_genes.csv`,
  one row per gene with its `claim` (`haploinsufficient`: a paper states it in so many words and
  none argues against it; `contested`: one states it and another argues against it; `suggested`:
  only hedged statements; `argued against`: papers on a human retinal disease say it is not, or
  unlikely to be, the mechanism), `explicit`, `negated`, `phenotype`, the strongest sentence
  verbatim with its `pmid`, `doi`, `title` (from the corpus manifest) and `page`, the strongest
  sentence against it (`quote_against`, `pmid_against`, `page_against`, `against_context`), the
  papers on each side and the review flags; `haploinsufficient_evidence.csv`, one row per sentence
  that bears on a claim. A gene is curated from sentences about a human retinal or ocular disease
  whose quote was found in the paper and whose gene is known. For such a gene, a sentence against
  its haploinsufficiency from an animal model or another disease ("heterozygous Tspan12 mice
  vascularize normally") is kept as `argues against elsewhere`: it makes a stated gene `contested`
  and sets the flag `argued_against_in_animal_model_or_other_disease`, but never makes a gene
  `argued against` on its own. "Save tables to my data" copies the folder into the user's Datasets.
- `work/haploinsufficiency/`: `requests.json` (the requests, reused by a rerun) and
  `judgments.jsonl` (the model's whole reply for every passage). A rerun after a timeout reads only
  the passages without a usable reply; a reply the parser rejected is parsed again from disk first,
  so fixing the parser costs no model call.
- Result: counts (papers, sentences, requests, gene requests, passages read, eligible hits, genes,
  keyword and model-word passages), the round-one requests, the model's words (`terms`), the genes
  with their paper counts and flags, the file paths, and `warnings`.

## Where it runs

<!-- generated:runs-on -->
As a Slurm job inside `paperqa.sif` on HPC3 (the PubMedBERT index lives on dfs3b), through `python -m aiscientist.tools.paperqa_cli`. Its job gets a GPU of its own on the lab's paid partition and starts its own model there, so the session needs no GPU allocation.

Slurm resources for its job: 8 CPUs, 48 GB, up to 06:00:00.

Needs: `paper-qa`.

Reads private data: no.

In the research agents' catalog: no. No model can call it: it runs only from its own console line.
<!-- /generated:runs-on -->

## Known limits

- Recall is bounded by retrieval: a paper that states the mechanism without any of the dosage
  words, is in no request's nearest chunks, and names no gene round one found, is never read.
  `k_per_request` and `n_requests` widen round one; `requests.csv` and `found_by` (`K` = the
  keyword round) show which request found what.
- The corpus cache (`mechanism_cache/` next to the papers: re-extracted page text, sentences, and
  PubMedBERT chunk vectors) is built once by `scripts/build_mechanism_cache.py`; without it the
  tool rebuilds the text in the run's `work/` (minutes for the PDFs). Without sentence-transformers
  and the embedding model the scan cannot retrieve and fails with a message saying so.
- A quote is kept only if it is a verbatim sentence of the paper (whitespace and hyphenation
  ignored); the model cannot invent evidence, but it can still misread a sentence. The flags mark
  the cases a curator should read first: a gene whose only quotes look like reference-list
  entries (`flag_only_cited_titles`), a quote with no heterozygous/dominant wording
  (`flag_no_dominance_marker`), a quote that does not contain the gene symbol
  (`flag_gene_not_in_quote`), a symbol that is an alias or not in HGNC
  (`flag_gene_alias_or_unknown`), and text damaged by two-column extraction (`flag_garbled`).
- "Retinal disease" is read broadly (retinal dystrophies and the ocular syndromes the corpus
  covers); the `phenotype` column lets the curator narrow it.
- If the model goes away mid-scan (out of memory, a lost node) the tool stops after a short streak
  of refused connections and returns `model_unreachable` with what it read so far; the console's
  corpus line submits the job again, and the rerun resumes from the kept requests and replies. A
  model that never starts ends the job with a `failed` result naming its log.
- A change to the code that decides which passages are retrieved, how replies are read or how rows
  are built needs `RULES_REVISION` bumped, or a kept scan made under the old code is reused.
- The requests are the model's, and temperature 0 is not bit-exact across cards and batch sizes, so
  two scans of the same corpus can differ by a few genes. A kept scan is one such run; read the
  flagged and the unexpected genes first.
- Recessive genes can still appear when a paper discusses NMD or dosage in a heterozygous
  carrier; the model is told to class those as `recessive_lof`, and such rows stay in hits.csv
  for review.

## Code and tests

- `tool.py`: text extraction (PyMuPDF blocks, column-aware), the request and word writing,
  retrieval (`nearest_chunks`, `keyword_chunks`, `model_term_chunks`, `chunks_naming`, on
  `../_lib/corpus.py`'s `TextIndex` and `nearest`), the per-passage reading, quote verification,
  HGNC normalisation, aggregation.
- `scripts/build_mechanism_cache.py`: the one-off cache build on HPC3.
- Tests: `tests/test_extract_haploinsufficient_genes.py` (a fake model, a toy embedder and a
  four-paper corpus), `tests/test_corpus_route.py` (the console line),
  `tests/test_corpus_curation.py` (the route from a RetiGene list question, the curation tables, saving them).

## What the model is told

Nothing while `research: false`: this is the description a model would read once the tool is
offered to the agents.

<!-- generated:model-description -->
> Search the indexed literature corpus for genes the papers explicitly report as HAPLOINSUFFICIENT
> in retinal disease (a heterozygous loss-of-function allele causes disease because one copy is
> not enough; not dominant-negative, not gain-of-function, not recessive). The model writes search
> requests, PubMedBERT retrieves the passages, the model reads each passage, and every quote is
> checked against the paper. Writes genes.csv (one row per gene: HGNC symbol, phenotype and
> mechanism in the paper's words, up to 3 PMIDs with verbatim quotes, review flags), hits.csv
> (every judged sentence) and requests.csv (the search requests). Use it when the user wants the
> list with evidence as a table; for one focused question use search_corpus or deep_literature.
> Slow: call it once per run.
<!-- /generated:model-description -->
