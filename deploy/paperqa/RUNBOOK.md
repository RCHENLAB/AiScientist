# RetiGene PaperQA — HPC3 run + Pull-Request runbook

Two parts: **A.** get the embedding job running on HPC3; **B.** open the PR so a
teammate can merge. Run the commands in order. `$USER` = your HPC3 username
(`<ucinetid>`). Connect the **UCI VPN (UCIFull) + Duo** before any HPC3 step.

---

## Part A — run the embedding on HPC3

### A0. Data is already on HPC3
The corpus was rsync'd to:
```
/dfs3b/ruic20_lab/<ucinetid>/retigene/papers/            # 1739 PDFs
/dfs3b/ruic20_lab/<ucinetid>/retigene/paperqa_manifest.csv
```
Confirm:
```bash
ssh <ucinetid>@hpc3.rcic.uci.edu \
  'ls /dfs3b/ruic20_lab/<ucinetid>/retigene/papers/*.pdf | wc -l'   # expect 1739
```

### A1. Put the code on HPC3 (once)
From your Mac (repo root):
```bash
rsync -av --exclude output --exclude .git --exclude .venv \
  "/Users/maziyao/Desktop/summer intern/code/BioAgentPrototype/" \
  <ucinetid>@access-hpc3.rcic.uci.edu:/dfs3b/ruic20_lab/<ucinetid>/BioAgentPrototype/
```
(Note `--exclude output` — never copy the 7 GB PDF folder into the repo path; the PDFs
already live under `retigene/papers/`.)

**Why `access-hpc3` and not `hpc3`:** RCIC's 2026-08-06 notice reserves the login nodes
(`login-i15/16/17`) for logging in and submitting Slurm jobs — no compute and **no data
transfer**; `rsync`/`SFTP`/`rclone`/`wget` must go through `access-hpc3.rcic.uci.edu`, and they
may kill login-node transfers. It mounts the same `/dfs3b` and `$HOME`, so only the hostname
changes. Its shell is restricted to transfer commands (`rsync`/`wget`/`curl`, **not** `bash`),
so keep running everything else on `hpc3.rcic.uci.edu`.

### A2. Build the Python env (once, in a compute allocation)
`conda create` + `pip install` is compute plus a multi-GB download (torch), so it does not
belong on a login node. Grab an interactive node first — compute nodes have outbound egress
(verified 2026-07-08):
```bash
ssh <ucinetid>@hpc3.rcic.uci.edu
srun -p standard -A ruic20_lab -c 4 --mem=16G -t 02:00:00 --pty /bin/bash -i
module load anaconda/2024.06
conda create -y -n paperqa python=3.12
source activate paperqa
# Pin to the version whose API the indexer was verified against (avoids a newer
# paper-qa changing the Settings/IndexSettings API out from under the script).
# Include the pymupdf extra: paper-qa prefers the pymupdf PDF reader if present and
# falls back to pypdf. pypdf (the default) CRASHES the whole build on some malformed
# PDFs (e.g. old Nature Genetics files: "More than one /FontFile found"), because
# paper-qa re-raises non-ValueError parse errors. pymupdf parses them without crashing.
pip install "paper-qa[local,pymupdf]==2026.3.18"  # sentence-transformers + torch (CPU) + pymupdf reader
# If that exact version is unavailable, use "paper-qa[local,pymupdf]>=5,<2027" and re-check
# scripts/build_paperqa_directory_index.py imports still resolve before sbatch.
# Confirm pymupdf is the active reader (must print paperqa_pymupdf.reader, not paperqa_pypdf):
python -c "from paperqa.settings import get_default_pdf_parser; print(get_default_pdf_parser().__module__)"
```

### A3. Pre-stage the embedding model (once)
Download the **PubMedBERT** model into the shared cache so the batch job never needs the network.
Stay in the A2 allocation — compute nodes on `standard` do have outbound egress (verified
2026-07-08), and a model download on a login node is exactly what RCIC asks us not to do:
```bash
export HF_HOME=/dfs3b/ruic20_lab/<ucinetid>/retigene/hf_cache
python - <<'PY'
from sentence_transformers import SentenceTransformer
SentenceTransformer("NeuML/pubmedbert-base-embeddings")   # biomedical, 768-dim
print("model cached")
PY
```

### A4. Submit the job
```bash
cd /dfs3b/ruic20_lab/<ucinetid>/BioAgentPrototype
sbatch deploy/paperqa/embed_corpus.slurm
squeue -u <ucinetid>                 # watch it queue/run
tail -f retigene_embed-*.log      # live log
```
Runtime: tens of minutes to a couple hours on 8 CPUs (PDF parsing dominates).

