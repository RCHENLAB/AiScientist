# PaperQA embedding on HPC3

Build the PaperQA retrieval index for the full RetiGene corpus (~1741 PDFs) on
RCIC **HPC3**, as a CPU Slurm job. The embedding model is a **local
sentence-transformers** model, so paper text never leaves UCI and no cloud API
is called.

- Indexer: `scripts/build_paperqa_directory_index.py`
- Manifest builder: `scripts/build_retigene_paperqa_manifest.py`
- Slurm job: `deploy/paperqa/embed_corpus.slurm`
- Default model: `st-NeuML/pubmedbert-base-embeddings` (biomedical PubMedBERT, 768-dim).
  Per advisor, go straight to this domain model — not the small MiniLM pilot. The query
  tool (`tools/paperqa_search.py`) must use the same model or retrieval breaks.

## Decision: embed *on* HPC3 (not locally, then upload)

The embedding is a local ST model = pure compute, and PaperQA runs its QA on
HPC3, so the index is built where it will be used. Data (PDFs + index) moves by
`rsync`; only code/config moves through git.

## Layout on HPC3

Everything lives under the lab DFS. Replace `$USER` with your HPC3 username.

```
/dfs3b/ruic20_lab/$USER/
  BioAgentPrototype/                 # this repo (git clone / rsync of the code)
  retigene/
    papers/                          # the 1741 PDFs
    paperqa_manifest.csv             # 9-col PaperQA manifest
    hf_cache/                        # Hugging Face model cache (persists)
    index_pubmedbert/                    # <- the built index (output)
```

## One-time setup

### 1. Python environment (login node)

```bash
module load anaconda/2024.06
conda create -y -n paperqa python=3.12
source activate paperqa
pip install "paper-qa[local,pymupdf]>=5"   # sentence-transformers + the pymupdf PDF reader
# pymupdf matters: paper-qa's default pypdf reader crashes the whole index build on some
# malformed PDFs (old Nature Genetics files: "More than one /FontFile found"). paper-qa
# auto-prefers pymupdf when installed. Verify with:
#   python -c "from paperqa.settings import get_default_pdf_parser; print(get_default_pdf_parser().__module__)"
```

### 2. Get the code + corpus onto HPC3

From your Mac (repo root). The manifest is generated locally first:

```bash
# a) build the full-corpus PaperQA manifest (writes paperqa_manifest.csv)
python scripts/build_retigene_paperqa_manifest.py \
  --priority-manifest output/retigene_papers/journal_priority/retigene_priority_manifest.csv \
  --papers            output/retigene_papers/journal_priority/papers_priority \
  --out               output/retigene_papers/journal_priority/paperqa_manifest.csv

# NOTE the host: RCIC (2026-08-06) forbids rsync/SFTP/rclone/wget on the login nodes and asks
# that ALL transfers go through access-hpc3.rcic.uci.edu. It mounts the same /dfs3b and $HOME,
# so only the hostname changes. (Its restricted shell allows rsync/wget/curl but not bash — run
# commands on hpc3.rcic.uci.edu as usual, just not the transfers.)

# b) code (small)
rsync -av --exclude output --exclude .venv --exclude .git \
  ./ $USER@access-hpc3.rcic.uci.edu:/dfs3b/ruic20_lab/$USER/BioAgentPrototype/

# c) corpus (~3.6 GB of PDFs + the manifest)
rsync -av \
  output/retigene_papers/journal_priority/papers_priority/ \
  $USER@access-hpc3.rcic.uci.edu:/dfs3b/ruic20_lab/$USER/retigene/papers/
rsync -av \
  output/retigene_papers/journal_priority/paperqa_manifest.csv \
  $USER@access-hpc3.rcic.uci.edu:/dfs3b/ruic20_lab/$USER/retigene/paperqa_manifest.csv
```

### 3. Pre-stage the embedding weights

Compute nodes have outbound egress (verified 2026-07-08), so fetch the weights inside a job
rather than on a login node — RCIC counts this as both compute and a download.

```bash
srun -p standard -A ruic20_lab -c 2 --mem=8G -t 01:00:00 --pty /bin/bash -i
source activate paperqa
export HF_HOME=/dfs3b/ruic20_lab/$USER/retigene/hf_cache
python - <<'PY'
from sentence_transformers import SentenceTransformer
SentenceTransformer("NeuML/pubmedbert-base-embeddings")
print("model cached")
PY
```

## Run the embedding job

```bash
cd /dfs3b/ruic20_lab/$USER/BioAgentPrototype
sbatch deploy/paperqa/embed_corpus.slurm
squeue -u $USER            # watch it
tail -f retigene_embed-*.log
```

Runtime is roughly tens of minutes to a couple of hours on 8 CPUs for ~1741
PDFs (PDF parsing dominates, not the embedding). Paths/model can be overridden
without editing the file:

```bash
sbatch --export=ALL,BASE=/dfs3b/ruic20_lab/$USER/retigene,\
EMBEDDING=st-NeuML/pubmedbert-base-embeddings,INDEX_DIR=/dfs3b/ruic20_lab/$USER/retigene/index_pubmedbert \
  deploy/paperqa/embed_corpus.slurm
```

## Verify

The job prints a JSON report with `indexed_file_count` (expect ~1741). Then a
quick retrieval smoke test (no cloud LLM):

```bash
source activate paperqa
python scripts/query_paperqa_index.py "CRB1 retinitis pigmentosa" \
  --papers    /dfs3b/ruic20_lab/$USER/retigene/papers \
  --manifest  /dfs3b/ruic20_lab/$USER/retigene/paperqa_manifest.csv \
  --index-dir /dfs3b/ruic20_lab/$USER/retigene/index_pubmedbert \
  --embedding st-NeuML/pubmedbert-base-embeddings \
  --name retigene_full_pubmedbert            # returns matching documents
```

Record the embedding model name alongside the index — a different model means a
different vector space and the index must be rebuilt.

## Notes

- **Offline:** `HF_HUB_OFFLINE=1` is set in the job so a misconfigured run fails
  fast instead of silently hitting the network. Unset it for the very first run
  if you skipped step 3.
- **No cloud LLM:** the indexer routes any accidental LLM call to a dead local
  endpoint; a misconfigured run fails locally rather than calling a cloud model.
- **GPU:** PubMedBERT runs on CPU (slower than MiniLM but fine for this corpus). If you
  later want a GPU to speed it up, switch to a `gpu` partition with `--gres=gpu:1` and add
  `--account`/`--partition` accordingly; the model auto-uses CUDA if present.
