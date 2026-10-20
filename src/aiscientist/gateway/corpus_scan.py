"""The corpus scan's own GPU job, and the finished scans kept on the Eye Server.

``extract_haploinsufficient_genes`` has a language model write search requests and read every
passage they retrieve from the corpus. It used to call the session's served model, so a session on
the lab's shared model or on an API key could not run it at all, and a session with a GPU had its
model busy for an hour. Now the scan brings its own model: ONE Slurm job on the paid ``gpu``
partition starts vLLM on the job's card (:func:`vllm_prelude`), runs the scan in ``paperqa.sif``
against ``127.0.0.1``, and exits, which frees the card. The model is fixed (``corpus_scan_model``,
else the cluster sessions' model) and runs at temperature 0, so the tables do not depend on the
model the asking session uses.

A scan's tables depend only on the corpus, the scan's rules and that model. They are kept under
``<data root>/corpus_scans/<key>/`` (:func:`store`), keyed by a fingerprint of all three
(:func:`scan_key`): a later request for the same corpus reads them back (:func:`load`) without a
job, from any session and any model, and adding papers changes the key, so the next request scans
again. Deleting a key's folder forces a new scan.

    <data root>/corpus_scans/<key>/
    ├── meta.json                       the corpus fingerprint, rules digest, model, run, date
    ├── result.json                     the tool's result (counts, genes, warnings)
    └── haploinsufficiency/{genes,hits,requests}.csv
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import HPCSettings

OUT_SUBDIR = "haploinsufficiency"     # where the tool writes its tables under artifacts/
TABLES = ("genes.csv", "hits.csv", "requests.csv")
CACHE_DIRNAME = "corpus_scans"


# ---------------------------------------------------------------------------------------------
# The job's own model
# ---------------------------------------------------------------------------------------------

def scan_model(settings: HPCSettings) -> str:
    """The model the scan's job serves and judges with."""
    return settings.corpus_scan_model or settings.vllm_model


def _vllm_body(settings: HPCSettings, port_file: str) -> str:
    """The vLLM server, as the serve job runs it (same image, weights and runtime module), on the
    node's loopback only and with a short window: the judge's prompts are a few thousand tokens."""
    hf = settings.hf_home
    model = scan_model(settings)
    flags = [
        "--host 127.0.0.1",
        f'--port "$(cat {shlex.quote(port_file)})"',
        f"--served-model-name {model}",
        f"--max-model-len {settings.corpus_scan_max_model_len}",
        f"--gpu-memory-utilization {settings.vllm_gpu_mem_util}",
    ]
    if settings.vllm_quantization:
        flags.append(f"--quantization {settings.vllm_quantization}")
    if settings.vllm_reasoning_parser:
        flags.append(f"--reasoning-parser {settings.vllm_reasoning_parser}")
    if settings.corpus_scan_vllm_args:
        flags.append(settings.corpus_scan_vllm_args)
    return (
        f'export HF_HOME="{hf}"\n'
        "export HF_HUB_OFFLINE=1\n"
        "source /etc/profile.d/lmod.sh 2>/dev/null || true\n"
        f"module load {settings.container_module} 2>/dev/null || true\n"
        f"exec {settings.container_bin} exec --nv -B {hf}:{hf} --env HF_HOME={hf} --env HF_HUB_OFFLINE=1 "
        f"{settings.vllm_image} vllm serve {model} " + " ".join(flags) + "\n"
    )


