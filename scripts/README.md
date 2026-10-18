# scripts/

Command-line tools that sit beside the product: deploying it, watching what it does, setting up
HPC3, keeping the repository consistent, building reference data, measuring model behaviour, and the
RetiGene literature corpus. None of them is imported by the gateway at run time.

Every script documents itself in its first lines (the module docstring, or the comment block of a
shell script); `python scripts/<name>.py --help` prints the options. This page says what each one is
for, where it runs and whether it changes anything.

**Where it runs:** *laptop* = a developer machine with the repository and SSH access;
*gateway* = the eyeserver host that runs `aiscientist.service`; *HPC3* = a compute node, never a
login node. **Changes:** *read-only*, or what it writes.

Settings shared by the deploy and monitoring scripts (`DEPLOY_SSH`, `ADMIN_SSH`, `BIND_HOST`, …) can be
kept in a `.deploy.env` at the repository root. It is git-ignored and must never be committed.

## Deploy and operate

| Script | What it is for | Runs on | Changes |
|---|---|---|---|
| `sync_deploy.sh` | Deploy: rsync the local working tree to the gateway host, reinstall the package into the server venv, restart `aiscientist.service`, health-check. The normal way to ship. | laptop | the gateway's app dir, the service |
| `deploy_interactive.sh` | The same deploy for a coworker without NOPASSWD sudo: one SSH master connection and one sudo prompt. Its header still names the old `bioagent` unit; prefer `sync_deploy.sh`. | laptop | as above |
| `migrate_prod_root.sh` | One-off move of the deployment root (`/data/BioAgent` → `/data/aiscientist`) and rename of the systemd unit, with preflight, `--apply`, `--rollback` and `--harden`. Done on 2026-10-09. | gateway (root) | the deployment root, the unit |
| `install_frameworks.sh` | Install the Biomni runtime under `/data`. Kept for reference; Biomni is no longer the research backend. | gateway (service account) | `/data` |
| `inject_admin.py` | Re-seed an admin account directly in the database when `aiscientist-admin` cannot run. | gateway | the `users` table |
| `backfill_run_conversation_id.py` | Fill `runs.conversation_id` for runs that finished before the column existed. Dry run unless `--commit`. | gateway | the `runs` table (with `--commit`) |
| `write_environment_doc.py` | Regenerate `docs/ENVIRONMENT.md` from the same manifest the `describe_environment` tool serves. | laptop | `docs/ENVIRONMENT.md` |

## Monitor and inspect

| Script | What it is for | Runs on | Changes |
|---|---|---|---|
| `inspect_run.py` | Collect the record of one research run from the gateway host and HPC3: the run directory, the event log, each round's Critic verdict, the database rows and every LLM call, the run's Slurm jobs (`sacct`), its HPC3 workspace and job scripts. Each command is stored with its verbatim output in `run_inspect/<run_id>/` (one `<id>.json` each, all of them in `record.txt`). `--watch N` polls the run's progress and the user's Slurm queue every N seconds until it finishes, then takes the snapshot. `--list` shows recent runs. | laptop | read-only (a temporary copy of `inspect_run_db.py` in `/tmp` on the gateway when the deployed app lacks it) |
| `inspect_run_db.py` | What the app database holds for one run: the `runs` row, its conversation and messages, the LLM calls by role and in time order (`--llm-jsonl` for a timeline, `--messages-json` for the full chat). `inspect_run.py` calls it; it can also be run by hand as the service account. | gateway (service account) | read-only |
| `e2e_prod_drive.py` | Drive a deployed gateway headlessly through the browser's own HTTP and WebSocket surface (connect, plan, run, report, continue) on a dataset already on dfs3b, and record everything with `--record`. The end-to-end test that unit tests cannot replace. | laptop or gateway | starts a real run and real HPC3 jobs |
| `backend_debug_run.py` | Run the research backend (Biomni era) headlessly against a live model endpoint and write one diagnostic log; can release its Slurm job at the end. | gateway | starts a run; may `scancel` the job it was given |
| `upload_speed_probe.py` | Measure browser-to-gateway tus upload throughput with real files, per network, chunk size and concurrency. | laptop | uploads test files |

Typical use while a run is in progress, and afterwards:

```bash
python scripts/inspect_run.py --list
python scripts/inspect_run.py <run_id> --watch 30
python scripts/inspect_run.py <run_id> --only sacct rounds db
```

`run_inspect/` holds the run's question and chat messages; it is git-ignored. HPC3 sweeps
`Temp/<user>/` three days after a run, so job scripts and logs are only there for that long; take the
snapshot before then.

## HPC3 setup (one-time)

| Script | What it is for | Runs on | Changes |
|---|---|---|---|
| `hpc3_vllm_setup.sh` | Pull the vLLM image and model into the shared lab area and print the serve command `gateway/gpu.py` generates. | HPC3 interactive compute node | `software/AiScientist/{containers,hf}` |
| `hpc3_vlreview_setup.sh` | Build the Qwen2.5-VL render-review container (`apptainer build --fakeroot`) and fetch its model. | HPC3 interactive compute node | `software/AiScientist/{containers,vlreview_model}` |

## Repository maintenance and publishing

