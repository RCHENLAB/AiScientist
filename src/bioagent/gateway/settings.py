from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class HPCSettings:
    """Cluster + vLLM serving settings, all overridable by environment variables.

    Defaults follow the UCI RCIC HPC3 conventions documented in the repo README.
    The served model is the AWQ Qwen3.6 HF repo (``BIOAGENT_VLLM_MODEL``).
    """

    host: str = "hpc3.rcic.uci.edu"
    ssh_port: int = 22

    # DEDICATED DATA-TRANSFER HOST. RCIC's 2026-08-06 notice: the login nodes (login-i15/16/17)
    # are for logging in and submitting Slurm jobs ONLY — they are "not data transfer nodes", and
    # rsync/SFTP/rclone/wget belong on access-hpc3.rcic.uci.edu, whose processes they may otherwise
    # kill. Our staging (`put_file`/`get_file`) moves GB-scale VCFs and h5ad matrices, so it gets its
    # OWN connection here instead of riding the login-node session that `exec` uses.
    # MEASURED 2026-08-06: this host runs a RESTRICTED shell (`ssh … echo hi` -> "Command 'echo' not
    # allowed"), so ONLY the SFTP subsystem works — sbatch/squeue/module/singularity and the vLLM
    # tunnel must stay on `host`. It mounts the SAME $HOME and /dfs3b, so paths and authorized_keys
    # are identical and no extra setup is needed.
    # Set BIOAGENT_HPC_TRANSFER_HOST="" to disable the split and stage over the login node again.
    transfer_host: str = "access-hpc3.rcic.uci.edu"

    # Slurm GPU allocation. UCI RUIC20 lab charges GPU jobs to ruic20_lab_gpu.
    partition: str = "gpu"
    account: str | None = "ruic20_lab_gpu"
    # PIN the LLM to A100. Plain "gpu:1" hands out ANY card in the heterogeneous pool
    # (A30 / L40S / RTX6000 / A100), and the AWQ Qwen3.6-35B build is tuned for A100 — a
    # smaller/weaker card degrades or OOMs. Requesting the type (``gpu:A100:1``) makes Slurm
    # schedule ONLY on A100 (a longer queue is acceptable; correctness beats latency here).
    # Override per-cluster with BIOAGENT_SLURM_GRES if the exact gres type name differs
    # (check `sinfo -o '%G'` on HPC3); set "gpu:1" to go back to any-card.
    gres: str = "gpu:A100:1"
    cpus: int = 8
    mem_gb: int = 32
    time_limit: str = "02:00:00"
    # Optional Slurm node exclude list, e.g. to avoid too-small GPUs. Slurm has no
    # "gres type != X" syntax, so to get "any GPU except V100" you submit across
    # both GPU partitions (partition="gpu,gpu32", gres="gpu:1") and exclude the
    # V100 nodes here. Maps to `#SBATCH --exclude=<list>`; empty = no exclude.
    exclude: str | None = None
    # Optional Slurm feature constraint, e.g. to pin the 80GB A100 flavour so a large
    # --max-model-len (131072) actually fits — `gpu:A100:1` alone can land on a 40GB
    # card where vLLM aborts at startup ("no KV cache room"), leaving the tunnel dead.
    # Maps to `#SBATCH --constraint=<feature>`; empty = no constraint. Confirm the exact
    # feature name on your cluster with `sinfo -o '%n %f'` (RCIC HPC3 tags A100 memory
    # as node features). See BIOAGENT_SLURM_CONSTRAINT.
    constraint: str | None = None
    # Optional GPU RACE: submit several candidate serve jobs at once and use whichever is
    # ALLOCATED FIRST, scancelling the losers — so a session grabs the earliest-free card
    # instead of queueing on one scarce type. A ';'-separated list of `partition,gres[,account]`
    # (account omitted → falls back to `account` above). Empty = the single partition/gres/account
    # above (NO behaviour change). Example — race the free/preemptible 96GB RTX PRO 6000 Blackwell
    # against the paid A100, first-come-first-served:
    #   BIOAGENT_GPU_CANDIDATES="free-gpu32,gpu:RTX6000:1,ruic20_lab;gpu,gpu:A100:1,ruic20_lab_gpu"
    # MEASURED 2026-08-02 (both serve this image+model at --max-model-len 262144):
    #   free-gpu32 gpu:RTX6000:1 = RTX PRO 6000 Blackwell 96GB (sm_120) -> 58.8 GiB KV, 11.58x
    #   gpu        gpu:A100:1    = A100 80GB PCIe            (sm_80)    -> 47.8 GiB KV,  9.41x
    # So the FREE card is the larger one here, and awq_marlin does run on Blackwell. The paid
    # partition still buys scheduling priority and is not preemptible — that, not capability, is
    # what the default is paying for. 7 RTX6000 nodes x 4 cards sit in free-gpu32.
    gpu_candidates: str = ""

    # STRICT per-user isolation. The console only ever finds, reuses, or stops
    # the *current user's own* serve job (named bioagent-vllm-<ucinetid> and
    # filtered with `squeue --me`). It never searches, reuses, or cancels another
    # lab member's jobs — one project must never be able to affect another.
    lab_storage: str = "/dfs3b/ruic20_lab"   # research data lives here
    lab_data_group: str = "ruic20_hpc"        # `newgrp ruic20_hpc` before touching lab_storage

    # --- SHARED project root: everything AiScientist owns on HPC3 ---------------
    # ONE directory holds all of it — the durable assets (containers/, hf/, envs/, *_model/) AND
    # each member's working files. It is the SAME dir the images above live in: it was
    # `software/bioagent` until 2026-08-08 and was renamed to match the product name, with
    # `software/bioagent` left behind as a symlink so a prod .env or an out-of-repo script that
    # still says `bioagent` keeps resolving (same zero-downtime trick as the BIOAGENT_*/AISCIENTIST_*
    # env aliases — see CLAUDE.md).
    #
    # NOT `/dfs3b/ruic20_lab/AiScientist`, which is where this would naturally go: that top level is
    # `drwxr-s--- ruic20 ruic20_hpc`, so group members cannot mkdir there (verified — `newgrp`/`sg`
    # do not help, since supplementary groups already count for the access check and the group
    # simply has no `w`). `software/` IS group-writable (2775), which is where the lab actually puts
    # shared things.
    #
    # Split by lifetime — per-user throughout, so one member can never reach another's files:
    #   <shared_root>/Temp/<user>/...     process files — SWEPT after temp_ttl_days (see hpc_gc.py)
    #   <shared_root>/uploads/<user>/...  raw uploaded data — NEVER swept
    #   <shared_root>/pysrc/<user>/...    the synced bioagent source (rewritten in place each connect)
    #   <shared_root>/bin/<user>/         the staged sweeper
    #   <shared_root>/{containers,hf,envs,scgpt_model,vlreview_model,ollama}/   assets, NEVER swept
    # The sweeper is hard-guarded to `<root>/Temp` and cannot walk into the assets. Nothing outside
    # <shared_root> is ever deleted automatically: a member's personal <lab_storage>/<ucinetid>/ dir
    # is read/browsed but left strictly alone.
    shared_root: str = "/dfs3b/ruic20_lab/software/AiScientist"
    # Age (days) after which a <shared_root>/Temp/<user>/<kind>/<run> dir whose whole subtree has
    # gone untouched is removed. 0 disables the sweep entirely.
    temp_ttl_days: int = 3

    # --- LLM serving backend ------------------------------------------------
    # "vllm" (Singularity container, OpenAI-compatible /v1) — the default and only
    # supported serving backend. "sglang" is reserved for a future engine swap that
    # reuses the SAME /v1 client (vllm_client) and only changes the serve command.
    # Everything downstream (Biomni, providers) is backend-agnostic (all OpenAI /v1).
    llm_backend: str = "vllm"

    # vLLM (runs inside a Singularity container on the GPU node; serves /v1).
    # AWQ INT4 (~24GB) is the A100-optimal + smallest-disk Qwen3.6-35B-A3B build,
    # and runs on the whole heterogeneous GPU pool (A30/L40S/A100/RTX6000) via
    # INT4 Marlin kernels — unlike FP8, which is native only on L40S (Ada).
    vllm_model: str = "QuantTrio/Qwen3.6-35B-A3B-AWQ"
    vllm_image: str = "/dfs3b/ruic20_lab/software/AiScientist/containers/vllm.sif"
    hf_home: str = "/dfs3b/ruic20_lab/software/AiScientist/hf"  # HF cache on shared DFS, NOT $HOME
    # The model's NATIVE window (config.json max_position_embeddings = 262144) — no YaRN, no
    # rope scaling. The old 32768 default was justified by "A100-40G leaves ~16GB KV", and both
    # halves of that were wrong. MEASURED on HPC3 2026-08-02 by booting this exact image+model
    # at --max-model-len 262144 (see the ctxprobe logs):
    #   A100 80GB  (partition gpu,       gpu:A100:1)  -> 47.8 GiB KV = 2,466,442 tokens, 9.41x
    #   RTX PRO 6000 Blackwell 96GB (free-gpu32)      -> 58.8 GiB KV = 3,035,461 tokens, 11.58x
    # HPC3's A100s are 80GB, not 40GB; and this Qwen3.6 is a HYBRID — `layer_types` is
    # linear_attention except every 4th layer, so only 10 of 40 layers hold a KV cache
    # (~20 KiB/token). A full 262K context costs ~5 GiB of KV, not tens of GiB. The window was
    # never the binding constraint; 9-11x concurrency at FULL length is the real headroom.
    # Lower this only to trade window for concurrency, never "to make it fit".
    vllm_max_model_len: int = 262144
    vllm_gpu_mem_util: float = 0.92
    vllm_quantization: str = "awq_marlin"  # AWQ INT4 on Ampere(A100); "" = let vLLM auto-detect
    vllm_tool_parser: str = "qwen3_coder"  # per the AWQ model card; needs vllm>=0.19
    vllm_reasoning_parser: str = "qwen3"   # parse Qwen3 thinking trace; "" to disable
    vllm_extra_args: str = ""              # escape hatch for extra `vllm serve` flags

    # Container runtime for the GPU serve job + code sandbox. RCIC HPC3 provides
    # SINGULARITY (module `singularity/<ver>`), NOT Apptainer — they share a CLI, but
    # the binary/module on HPC3 is `singularity`. See rcic.uci.edu user-installed software.
    container_bin: str = "singularity"
    container_module: str = "singularity/3.11.3"

    # --- scGPT per-cell annotation (GPU batch job, NOT a persistent server) -----
    # scGPT is a gene/expression transformer (not an LLM); we run its one-shot
    # reference-based annotation inference as a short-lived gpu:1 batch job via
    # scgpt_job.run_scgpt_inference, fully contained in this .sif. scgpt/torch live
    # ONLY in the image — never in the gateway's Python env. The image bundles the
    # scGPT_refactor step-2 code; ``scgpt_entrypoint`` is the in-container command,
    # given --input/--model/--out by the engine.
    scgpt_image: str = "/dfs3b/ruic20_lab/software/AiScientist/containers/scgpt.sif"
    # In-container CLI (deploy/scgpt/run_infer.py): runs the full reference flow
    # (step1 preprocess + step2 inference) given --input/--model/--out and writes
    # predictions.csv to --out.
    scgpt_entrypoint: str = "python /opt/scgpt/run_infer.py"
    # Reference model dir on shared DFS (best_model.pt, vocab.json, id2type.json,
    # dev_train_args.yml), bound read-only into the job. Staged by deploy/scgpt/build_and_stage.sh.
    scgpt_model_dir: str = "/dfs3b/ruic20_lab/software/AiScientist/scgpt_model"
    # scGPT inference is a short annotation job that runs fine on ANY card — keep it on gpu:1 so it
    # is NOT pinned to the (scarcer) A100 the LLM reserves. Decoupled from the main `gres`.
    scgpt_gres: str = "gpu:1"

    # --- Render-level VL review (GPU batch job, NOT a persistent server) --------
    # Qwen3.6 is text-only and cannot SEE render defects (text overlap, clipped cells, a
    # caption printed on the figure). A separate small vision model (Qwen2.5-VL-7B) audits the
    # rendered pdf as a short-lived gpu:1 batch job via vlreview_job.run_vlreview, fully
    # contained in this .sif. transformers/torch/VL-weights live ONLY in the image. Opt-in.
    vlreview_enabled: bool = False
    vlreview_image: str = "/dfs3b/ruic20_lab/software/AiScientist/containers/vlreview.sif"
    vlreview_entrypoint: str = "python /opt/vlreview/run_review.py"
    # VL weights on shared DFS, bound read-only. Staged by deploy/vlreview/build_and_stage.sh.
    vlreview_model_dir: str = "/dfs3b/ruic20_lab/software/AiScientist/vlreview_model"
    # Use the lab's paid GPU partition ("gpu") — the lab GPU account buys scheduling PRIORITY,
    # and the free/preemptible partitions (free-gpu/free-gpu32) queue too slowly, delaying the
    # report. Cost is bounded anyway: a cheap A30 for one short (~30 min) job. Set empty to fall
    # back to ``partition``; override to "free-gpu" only if you deliberately want zero-charge.
    vlreview_partition: str = "gpu"
    # Pin a CHEAP 24GB card (7B VL needs ~16GB); do NOT burn an A100 on layout review.
    # V100 (16GB, no flash-attn) is too tight — exclude it. RTX6000 also works: "gpu:RTX6000:1".
    vlreview_gres: str = "gpu:A30:1"
    vlreview_time_limit: str = "00:30:00"   # load + rasterize + review a few dozen pages
    vlreview_dpi: int = 200                 # rasterization DPI (200 catches text overlap)
    vlreview_max_iters: int = 3             # render -> review -> re-render passes before giving up

    # --- CodeAct (run_code) as a CPU Slurm batch job (opt-in) -------------------
    # By default run_code executes in the LOCAL CodeSandbox on the eyeserver (uncapped subprocess).
    # Set BIOAGENT_RUN_CODE_ON_HPC=1 to instead submit each snippet as a Singularity-contained CPU
    # batch job on HPC3, where `#SBATCH --mem` is a REAL, cgroup-enforced memory cap (the durable
    # fix for the OOM/-9 kills). Needs the dataset + run dirs on shared DFS and an analysis image.
    run_code_on_hpc: bool = False
    analysis_image: str = "/dfs3b/ruic20_lab/software/AiScientist/containers/analysis.sif"
    cpu_partition: str = "standard"          # RCIC HPC3 free CPU partition (no GPU)
    cpu_account: str | None = "ruic20_lab"   # CPU jobs charge the lab's non-GPU account
    run_code_mem_gb: int = 64                # per-snippet memory cap on HPC3
    run_code_cpus: int = 8
    run_code_time_limit: str = "01:00:00"

    # --- Uploads land on HPC3 dfs3b, not the eyeserver (opt-in) -----------------
    # Off: uploaded datasets are written to the eyeserver's per-user workspace. Set
    # BIOAGENT_UPLOADS_ON_HPC=1 to stream each upload straight to the user's HPC3 area
    # (<lab_storage>/<user>/uploads) over the SSH session, so raw data never lives on the
    # eyeserver. Needs a connected session (the SSH executor, which /api/connect brings up
    # together with the GPU). Tools that still run locally stage the file back on demand;
    # once analysis moves to HPC3 they read dfs3b in place with no round-trip.
    uploads_on_hpc: bool = False

    # --- scanpy analysis line runs as HPC3 CPU Slurm jobs (opt-in) --------------
    # Off: run_scanpy_qc/clustering/de/enrichment + the dataset preflight execute IN-PROCESS on
    # the eyeserver. Set BIOAGENT_ANALYSIS_ON_HPC=1 to submit each as a Singularity-contained CPU
    # batch job on HPC3 (SlurmAnalysisExecutor), reading the dataset on dfs3b in place — the real
    # memory cap is Slurm --mem, and the gateway host stays a thin I/O layer. Reuses analysis_image
    # + cpu_partition/cpu_account/run_code_mem_gb/run_code_cpus. Falls back in-process on failure.
    analysis_on_hpc: bool = False

    # --- report render (pandoc/xelatex) runs as an HPC3 CPU Slurm job (opt-in) --
    # Off: build_pdf_report shells out to pandoc/xelatex on the eyeserver (a multi-GB texlive install
    # + minutes of CPU per report). Set BIOAGENT_REPORT_ON_HPC=1 to render each report as a
    # Singularity-contained CPU batch job on HPC3 (SlurmReportRenderer) using a deps-only
    # pandoc/texlive image — no texlive on the eyeserver. Falls back to local pandoc on failure.
    report_on_hpc: bool = False
    report_image: str = "/dfs3b/ruic20_lab/software/AiScientist/containers/report.sif"

    # --- VCF variant annotation runs OFFLINE on HPC3 (opt-in) -------------------
    # Off: annotate_variants uses the public Ensembl VEP REST API on the eyeserver — fine for a small
    # VCF but it reads the whole file into memory, caps at 500 variants, and is rate-limited, so it
    # can't scale to a WGS-size VCF. Set BIOAGENT_VARIANT_ON_HPC=1 to run the OFFLINE line
    # (bcftools PASS-filter -> `vep --offline --cache --fork` over a local cache -> stream-parse) as a
    # Singularity-contained CPU batch job on HPC3, reading the VCF on dfs3b in place. No network is
    # needed at run time (the cache is bind-mounted) — the analysis container now allows network
    # on-demand (BIOAGENT_SANDBOX_NETWORK, default on) but the offline line doesn't use it. Reuses
    # SlurmAnalysisExecutor + cpu_partition/cpu_account/run_code_mem_gb. Falls
    # back to the REST path in-process on failure (small VCFs still work when HPC is unavailable).
    variant_on_hpc: bool = False
    vep_image: str = "/dfs3b/ruic20_lab/software/AiScientist/containers/vep.sif"
    # VEP cache + ClinVar VCF per assembly, staged on dfs3b by deploy/vep/build_and_stage.sh. The
    # cache is a bind-mounted local directory (offline); ClinVar is added via VEP --custom so the
    # offline path reproduces the REST tool's pathogenicity output.
    # Annotation DBs live in the lab's SHARED reference dir (Jin Li's convention: download each DB
    # once, mount read-only, reuse across projects) — NOT under a bioagent-private path.
    vep_cache_dir_grch38: str = "/dfs3b/ruic20_lab/software/reference/vep_annotation/GRCh38"
    vep_cache_dir_grch37: str = "/dfs3b/ruic20_lab/software/reference/vep_annotation/GRCh37"
    vep_clinvar_grch38: str = "/dfs3b/ruic20_lab/software/reference/vep_annotation/clinvar_GRCh38.vcf.gz"
    vep_clinvar_grch37: str = "/dfs3b/ruic20_lab/software/reference/vep_annotation/clinvar_GRCh37.vcf.gz"
    # Fallback assembly used ONLY when the build can't be read from the VCF header — the gateway
    # auto-detects GRCh37/GRCh38 from the header (chr1 contig length) and overrides this per run
    # (see app.py `_detect_vcf_assembly`). GRCh37 is the right fallback for an ophthalmology lab:
    # most eye/IRD datasets are on GRCh37/hg19, so an undetectable-header VCF is more likely 37.
    vep_assembly: str = "GRCh37"            # header auto-detection wins; this is only the fallback
    # Known-gene panel applied by DEFAULT for a variant study — deterministic known-gene-first (Rui
    # Chen's IRD protocol) instead of relying on the LLM to pass a panel. A name in
    # bioagent.tools.gene_panels (e.g. "ird"); empty = annotate genome-wide. A caller-supplied
    # `genes`/`regions_bed` OVERRIDES it (this rides in inject_args, which the caller wins over).
    default_gene_panel: str = ""
    # Default rarity floor injected as `max_pop_af` for a variant study (0 = off; the lab's IRD base
    # cutoff is 0.005 — keep variants below 0.5%). A caller-supplied max_pop_af overrides it. The
    # stricter per-gene disease-model thresholds (dominant ≤1e-4 / recessive ≤5e-3) are applied later
    # by tools.ird_prioritize, not here. See docs/ird_filter_spec.md.
    default_max_pop_af: float = 0.0
    # Pre-VEP region restriction — the COMPUTE-SAVING panel. A BED that bcftools uses to restrict the
    # VCF BEFORE VEP annotates it, so VEP only sees panel variants (annotation drops from ~45-60 min on
    # a WGS VCF to minutes). This is different from default_gene_panel (`genes`), which filters AFTER
    # VEP and so does NOT reduce annotation time. Point at the assembly-matched IRD capture BED — e.g.
    # the lab's retcap_v5_final_1_Covered.bed with its UCSC `browser`/`track` header lines stripped.
    # Empty = whole genome. A caller-supplied `regions_bed` overrides it.
    default_regions_bed: str = ""
    # IRD annotation layers (HGMD / retina-specific exon / retina ATAC / dbscSNV splice) — tabix lookups
    # against the lab's staged reference files (Rui Chen authorized reuse; HGMD is a public version).
    # OFF by default. HGMD + dbscSNV are already tabix-indexed on HPC3 → default to their located paths;
    # the retina-exon BED and ATAC narrowPeak need bgzip+tabix (or a memory loader) first, so they
    # default empty — set BIOAGENT_IRD_RETINA_EXONS / _ATAC once prepped. See docs/ird_filter_spec.md.
    ird_annotate_enabled: bool = False
    _IRD = "/dfs3b/ruic20_lab/chen/pipeline_restructure/pipeline_restructure"
    ird_hgmd: str = f"{_IRD}/bin/annotate_filter/HGMD_v.12-20-2016.SNVs.INDELs.parsedforVCFannotationandindexingfixed.txt.gz"
    ird_dbscsnv: str = f"{_IRD}/bin/dbNSFP3.5a/dbscSNV1.1.{{chrom}}"   # per-chromosome tabix template
    ird_retina_exons: str = ""
    ird_atac: str = ""
    # Predictor plugins + normalization reference — all OFF/empty by default; set once the data is
    # staged (deploy/vep/stage_annotation_dbs.sh). Paths are on dfs3b and bind-mounted into vep.sif.
    # Predictor plugins: OFF by default (master switch), but the data-file paths default to the
    # shared-reference locations the staging script writes — so enabling in prod is just
    # BIOAGENT_VEP_PLUGINS=1 (no need to re-specify every path).
    vep_plugins_enabled: bool = False       # master switch for CADD/AlphaMissense/REVEL + MANE
    _VA = "/dfs3b/ruic20_lab/software/reference/vep_annotation"
    vep_plugins_dir: str = f"{_VA}/plugins/vep_plugins"                          # VEP --dir_plugins (.pm scripts)
    vep_cadd_snv: str = f"{_VA}/plugins/cadd/whole_genome_SNVs.tsv.gz"           # CADD (+ .tbi alongside)
    vep_cadd_indels: str = ""                                                    # optional CADD indels
    vep_alphamissense: str = f"{_VA}/plugins/alphamissense/AlphaMissense_hg38.tsv.gz"
    vep_revel: str = f"{_VA}/plugins/revel/new_tabbed_revel_grch38.tsv.gz"
    vep_ref_fasta: str = f"{_VA}/ref/Homo_sapiens.GRCh38.dna.primary_assembly.fa"  # norm + HGVS
    # --- GRCh37 counterparts -------------------------------------------------------------------
    # Predictor data is BUILD-SPECIFIC: a GRCh38 file on a GRCh37 run is not merely useless, it is
    # unsafe — the tabix lookup is by coordinate, so a position that happens to exist in both builds
    # with the same ref/alt returns the score of a DIFFERENT variant. So each build gets its own path
    # and an empty value means "not staged for this build" -> that predictor is simply skipped
    # (``vep_plugin_flags`` already drops any path that does not exist).
    # CADD GRCh37 IS staged (80 GB, verified on HPC3 2026-07-17); the rest are not yet.
    vep_cadd_snv_grch37: str = f"{_VA}/plugins/cadd/whole_genome_SNVs.grch37.tsv.gz"
    vep_cadd_indels_grch37: str = ""
    vep_alphamissense_grch37: str = ""   # upstream publishes an hg19 file; download to enable
    vep_revel_grch37: str = ""           # plugins/revel/revel_with_transcript_ids carries BOTH builds — needs a rebuild
    vep_ref_fasta_grch37: str = ""       # no GRCh37 FASTA staged -> norm/HGVS stay OFF on GRCh37
    # SpliceAI (OpenSpliceAI) — a splice-disruption scorer that runs from its OWN conda env on HPC3
    # (torch; not in vep.sif). OFF by default; enable in prod with BIOAGENT_SPLICEAI=1. The env + model
    # dir are on dfs3b and bind-mounted into vep.sif at run time (validated: the conda-forge python runs
    # under vep.sif's glibc). ~50 s/variant on CPU ⇒ it runs only on the reduced set, capped.
    spliceai_enabled: bool = False
    spliceai_bin: str = "/dfs3b/ruic20_lab/software/AiScientist/envs/openspliceai/bin/openspliceai"
    spliceai_models: str = "/dfs3b/ruic20_lab/software/reference/spliceai/OSAI-MANE-10000nt"
    spliceai_max_variants: int = 0          # 0 = NO cap (default); set >0 as an optional safety valve
    #                                         to skip SpliceAI if the post-filter set is still huge
    vep_fork: int = 8                       # VEP --fork width == cpus-per-task on the CPU node
    vep_time_limit: str = "04:00:00"        # a WGS VCF forks through in ~30-60 min; request generous
    #                                         headroom — a batch job frees the node the instant its
    #                                         script exits and the gateway job-wait auto-tracks this
    #                                         --time, so a long request costs nothing for a normal run.

    # --- phenotype → disease differential (LIRICAL) ---------------------------------------------------
    # DOWNSTREAM of the variant line: LIRICAL fuses the patient's HPO terms with the VCF findings into a
    # per-disease post-test probability (Rui Chen's confidence ask). OFF until the sif + data are staged
    # (deploy/lirical/build_and_stage.sh); when off, the phenotype step reports not_installed and the run
    # continues without the differential. Genotype-aware scoring needs an Exomiser DB (>= 2302 — the
    # lab's existing 1805_hg19/exomiser-10.1.0 data is too old for LIRICAL v2); leave the exomiser_* paths
    # empty for phenotype-only, which needs no Exomiser DB. See deploy/lirical/README.md.
    phenotype_on_hpc: bool = False
    lirical_image: str = "/dfs3b/ruic20_lab/software/AiScientist/containers/lirical.sif"
    lirical_data_dir: str = "/dfs3b/ruic20_lab/software/reference/lirical/data"
    lirical_exomiser_hg19: str = ""         # e.g. .../reference/lirical/exomiser/2302_hg19 (optional)
    lirical_exomiser_hg38: str = ""         # e.g. .../reference/lirical/exomiser/2302_hg38 (optional)
    # MEASURED on HPC3 2026-07-15, genotype-aware, on a REAL 1.13 GB WGS VCF (CASE_A, 4,928,515
    # variants, standard partition, 4 CPUs): the whole job took **4m22s** — Exomiser streamed the
    # callset at ~21.6k variants/s (3m48s) and the disease pass added seconds. So 1h already carried
    # ~13x headroom and this does NOT need VEP's 4h: LIRICAL scores variants against the prebuilt
    # Jannovar/Exomiser stores, it does not re-run VEP's per-variant transcript annotation.
    # Phenotype-only is seconds. --time is a CEILING, not a reservation, so 1h costs nothing when the
    # job finishes in 5 min, and keeps better backfill priority than an inflated ask.
    lirical_time_limit: str = "01:00:00"
    lirical_cpus: int = 4
    # Its own knob: this used to borrow run_code_mem_gb, so tightening the CodeAct sandbox's memory
    # would have silently starved LIRICAL — unrelated things sharing a number.
    #
    # Why 64 when the MEASURED peak is 7.9 GB (MaxRSS on the WGS run above — Exomiser mmaps the ~28 GB
    # store, so most of it is page cache, not RSS): the sif's launcher is a bare ``java -jar`` with NO
    # ``-Xmx``, and the JVM — despite reporting UseContainerSupport=true — sized MaxHeapSize at 32 GB,
    # i.e. 1/4 of the NODE's 187 GB, NOT of this --mem. The heap ceiling and --mem are therefore
    # independent, and a JVM that decided to grow into its 32 GB would be OOM-KILLED by Slurm if --mem
    # sat below it. So --mem stays above the heap the JVM believes it owns. Pinning -Xmx in the image
    # (a rebuild) would let this drop to ~16 GB and schedule better.
    lirical_mem_gb: int = 64

    # A2 resume keeps each run's analysis checkpoints (work/adata_*.h5ad) so a follow-up can re-run
    # one step without redoing the whole pipeline. Those densified matrices are large, so they auto-
    # expire after this many days (only the CHECKPOINTS — never artifacts/reports). 0 disables the
    # sweep (keep forever). After expiry, step-level "continue" needs the study re-run once; the
    # report + regenerate still work (they read artifacts/, which never expire).
    checkpoint_ttl_days: int = 7

    # vLLM serve job networking (the GPU node binds a DYNAMIC port; these are the
    # fallback + the local tunnel end).
    serve_port: int = 11434           # last-resort bind port if the dynamic pick fails
    # Local end of the SSH tunnel to the GPU node's vLLM /v1. 0 = pick a free ephemeral
    # port per session (default; safe for many concurrent users). Set a FIXED port so an
    # external tool reading a static base_url can reach vLLM without a manual tunnel.
    # NOTE: a fixed port only serves ONE active session at a time.
    local_tunnel_port: int = 0
    serve_home: str = "$HOME/.bioagent"   # where serve.sbatch + the port file live

    # health monitoring
    gpu_poll_seconds: int = 20
    gpu_idle_util_threshold: int = 2  # %, below this while "serving" => suspicious

    def serving_model(self) -> str:
        """The HF repo id vLLM serves."""
        return self.vllm_model

    def data_group(self) -> str | None:
        """UNIX group to run commands under (``newgrp ruic20_hpc``), or None — returned
        only when the vLLM image / HF cache live under the lab DFS storage."""
        paths = (self.vllm_image, self.hf_home)
        if any(p.startswith(self.lab_storage) for p in paths):
            return self.lab_data_group or None
        return None

    @classmethod
    def from_env(cls) -> "HPCSettings":
        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            raw = os.environ.get(name)
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        return cls(
            host=os.environ.get("BIOAGENT_HPC_HOST", cls.host),
            ssh_port=_int("BIOAGENT_HPC_SSH_PORT", cls.ssh_port),
            # "" is a MEANINGFUL value here (= stage over the login node), so keep an explicitly
            # empty env var rather than falling back to the default transfer host.
            transfer_host=os.environ.get("BIOAGENT_HPC_TRANSFER_HOST", cls.transfer_host).strip(),
            partition=os.environ.get("BIOAGENT_SLURM_PARTITION", cls.partition),
            account=os.environ.get("BIOAGENT_SLURM_ACCOUNT", cls.account) or None,
            gres=os.environ.get("BIOAGENT_SLURM_GRES", cls.gres),
            cpus=_int("BIOAGENT_SLURM_CPUS_PER_TASK", cls.cpus),
            mem_gb=_int("BIOAGENT_SLURM_MEMORY_GB", cls.mem_gb),
            time_limit=os.environ.get("BIOAGENT_SLURM_TIME_LIMIT", cls.time_limit),
            exclude=os.environ.get("BIOAGENT_SLURM_EXCLUDE") or None,
            constraint=os.environ.get("BIOAGENT_SLURM_CONSTRAINT") or None,
            gpu_candidates=os.environ.get("BIOAGENT_GPU_CANDIDATES", cls.gpu_candidates),
            # New names, with backward-compat fallback to the old BIOAGENT_OLLAMA_* so a
            # deployed .env keeps working until it's updated.
            serve_port=_int("BIOAGENT_VLLM_PORT", _int("BIOAGENT_OLLAMA_PORT", cls.serve_port)),
            local_tunnel_port=_int("BIOAGENT_LOCAL_TUNNEL_PORT",
                                   _int("BIOAGENT_OLLAMA_LOCAL_PORT", cls.local_tunnel_port)),
            serve_home=(os.environ.get("BIOAGENT_SERVE_HOME")
                        or os.environ.get("BIOAGENT_OLLAMA_HOME") or cls.serve_home),
            gpu_poll_seconds=_int("BIOAGENT_GPU_POLL_SECONDS", cls.gpu_poll_seconds),
            checkpoint_ttl_days=_int("BIOAGENT_CHECKPOINT_TTL_DAYS", cls.checkpoint_ttl_days),
            scgpt_gres=os.environ.get("BIOAGENT_SCGPT_GRES", cls.scgpt_gres),
            lab_storage=os.environ.get("BIOAGENT_LAB_STORAGE", cls.lab_storage),
            lab_data_group=os.environ.get("BIOAGENT_LAB_DATA_GROUP", cls.lab_data_group),
            shared_root=(os.environ.get("BIOAGENT_HPC_SHARED_ROOT") or cls.shared_root).rstrip("/"),
            temp_ttl_days=_int("BIOAGENT_TEMP_TTL_DAYS", cls.temp_ttl_days),
            llm_backend=os.environ.get("BIOAGENT_LLM_BACKEND", cls.llm_backend).strip().lower(),
            vllm_model=os.environ.get("BIOAGENT_VLLM_MODEL", cls.vllm_model),
            vllm_image=os.environ.get("BIOAGENT_VLLM_IMAGE", cls.vllm_image),
            hf_home=os.environ.get("BIOAGENT_HF_HOME", cls.hf_home),
            vllm_max_model_len=_int("BIOAGENT_VLLM_MAX_MODEL_LEN", cls.vllm_max_model_len),
            vllm_gpu_mem_util=_float("BIOAGENT_VLLM_GPU_MEM_UTIL", cls.vllm_gpu_mem_util),
            vllm_quantization=os.environ.get("BIOAGENT_VLLM_QUANTIZATION", cls.vllm_quantization),
            vllm_tool_parser=os.environ.get("BIOAGENT_VLLM_TOOL_PARSER", cls.vllm_tool_parser),
            vllm_reasoning_parser=os.environ.get("BIOAGENT_VLLM_REASONING_PARSER", cls.vllm_reasoning_parser),
            vllm_extra_args=os.environ.get("BIOAGENT_VLLM_EXTRA_ARGS", cls.vllm_extra_args),
            container_bin=os.environ.get("BIOAGENT_CONTAINER_BIN", cls.container_bin),
            container_module=os.environ.get("BIOAGENT_CONTAINER_MODULE", cls.container_module),
            scgpt_image=os.environ.get("BIOAGENT_SCGPT_IMAGE", cls.scgpt_image),
            scgpt_entrypoint=os.environ.get("BIOAGENT_SCGPT_ENTRYPOINT", cls.scgpt_entrypoint),
            scgpt_model_dir=os.environ.get("BIOAGENT_SCGPT_MODEL_DIR", cls.scgpt_model_dir),
            vlreview_enabled=(os.environ.get("BIOAGENT_VLREVIEW_ENABLED", "").strip().lower()
                              in ("1", "true", "yes", "on")),
            vlreview_image=os.environ.get("BIOAGENT_VLREVIEW_IMAGE", cls.vlreview_image),
            vlreview_entrypoint=os.environ.get("BIOAGENT_VLREVIEW_ENTRYPOINT", cls.vlreview_entrypoint),
            vlreview_model_dir=os.environ.get("BIOAGENT_VLREVIEW_MODEL_DIR", cls.vlreview_model_dir),
            vlreview_partition=os.environ.get("BIOAGENT_VLREVIEW_PARTITION", cls.vlreview_partition),
            vlreview_gres=os.environ.get("BIOAGENT_VLREVIEW_GRES", cls.vlreview_gres),
            vlreview_time_limit=os.environ.get("BIOAGENT_VLREVIEW_TIME_LIMIT", cls.vlreview_time_limit),
            vlreview_dpi=_int("BIOAGENT_VLREVIEW_DPI", cls.vlreview_dpi),
            vlreview_max_iters=_int("BIOAGENT_VLREVIEW_MAX_ITERS", cls.vlreview_max_iters),
            run_code_on_hpc=(os.environ.get("BIOAGENT_RUN_CODE_ON_HPC", "").strip().lower()
                             in ("1", "true", "yes", "on")),
            uploads_on_hpc=(os.environ.get("BIOAGENT_UPLOADS_ON_HPC", "").strip().lower()
                            in ("1", "true", "yes", "on")),
            analysis_on_hpc=(os.environ.get("BIOAGENT_ANALYSIS_ON_HPC", "").strip().lower()
                             in ("1", "true", "yes", "on")),
            report_on_hpc=(os.environ.get("BIOAGENT_REPORT_ON_HPC", "").strip().lower()
                           in ("1", "true", "yes", "on")),
            report_image=os.environ.get("BIOAGENT_REPORT_IMAGE", cls.report_image),
            analysis_image=os.environ.get("BIOAGENT_ANALYSIS_IMAGE", cls.analysis_image),
            cpu_partition=os.environ.get("BIOAGENT_CPU_PARTITION", cls.cpu_partition),
            cpu_account=os.environ.get("BIOAGENT_CPU_ACCOUNT", cls.cpu_account) or None,
            run_code_mem_gb=_int("BIOAGENT_RUN_CODE_MEM_GB", cls.run_code_mem_gb),
            run_code_cpus=_int("BIOAGENT_RUN_CODE_CPUS", cls.run_code_cpus),
            run_code_time_limit=os.environ.get("BIOAGENT_RUN_CODE_TIME_LIMIT", cls.run_code_time_limit),
            variant_on_hpc=(os.environ.get("BIOAGENT_VARIANT_ON_HPC", "").strip().lower()
                            in ("1", "true", "yes", "on")),
            vep_image=os.environ.get("BIOAGENT_VEP_IMAGE", cls.vep_image),
            vep_cache_dir_grch38=os.environ.get("BIOAGENT_VEP_CACHE_DIR_GRCH38", cls.vep_cache_dir_grch38),
            vep_cache_dir_grch37=os.environ.get("BIOAGENT_VEP_CACHE_DIR_GRCH37", cls.vep_cache_dir_grch37),
            vep_clinvar_grch38=os.environ.get("BIOAGENT_VEP_CLINVAR_GRCH38", cls.vep_clinvar_grch38),
            vep_clinvar_grch37=os.environ.get("BIOAGENT_VEP_CLINVAR_GRCH37", cls.vep_clinvar_grch37),
            vep_assembly=os.environ.get("BIOAGENT_VEP_ASSEMBLY", cls.vep_assembly),
            default_gene_panel=os.environ.get("BIOAGENT_DEFAULT_GENE_PANEL", cls.default_gene_panel),
            default_max_pop_af=_float("BIOAGENT_DEFAULT_MAX_POP_AF", cls.default_max_pop_af),
            default_regions_bed=os.environ.get("BIOAGENT_DEFAULT_REGIONS_BED", cls.default_regions_bed),
            ird_annotate_enabled=(os.environ.get("BIOAGENT_IRD_ANNOTATE", "").strip().lower()
                                  in ("1", "true", "yes", "on")),
            ird_hgmd=os.environ.get("BIOAGENT_IRD_HGMD", cls.ird_hgmd),
            ird_dbscsnv=os.environ.get("BIOAGENT_IRD_DBSCSNV", cls.ird_dbscsnv),
            ird_retina_exons=os.environ.get("BIOAGENT_IRD_RETINA_EXONS", cls.ird_retina_exons),
            ird_atac=os.environ.get("BIOAGENT_IRD_ATAC", cls.ird_atac),
            vep_plugins_enabled=(os.environ.get("BIOAGENT_VEP_PLUGINS", "").strip().lower()
                                 in ("1", "true", "yes", "on")),
            vep_plugins_dir=os.environ.get("BIOAGENT_VEP_PLUGINS_DIR", cls.vep_plugins_dir),
            vep_cadd_snv=os.environ.get("BIOAGENT_VEP_CADD_SNV", cls.vep_cadd_snv),
            vep_cadd_indels=os.environ.get("BIOAGENT_VEP_CADD_INDELS", cls.vep_cadd_indels),
            vep_alphamissense=os.environ.get("BIOAGENT_VEP_ALPHAMISSENSE", cls.vep_alphamissense),
            vep_revel=os.environ.get("BIOAGENT_VEP_REVEL", cls.vep_revel),
            vep_ref_fasta=os.environ.get("BIOAGENT_REF_FASTA", cls.vep_ref_fasta),
            vep_cadd_snv_grch37=os.environ.get("BIOAGENT_VEP_CADD_SNV_GRCH37", cls.vep_cadd_snv_grch37),
            vep_cadd_indels_grch37=os.environ.get("BIOAGENT_VEP_CADD_INDELS_GRCH37", cls.vep_cadd_indels_grch37),
            vep_alphamissense_grch37=os.environ.get("BIOAGENT_VEP_ALPHAMISSENSE_GRCH37", cls.vep_alphamissense_grch37),
            vep_revel_grch37=os.environ.get("BIOAGENT_VEP_REVEL_GRCH37", cls.vep_revel_grch37),
            vep_ref_fasta_grch37=os.environ.get("BIOAGENT_REF_FASTA_GRCH37", cls.vep_ref_fasta_grch37),
            spliceai_enabled=(os.environ.get("BIOAGENT_SPLICEAI", "").strip().lower()
                              in ("1", "true", "yes", "on")),
            spliceai_bin=os.environ.get("BIOAGENT_SPLICEAI_BIN", cls.spliceai_bin),
            spliceai_models=os.environ.get("BIOAGENT_SPLICEAI_MODELS", cls.spliceai_models),
            spliceai_max_variants=_int("BIOAGENT_SPLICEAI_MAX_VARIANTS", cls.spliceai_max_variants),
            vep_fork=_int("BIOAGENT_VEP_FORK", cls.vep_fork),
            vep_time_limit=os.environ.get("BIOAGENT_VEP_TIME_LIMIT", cls.vep_time_limit),
            phenotype_on_hpc=(os.environ.get("BIOAGENT_PHENOTYPE_ON_HPC", "").strip().lower()
                              in ("1", "true", "yes", "on")),
            lirical_image=os.environ.get("BIOAGENT_LIRICAL_IMAGE", cls.lirical_image),
            lirical_data_dir=os.environ.get("BIOAGENT_LIRICAL_DATA_DIR", cls.lirical_data_dir),
            lirical_exomiser_hg19=os.environ.get("BIOAGENT_LIRICAL_EXOMISER_HG19",
                                                 cls.lirical_exomiser_hg19),
            lirical_exomiser_hg38=os.environ.get("BIOAGENT_LIRICAL_EXOMISER_HG38",
                                                 cls.lirical_exomiser_hg38),
            lirical_time_limit=os.environ.get("BIOAGENT_LIRICAL_TIME_LIMIT", cls.lirical_time_limit),
            lirical_cpus=_int("BIOAGENT_LIRICAL_CPUS", cls.lirical_cpus),
            lirical_mem_gb=_int("BIOAGENT_LIRICAL_MEM_GB", cls.lirical_mem_gb),
        )
