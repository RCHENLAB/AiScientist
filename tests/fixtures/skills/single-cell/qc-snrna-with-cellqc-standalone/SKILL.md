---
name: qc-snrna-with-cellqc-standalone
description: Test fixture of the CellQC skill that run_cellqc names. Run the CellQC pipeline over a cohort of 10x Cell Ranger libraries.
license: MIT
metadata:
  category: single-cell
  version: "1.2.0"
  status: core
  image: "bioconda:cellqc=0.3.6 r-doubletfinder python=3.12"
  cpus: "16"
  mem_gb: "80"
  time_limit: "08:00:00"
  requires-network: "install"
---

# QC snRNA-seq with CellQC (test fixture)

The shape of the registry skill as `run_cellqc` reads it: the environment in `metadata`, the
reference settings in `assets/cellqc-defaults.json`.
