#!/usr/bin/env python
"""In-container entrypoint for scGPT per-cell annotation (Route C GPU batch job).

Runs INSIDE the scGPT Singularity image ONLY (scgpt + torch live here, never in the
gateway's Python env). Invoked by ``bioagent.gateway.scgpt_job`` via:

    python /opt/scgpt/run_infer.py --input <query.h5ad> --model <model_dir> --out <out_dir>

Design (route B — "vendor + run unchanged"): we do NOT reimplement scGPT inference. The
image bundles the user's *validated* ``scGPT_refactor`` harness at ``/opt/scgpt`` and this
wrapper just arranges the directory layout those scripts expect (they use hardcoded
relative paths) and runs ``step1_preprocess.py`` + ``step2_inference.py`` unchanged, then
copies their ``predictions.csv`` out. Single source of truth = the user's refactor code.

I/O contract (must NOT change — the gateway depends on it; see
``src/bioagent/gateway/scgpt_job.py`` + ``settings.scgpt_entrypoint``):
  --input   query AnnData .h5ad  (step1 does vocab alignment + HVG + log1p)
  --model   dir with the pretrained reference model
            (best_model.pt, vocab.json, id2type.json, dev_train_args.yml)
  --out     writable dir; receives predictions.csv (per-cell: barcode, predictions,
            confidence) and run.log
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Where the .def vendored the user's harness (see deploy/scgpt/scgpt.def %files).
REFACTOR_SRC = Path(os.environ.get("BIOAGENT_SCGPT_REFACTOR", "/opt/scgpt/scGPT_refactor"))


def _run(cmd: list[str], cwd: Path) -> None:
    print(f"[scgpt] $ {' '.join(cmd)}  (cwd={cwd})", flush=True)
    subprocess.run(cmd, cwd=str(cwd), check=True)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="scGPT per-cell annotation (vendored refactor)")
    p.add_argument("--input", required=True, help="query AnnData .h5ad")
    p.add_argument("--model", required=True, help="reference model dir (best_model.pt, vocab.json, ...)")
    p.add_argument("--out", required=True, help="writable output dir (receives predictions.csv)")
    a = p.parse_args(argv)

    input_h5ad = Path(a.input).resolve()
    model_dir = Path(a.model).resolve()
    out_dir = Path(a.out).resolve()
    if not input_h5ad.is_file():
        raise SystemExit(f"--input not found: {input_h5ad}")
    if not (model_dir / "best_model.pt").is_file():
        raise SystemExit(f"--model dir has no best_model.pt: {model_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Work in a writable copy under --out (the only guaranteed-writable bind). The vendored
    # scripts run from their own dir and expect ../download/{query.h5ad,reference_model}.
    work = out_dir / "_scgpt_run"
    if work.exists():
        shutil.rmtree(work)
    refactor = work / "scGPT_refactor"
    shutil.copytree(REFACTOR_SRC, refactor)
    download = work / "download"
    download.mkdir(parents=True, exist_ok=True)
    # Symlink the bound read-only inputs into the layout the scripts hardcode.
    os.symlink(input_h5ad, download / "query.h5ad")
    os.symlink(model_dir, download / "reference_model")

    # Run the user's harness UNCHANGED: step1 preprocess -> step2 inference.
    _run([sys.executable, "step1_preprocess.py"], cwd=refactor)
    _run([sys.executable, "step2_inference.py"], cwd=refactor)

    predictions = refactor / "outdir_step2" / "predictions.csv"
    if not predictions.is_file():
        raise SystemExit(f"step2 wrote no predictions.csv at {predictions}")
    shutil.copyfile(predictions, out_dir / "predictions.csv")
    run_log = refactor / "outdir_step2" / "run.log"
    if run_log.is_file():
        shutil.copyfile(run_log, out_dir / "run.log")
    print(f"[scgpt] wrote {out_dir / 'predictions.csv'}", flush=True)


if __name__ == "__main__":
    main()