def vllm_prelude(settings: HPCSettings, *, name: str, scratch: str, res_f: str) -> tuple[str, str]:
    """``(shell, payload env)`` for ``SlurmAnalysisExecutor.job_prelude``.

    The shell runs on the job's node before the scan's container: it picks a free port, starts vLLM
    in a session of its own (stopped when the job script exits), and waits until ``/v1/models``
    answers. When the model never comes up it writes a ``failed`` result with the reason and the
    path of the server's log and ends the job, so the gateway reports that instead of a scan that
    found no model. The payload env points the scan at the server
    (``AISCIENTIST_PAPERQA_LLM_BASE_URL``, read by the tool when its args carry no endpoint)."""
    port_file = f"{scratch}/{name}.port"
    vllm_log = f"{scratch}/{name}.vllm.log"
    body = base64.b64encode(_vllm_body(settings, port_file).encode("utf-8")).decode("ascii")
    group = settings.data_group()
    runner = f"sg {group} -c 'bash -s'" if group else "bash -s"
    timeout = max(60, int(settings.corpus_scan_ready_timeout_s))
    q_port, q_log, q_res = shlex.quote(port_file), shlex.quote(vllm_log), shlex.quote(res_f)

    def failed(why: str) -> str:
        payload = json.dumps({"status": "failed", "model_unreachable": False, "vllm_log": vllm_log,
                              "error": f"The scan's model did not start: {why}. Its log is {vllm_log}."})
        return f"  echo {shlex.quote('AISCIENTIST_RESULT_JSON ' + payload)} > {q_res}\n"

    shell = (
        "# The scan's own model (gateway/corpus_scan.py): vLLM on this job's card, loopback only.\n"
        'AISCIENTIST_PORT=""\n'
        "for _try in {1..50}; do\n"
        "  _cand=$(( (RANDOM % 20000) + 30000 ))\n"
        '  if ! (exec 3<>/dev/tcp/127.0.0.1/$_cand) 2>/dev/null; then AISCIENTIST_PORT=$_cand; break; fi\n'
        "done\n"
        f'[ -n "$AISCIENTIST_PORT" ] || AISCIENTIST_PORT={settings.serve_port}\n'
        f'echo "$AISCIENTIST_PORT" > {q_port}\n'
        f"setsid bash -c \"printf %s '{body}' | base64 --decode | {runner}\" > {q_log} 2>&1 &\n"
        "_vllm=$!\n"
        # `|| true` on each: the job script runs under `set -e`, which a failing kill in the trap
        # would turn into the job's exit status.
        "trap 'kill -TERM -- \"-$_vllm\" 2>/dev/null || true; pkill -TERM -s \"$_vllm\" 2>/dev/null || true' EXIT\n"
        f'echo "Starting {scan_model(settings)} on 127.0.0.1:$AISCIENTIST_PORT ($(date))"\n'
        "_ready=0\n"
        f"_deadline=$((SECONDS + {timeout}))\n"
        'while [ "$SECONDS" -lt "$_deadline" ]; do\n'
        '  if curl -sf -m 5 "http://127.0.0.1:$AISCIENTIST_PORT/v1/models" >/dev/null 2>&1; then _ready=1; break; fi\n'
        '  kill -0 "$_vllm" 2>/dev/null || break\n'
        "  sleep 5\n"
        "done\n"
        'if [ "$_ready" != 1 ]; then\n'
        '  if kill -0 "$_vllm" 2>/dev/null; then\n'
        f"{failed(f'it did not answer within {timeout} s')}"
        "  else\n"
        f"{failed('the server exited before it answered')}"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'echo "Model ready ($(date))"\n'
    )
    env = f"export AISCIENTIST_PAPERQA_LLM_BASE_URL=http://127.0.0.1:$(cat {q_port})/v1; "
    return shell, env


# ---------------------------------------------------------------------------------------------
# Which corpus a scan read
# ---------------------------------------------------------------------------------------------

# The files under the corpus root that decide the scan's input: the text cache it reads (sentences,
# chunks and their vectors, built by scripts/build_mechanism_cache.py) and the HGNC table it
# normalises symbols with. The manifest is hashed; these are compared by size and mtime.
_CORPUS_FILES = ("mechanism_cache/sentences.jsonl", "mechanism_cache/chunks.jsonl",
                 "mechanism_cache/chunk_vecs.npy", "hgnc_complete_set.txt")


def corpus_fingerprint(executor: Any, papers: str, manifest: str = "") -> "dict[str, str] | None":
    """What the scan would read on HPC3 right now, from one metadata command on the login node:
    the manifest's sha256 and the size and mtime of the corpus files. None when the corpus has no
    text cache (the scan would then extract the PDFs into its own run, and nothing is kept)."""
    if executor is None or not papers:
        return None
    root = os.path.dirname(papers.rstrip("/"))
    files = " ".join(shlex.quote(f) for f in _CORPUS_FILES)
    hash_cmd = f"sha256sum {shlex.quote(manifest)} 2>/dev/null | cut -c1-64; " if manifest else ""
    try:
        r = executor.exec(f"cd {shlex.quote(root)} && {hash_cmd}stat -c '%n %s %Y' {files} 2>/dev/null; true")
    except Exception:  # noqa: BLE001 - an unreachable cluster means: no reuse this time
        return None
    lines = [ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()]
    fp: dict[str, str] = {"root": root}
    if manifest and lines and re.fullmatch(r"[0-9a-f]{64}", lines[0]):
        fp["manifest_sha256"] = lines.pop(0)
    for ln in lines:
        name, _, rest = ln.partition(" ")
        if name in _CORPUS_FILES:
            fp[name] = rest
    if "mechanism_cache/chunks.jsonl" not in fp or "mechanism_cache/sentences.jsonl" not in fp:
        return None
    return fp