| Script | What it is for | Runs on | Changes |
|---|---|---|---|
| `tool_docs.py` | Regenerate the generated sections of every `tools/<name>/TOOL.md` and the tool index. Run after changing a tool's schema or description. | laptop | `src/aiscientist/tools/**/TOOL.md` |
| `publish_public_mirror.py` | Build the snapshot for the public mirror (git archive minus `.publicexclude`), refuse it if it contains a credential, sync it into a local clone. Never commits or pushes. | laptop | the mirror clone's working tree |
| `pr_review_gate.py` | CI gate (`.github/workflows/pr-review.yml`): scan a pull request's changed files for API keys and check the required files. | CI | read-only |
| `check_croissant.py` | Validate `croissant.json` files as AiScientist does: its own rules, then mlcroissant when installed (`pip install -e ".[metadata]"`). See `docs/development/metadata.md`. Exits 1 when one fails. | laptop or gateway | read-only |
| `refactor/split_tools.py` | Reorganise `src/aiscientist/tools` into one folder per tool, derived from the current files. | laptop | the working tree |
| `refactor/extract_repos.py` | Rehearse splitting the repository into AiScientist, AiScientist-tools and AiScientist-skills with history, in fresh clones under `--out`. See `docs/architecture/REPO_SPLIT.md`. | laptop | new clones only |

## Reference data and test data

| Script | What it is for | Runs on | Changes |
|---|---|---|---|
| `build_hpo_lexicon.py` | Build the bundled HPO lexicon (`hpo_lexicon.tsv.gz`) from the official `hp.json` for the free-text → HPO mapper. | laptop | the bundled lexicon |
| `fetch_genesets.py` | Download the GMT gene-set libraries the offline enrichment tools read. | a machine with internet | the gene-set directory |
| `h5ad_to_csv.py` | Turn an `.h5ad` into the cells-as-rows CSV (a `condition` column plus gene columns) that the older smoke QC + DE path reads. | gateway or laptop | the output CSV |
| `generate_upload_test_data.py` | Generate large upload test fixtures (single file, folder, archive) outside the repository. | laptop | the output directory |

## Model behaviour: probes, smoke tests, demos

These call a real model, either OpenRouter or the model served on HPC3, to measure what the unit
tests cannot. They start no HPC3 jobs unless stated.

| Script | What it measures |
|---|---|
| `dag_smoke_openrouter.py` | The DAG planner's prompts produce a parseable dependency graph and valid scheduling choices. |
| `dag_full_sim_openrouter.py` | A full DAG + human-in-the-loop run with every role on OpenRouter and the analysis tools run locally on synthetic data. |
| `dag_memory_openrouter.py` | Per-specialist memory across two runs: a lesson written in run 1 is recalled in run 2. |
| `lab_openrouter_demo.py` | The multi-agent lab on a real dataset with OpenRouter standing in for the HPC3 model. |
| `no_contrast_enrichment_openrouter.py` | The guard that drops enrichment on an annotated dataset with no experimental contrast. |
| `probe_exploration.py` | Whether a model can open a new research path from a result (Qwen against a frontier model). |
| `probe_plan_quality.py` | Planning quality against the DDX41 run, with the served model. |
| `probe_tool_audit.py` | Whether the model finds a tool's defect once it can read the tool's source. |
| `probe_relay.py` | Helper for the probes: posts prepared chat completions to a vLLM on an HPC3 node, from inside HPC3. |
| `hpo_mapper_smoke.py` | Free-text → HPO mapping with a real model: translation, negation, family history. |
| `biomni_local_smoke.py` | Biomni A1 against a local model, without the data lake. Historical. |

## RetiGene literature corpus (PaperQA)

Building and querying the PDF corpus behind the RetiGene haploinsufficiency task and `deep_literature`.
They run on a laptop with the UCI network or on HPC3 (the PaperQA and cache steps inside `paperqa.sif`),
and write only into the corpus directories and manifests they are given.

| Script | Step |
|---|---|
| `prepare_retigene_paper_corpus.py` | Collect the PMIDs of the RetiGene gene table and download the open-access PDFs. |
| `build_retigene_priority_corpus.py` | Build the journal-first corpus and manifest (publisher PDF, then PMC, then manual). |
| `try_journal_direct_pdf_downloads.py` | Try official journal PDF URLs for papers still missing. |
| `download_journal_pdfs_with_uci_vpn.py` | Download publisher and repository PDFs through the UCI network. |
| `recover_retigene_open_access_apis.py` | Recover missing PDFs from OpenAlex and Semantic Scholar open-access records. |
| `recover_molecular_vision_wayback.py` | Recover Molecular Vision PDFs from archived copies of the journal site. |
| `import_europepmc_pdf_urls.py` | Import signed Europe PMC PDF URLs captured in the browser. |
| `import_pmc_browser_captures.py` | Build PDFs from PMC scan pages captured in the browser. |
| `capture_remaining_pmc_html.py` | Capture full PMC article HTML for the remaining rows. |
| `import_ncbi_bookshelf_pdfs.py` | Import the official NCBI Bookshelf PDFs for GeneReviews rows. |
| `import_known_repository_pdfs.py` | Import individually verified repository PDFs. |
| `reconcile_invalid_selected_pdfs.py` | Drop unreadable PDFs from the selection and repair resolvable paths. |
| `audit_retigene_selected_pdfs.py` | Audit every selected PDF for completeness, identity and provenance. |
| `build_retigene_paperqa_manifest.py` | Turn the priority manifest into the PaperQA metadata manifest. |
| `refresh_paperqa_manifest_authors.py` | Refresh the manifest's citations with full author lists. |
| `build_paperqa_directory_index.py` | Build the PaperQA embedding index for the corpus. |
| `query_paperqa_index.py` | Ask the index a question. |
| `build_mechanism_cache.py` | Re-extract page text and sentence vectors for `extract_haploinsufficient_genes`. |

## Adding a script

Start it with a docstring (or comment block) that says what it is for, how to run it, where it runs
and what it changes, then add a row to the right table here. Never put a credential in a script or
its examples; use a placeholder such as `<password>` and read secrets from the environment.