> First run only: if step A3 was skipped, the job fails offline. Either do A3, or submit
> once with `--export=ALL,HF_HUB_OFFLINE=0` so it can download mid-job.

### A5. Verify
The log ends with a JSON report — `indexed_file_count` should be ≈ **1739**. Then a
retrieval smoke test (no cloud LLM):
```bash
source activate paperqa
export HF_HOME=/dfs3b/ruic20_lab/<ucinetid>/retigene/hf_cache
python scripts/query_paperqa_index.py "CRB1 retinitis pigmentosa" \
  --papers    /dfs3b/ruic20_lab/<ucinetid>/retigene/papers \
  --manifest  /dfs3b/ruic20_lab/<ucinetid>/retigene/paperqa_manifest.csv \
  --index-dir /dfs3b/ruic20_lab/<ucinetid>/retigene/index_pubmedbert \
  --embedding st-NeuML/pubmedbert-base-embeddings \
  --name retigene_full_pubmedbert   # should return matching documents
```
If it returns relevant papers, the index works. **Tell the teammate: embedding runs on
HPC3, index built at `retigene/index_pubmedbert`.**

### A6. Wire the full QA (step 2) onto the same index
`src/bioagent/tools/paperqa_search.py` runs the actual question-answering with the local
Qwen LLM. For it to **reuse** the index you just built (not re-embed 1739 papers per
query), the gateway/serve environment must set these so both halves agree:
```bash
export BIOAGENT_PAPERQA_EMBEDDING=st-NeuML/pubmedbert-base-embeddings   # same model as the index
export BIOAGENT_PAPERQA_PAPERS=/dfs3b/ruic20_lab/<ucinetid>/retigene/papers
export BIOAGENT_PAPERQA_INDEX_DIR=/dfs3b/ruic20_lab/<ucinetid>/retigene/index_pubmedbert
export BIOAGENT_PAPERQA_INDEX_NAME=retigene_full_pubmedbert
export BIOAGENT_PAPERQA_MANIFEST=/dfs3b/ruic20_lab/<ucinetid>/retigene/paperqa_manifest.csv
```
These match the Slurm job's `EMBEDDING` / `PAPERS` / `INDEX_DIR` / `INDEX_NAME` /
`MANIFEST`. The answering LLM comes from the run's local Qwen (tunnel port + served
model name on the context), so no cloud LLM is used end-to-end.

---

## Part B — open the Pull Request (so the teammate can merge)

Only **code / config / docs** go through git. **Never commit the PDFs or the index**
(GBs; GitHub rejects >100 MB files). Do this on your **Mac**, in the repo.

### B1. Make sure the big data is ignored
```bash
cd "/Users/maziyao/Desktop/summer intern/code/BioAgentPrototype"
grep -qxF 'output/' .gitignore || echo 'output/' >> .gitignore
```
(`output/` holds the 7 GB corpus + index-type artefacts — keep it out of git. The small
manifest CSVs live there too; if you want them tracked, copy them somewhere outside
`output/` or force-add them individually — but do NOT `git add output/`.)

### B2. Stage only the code/config/docs
```bash
git add .gitignore \
        deploy/paperqa/ \
        scripts/build_paperqa_directory_index.py \
        scripts/build_retigene_paperqa_manifest.py \
        scripts/query_paperqa_index.py \
        skills/literature-corpus-recovery/ \
        handoff/
git status        # SANITY CHECK: confirm NO *.pdf and NO output/ are staged
```

### B3. Commit + push to your fork
```bash
git commit -m "PaperQA embedding: HPC3 Slurm job, indexer, runbook, corpus-recovery skill"
git push origin feat/paperqa-embedding
```

### B4. Open the PR
- GitHub prints a "Create a pull request" link after the push — open it, **or** go to
  your fork `<ucinetid>-stack/BioAgentPrototype` on github.com and click **Compare & pull
  request**.
- Base = the teammate's repo `KrimsonSun/BioAgentPrototype` (branch `main`); compare =
  your `feat/paperqa-embedding`.
- Title it e.g. *"PaperQA embedding pipeline + corpus recovery"*, describe what it does,
  create the PR.
- The teammate reviews and **merges**; then they pull/deploy as they described.

### PR checklist (paste into the PR description)
- [ ] Embedding job runs on HPC3, `indexed_file_count` ≈ 1739
- [ ] Retrieval smoke test returns relevant papers
- [ ] No PDFs / index files committed (only code, config, docs, small manifests)