def scan_key(fingerprint: dict[str, str], rules: str, model: str) -> str:
    """The folder name of a scan: one corpus, one set of rules, one model."""
    blob = json.dumps({"corpus": fingerprint, "rules": rules, "model": model}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------
# Scans kept on the Eye Server
# ---------------------------------------------------------------------------------------------

def cache_root() -> Path:
    from . import dataset_storage
    return dataset_storage.data_root() / CACHE_DIRNAME


@dataclass
class StoredScan:
    key: str
    dir: Path
    result: dict[str, Any]
    meta: dict[str, Any]

    @property
    def tables(self) -> Path:
        return self.dir / OUT_SUBDIR


def load(key: str) -> "StoredScan | None":
    """The scan kept under ``key``, or None when there is none (or it is incomplete)."""
    d = cache_root() / key
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        result = json.loads((d / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (d / OUT_SUBDIR / "genes.csv").is_file() or not isinstance(result, dict):
        return None
    return StoredScan(key, d, result, meta if isinstance(meta, dict) else {})


def store(key: str, result: dict[str, Any], tables: Path, meta: dict[str, Any]) -> StoredScan:
    """Keep a finished scan: its result, its two tables (copied from ``tables``) and ``meta``. The
    folder appears whole or not at all (written beside it, then renamed into place)."""
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".tmp-{key}-{uuid.uuid4().hex[:8]}"
    (tmp / OUT_SUBDIR).mkdir(parents=True)
    try:
        for name in TABLES:
            if (tables / name).is_file():
                shutil.copy2(tables / name, tmp / OUT_SUBDIR / name)
        kept = {k: v for k, v in result.items() if k not in ("files", "execution_mode", "slurm_state")}
        (tmp / "result.json").write_text(json.dumps(kept, indent=1, default=str), encoding="utf-8")
        meta = {**meta, "key": key, "stored_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        (tmp / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
        final = root / key
        if final.exists():
            shutil.rmtree(final)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return StoredScan(key, final, kept, meta)


def copy_into(stored: StoredScan, artifacts: Path) -> None:
    """Put a kept scan's tables where a run's own scan would have written them."""
    out = artifacts / OUT_SUBDIR
    out.mkdir(parents=True, exist_ok=True)
    for name in TABLES:
        if (stored.tables / name).is_file():
            shutil.copy2(stored.tables / name, out / name)


# ---------------------------------------------------------------------------------------------
# The chat message it answers, and the table it answers with
# ---------------------------------------------------------------------------------------------

_HAPLO = re.compile(r"haplo-?\s?insufficien|单倍剂量不足|单倍体不足|剂量不足", re.I)
_LIST = re.compile(r"\bgenes\b|\blist\b|\ball\b|\bwhich\b|哪些|列出|所有|清单", re.I)


def asks_for_gene_list(question: str) -> bool:
    """A request for the corpus' haploinsufficient genes as a list ("What are haploinsufficient
    genes?", "which genes are haploinsufficient in RP"), not a question about one gene ("is PRPF31
    haploinsufficient?"), which the chat's corpus search answers."""
    q = question or ""
    return bool(_HAPLO.search(q) and _LIST.search(q))


def gene_rows(tables: Path) -> list[dict[str, Any]]:
    """genes.csv as rows for the chat: the gene, its first quote and that quote's page (from
    hits.csv), up to three PMIDs and the review flags that are set."""
    try:
        with (tables / "genes.csv").open(newline="", encoding="utf-8") as fh:
            genes = list(csv.DictReader(fh))
    except OSError:
        return []
    pages: dict[tuple[str, str], str] = {}
    try:
        with (tables / "hits.csv").open(newline="", encoding="utf-8") as fh:
            for h in csv.DictReader(fh):
                pages.setdefault((h.get("pmid") or "", (h.get("quote") or "").strip()), h.get("page") or "")
    except OSError:
        pass
    truthy = {"1", "true", "yes"}
    rows = []
    for g in genes:
        quote = (g.get("quote_1") or "").strip()
        pmids = [p for p in (g.get(f"pmid_{i}") or "" for i in (1, 2, 3)) if p.strip()]
        rows.append({
            "gene": g.get("gene") or "", "phenotype": g.get("phenotype") or "",
            "n_papers": g.get("n_papers") or "", "evidence": quote,
            "page": pages.get((g.get("pmid_1") or "", quote), ""), "pmids": pmids,
            "flags": [k[5:] for k, v in g.items() if k.startswith("flag_") and str(v).strip().lower() in truthy],
        })
    return rows
