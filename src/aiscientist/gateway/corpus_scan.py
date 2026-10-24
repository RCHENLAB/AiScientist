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

What a researcher reviews is built here from the scan's sentences, after the job or from a kept
scan (:func:`write_curation`, RCHENLAB/AiScientist issue 46): one row per gene with the claim its
papers make (a gene they argue against is a row of its own claim, never one of the list) and one row
per sentence, each with its paper and page.

    <data root>/corpus_scans/<key>/
    ├── meta.json                       the corpus fingerprint, rules digest, model, run, date
    ├── result.json                     the tool's result (counts, genes, warnings)
    └── haploinsufficiency/{genes,hits,requests}.csv

    <run>/artifacts/curation/haploinsufficient_{genes,evidence}.csv   the curation tables
    <run>/curation_tables.json                                        their record, for saving them
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
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


# ---------------------------------------------------------------------------------------------
# The curation tables: the result a researcher reviews (RCHENLAB/AiScientist issue 46)
# ---------------------------------------------------------------------------------------------
# Built from the scan's hits.csv (every judged sentence), so a kept scan needs no new job and the
# scan's rules, and with them its key, do not change. One row per gene with the claim its papers
# make, and one row per sentence that bears on it. A gene the papers argue against is a row of its
# own claim, never one of the haploinsufficient genes.

CURATION_SUBDIR = "curation"           # under artifacts/, beside the scan's own tables
CURATION_GENES = "haploinsufficient_genes.csv"
CURATION_EVIDENCE = "haploinsufficient_evidence.csv"
CURATION_POINTER = "curation_tables.json"   # in the run's folder, outside artifacts/

# A gene's claim, strongest first. ``haploinsufficient``: a paper states it in so many words and none
# argues against it; ``contested``: one states it and another argues against it (in a human retinal
# disease, or with an animal model or another disease); ``suggested``: only hedged statements (may,
# suggests, consistent with); ``argued against``: papers on a human retinal disease say it is not, or
# unlikely to be, the mechanism, and none states it.
SUPPORTED, CONTESTED, SUGGESTED, AGAINST = "haploinsufficient", "contested", "suggested", "argued against"
CLAIMS = (SUPPORTED, CONTESTED, SUGGESTED, AGAINST)
# One sentence's statement. ``argues against elsewhere``: about a gene the curation already holds, a
# sentence against its haploinsufficiency from an animal model or another disease ("heterozygous
# Tspan12 mice show normal vascularization"). It makes a stated gene contested, never a gene argued
# against on its own: a heterozygous mouse without the human phenotype is common for dominant genes.
STATES, HEDGES, DENIES, DENIES_ELSEWHERE = "states", "hedged", "argues against", "argues against elsewhere"
AGAINST_ELSEWHERE_FLAG = "argued_against_in_animal_model_or_other_disease"

GENE_TABLE_COLUMNS = (
    "gene", "claim", "explicit", "negated", "phenotype", "quote", "pmid", "doi", "title", "page",
    "papers_stating", "papers_hedged", "papers_against", "pmids_stating", "pmids_against",
    "quote_against", "pmid_against", "page_against", "against_context",
    "conflicting_mechanism", "review_flags", "gene_as_written")
EVIDENCE_COLUMNS = (
    "gene", "claim", "statement", "explicit", "negated", "phenotype", "quote", "pmid", "doi", "title",
    "page", "sentence_id", "mechanism_statement", "gene_as_written", "review_flags", "found_by")

_TRUE = frozenset({"1", "true", "yes"})


def _yes(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE


def _read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return []


def papers_from_manifest(text: str) -> dict[str, dict[str, str]]:
    """PMID -> ``{"title", "doi", "citation"}`` from the corpus manifest (a CSV). The PMID is read as
    the corpus tools read it: ``doc_id``, else ``docname`` without "PMID", else the PDF's name."""
    out: dict[str, dict[str, str]] = {}
    try:
        rows = list(csv.DictReader(io.StringIO(text or "")))
    except csv.Error:
        return out
    for row in rows:
        pmid = str(row.get("doc_id") or "").strip()
        if not pmid and row.get("docname"):
            pmid = str(row["docname"]).removeprefix("PMID").strip()
        if not pmid and row.get("file_location"):
            pmid = Path(str(row["file_location"])).stem
        if not pmid:
            continue
        citation = " ".join(str(row.get("citation") or "").split())
        doi = str(row.get("doi") or "").strip()
        if not doi:
            m = re.search(r"\bDOI:\s*(\S+)", citation, re.IGNORECASE)
            doi = m.group(1).rstrip(".") if m else ""
        out[pmid] = {"title": " ".join(str(row.get("title") or "").split()), "doi": doi, "citation": citation}
    return out


def _bears_on_claim(h: dict[str, str]) -> bool:
    """A sentence that says something about the gene's haploinsufficiency in a human retinal disease,
    whose quote is a sentence of the paper and whose gene is known: what the curation is made of.
    (The scan's genes.csv is its explicit, not negated subset.)"""
    return (str(h.get("mechanism_class") or "") == "haploinsufficiency" and bool(str(h.get("gene") or "").strip())
            and str(h.get("gene_status") or "") != "missing"
            and _yes(h.get("quote_verified")) and _yes(h.get("human_retinal")))


def _argues_against_elsewhere(h: dict[str, str]) -> bool:
    """A sentence against a gene's haploinsufficiency outside a human retinal disease (an animal
    model, another disease), whose quote is a sentence of the paper and not a reference entry: counter
    evidence for a gene the curation already holds, never a gene of its own."""
    return (str(h.get("mechanism_class") or "") == "haploinsufficiency" and bool(str(h.get("gene") or "").strip())
            and str(h.get("gene_status") or "") != "missing" and _yes(h.get("negated"))
            and _yes(h.get("quote_verified")) and not _yes(h.get("human_retinal")) and not _yes(h.get("cite_like")))


def _statement(h: dict[str, str]) -> str:
    if _yes(h.get("negated")):
        return DENIES if _yes(h.get("human_retinal")) else DENIES_ELSEWHERE
    return STATES if _yes(h.get("explicit")) else HEDGES


def _sentence_flags(h: dict[str, str]) -> list[str]:
    """What a curator should check in a sentence (a question, not an exclusion)."""
    flags = []
    if _yes(h.get("cite_like")):
        flags.append("looks_like_a_reference")
    if "gene_in_quote" in h and not _yes(h.get("gene_in_quote")):
        flags.append("gene_not_in_quote")
    if str(h.get("gene_status") or "ok") not in ("ok", "unchecked"):
        flags.append("gene_alias_or_unknown")
    if _yes(h.get("garbled")):
        flags.append("garbled")
    if "dominance_marker" in h and not _yes(h.get("dominance_marker")):
        flags.append("no_dominance_marker")
    return flags


def _best_first(h: dict[str, str]) -> tuple:
    """The order a gene's quotes are shown in, as the scan picks them: body sentences before
    reference entries, clean before garbled, the gene in the quote, the word itself, then longer."""
    q = str(h.get("quote") or "")
    return (_yes(h.get("cite_like")), _yes(h.get("garbled")),
            "gene_in_quote" in h and not _yes(h.get("gene_in_quote")),
            "haploinsuffic" not in q.lower(), -len(q))


def _int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def curation_tables(tables: Path, papers: "dict[str, dict[str, str]] | None" = None,
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(gene rows, sentence rows)`` from a scan's tables (``hits.csv``, and ``genes.csv`` for the
    review flags it computed). ``papers`` (:func:`papers_from_manifest`) gives each paper's title and
    DOI. Gene rows come strongest first: the claim (:data:`CLAIMS`), then how many papers make it."""
    papers = papers or {}
    flagged = {str(g.get("gene") or ""): g for g in _read_rows(tables / "genes.csv")}
    by_gene: dict[str, list[dict[str, str]]] = {}
    other: dict[str, set[str]] = {}
    elsewhere: dict[str, list[dict[str, str]]] = {}
    for h in _read_rows(tables / "hits.csv"):
        gene = str(h.get("gene") or "").strip()
        if (gene and str(h.get("mechanism_class") or "") in ("dominant_negative", "gain_of_function")
                and _yes(h.get("explicit")) and not _yes(h.get("negated"))):
            other.setdefault(gene, set()).add(str(h["mechanism_class"]))
        if _bears_on_claim(h):
            by_gene.setdefault(gene, []).append(h)
        elif _argues_against_elsewhere(h):
            elsewhere.setdefault(gene, []).append(h)
    for gene, hs in elsewhere.items():       # counter evidence only for a gene the curation holds
        if gene in by_gene:
            by_gene[gene].extend(hs)

    def paper(pmid: str) -> dict[str, str]:
        return papers.get(pmid) or {}

    rows: list[dict[str, Any]] = []
    evidence: dict[str, list[dict[str, Any]]] = {}
    for gene, hs in by_gene.items():
        said = {s: sorted((h for h in hs if _statement(h) == s), key=_best_first)
                for s in (STATES, HEDGES, DENIES, DENIES_ELSEWHERE)}
        against = said[DENIES] + said[DENIES_ELSEWHERE]          # a human retinal disease first
        pmids = {s: list(dict.fromkeys(str(h.get("pmid") or "") for h in said[s])) for s in said}
        pmids_against = list(dict.fromkeys(str(h.get("pmid") or "") for h in against))
        claim = (CONTESTED if said[STATES] and against else SUPPORTED if said[STATES]
                 else AGAINST if said[DENIES] else SUGGESTED)
        basis = said[STATES] if claim in (SUPPORTED, CONTESTED) else said[DENIES] if claim == AGAINST else said[HEDGES]
        top = basis[0]
        counter = against[0] if against else {}
        phen = [str(h.get("phenotype") or "").strip() for h in basis if str(h.get("phenotype") or "").strip()]
        g = flagged.get(gene)
        flags = ([k[5:] for k, v in g.items() if k.startswith("flag_") and _yes(v)] if g is not None
                 else _sentence_flags(top))
        if said[DENIES_ELSEWHERE]:
            flags.append(AGAINST_ELSEWHERE_FLAG)
        conflicting = (str(g.get("conflicting_mechanism") or "") if g is not None
                       else "|".join(sorted(other.get(gene, set()))))
        pmid = str(top.get("pmid") or "")
        rows.append({
            "gene": gene, "claim": claim,
            "explicit": "yes" if _yes(top.get("explicit")) else "no",
            "negated": "yes" if _yes(top.get("negated")) else "no",
            "phenotype": max(set(phen), key=phen.count) if phen else "",
            "quote": str(top.get("quote") or ""), "pmid": pmid, "doi": paper(pmid).get("doi", ""),
            "title": paper(pmid).get("title", ""), "page": str(top.get("page") or ""),
            "papers_stating": len(pmids[STATES]), "papers_hedged": len(pmids[HEDGES]),
            "papers_against": len([p for p in pmids_against if p]),
            "pmids_stating": "|".join(p for p in pmids[STATES] if p),
            "pmids_against": "|".join(p for p in pmids_against if p),
            "quote_against": str(counter.get("quote") or ""), "pmid_against": str(counter.get("pmid") or ""),
            "page_against": str(counter.get("page") or ""),
            "against_context": ("" if not counter else "human retinal disease" if _yes(counter.get("human_retinal"))
                                else "animal model or another disease"),
            "conflicting_mechanism": conflicting, "review_flags": "|".join(flags),
            "gene_as_written": str(top.get("gene_as_written") or ""),
        })
        evidence[gene] = [{
            "gene": gene, "claim": claim, "statement": _statement(h),
            "explicit": "yes" if _yes(h.get("explicit")) else "no",
            "negated": "yes" if _yes(h.get("negated")) else "no",
            "phenotype": str(h.get("phenotype") or ""), "quote": str(h.get("quote") or ""),
            "pmid": str(h.get("pmid") or ""), "doi": paper(str(h.get("pmid") or "")).get("doi", ""),
            "title": paper(str(h.get("pmid") or "")).get("title", ""), "page": str(h.get("page") or ""),
            "sentence_id": str(h.get("sentence_id") or ""),
            "mechanism_statement": str(h.get("mechanism_statement") or ""),
            "gene_as_written": str(h.get("gene_as_written") or ""),
            "review_flags": "|".join(_sentence_flags(h)), "found_by": str(h.get("found_by") or ""),
        } for s in (STATES, HEDGES, DENIES, DENIES_ELSEWHERE) for h in said[s]]

    def strength(r: dict[str, Any]) -> tuple:
        made = r["papers_against"] if r["claim"] == AGAINST else (
            r["papers_hedged"] if r["claim"] == SUGGESTED else r["papers_stating"])
        return (CLAIMS.index(r["claim"]), -made, len([f for f in r["review_flags"].split("|") if f]), r["gene"])

    rows.sort(key=strength)
    return rows, [e for r in rows for e in evidence[r["gene"]]]


def write_curation(tables: Path, out: Path, papers: "dict[str, dict[str, str]] | None" = None,
                   ) -> dict[str, Any]:
    """Write the two curation tables into ``out`` and return ``{"rows", "n_sentences", "counts",
    "files"}`` (``counts``: genes per claim). Nothing is written when the scan found no sentence."""
    rows, evidence = curation_tables(tables, papers)
    counts = {c: sum(1 for r in rows if r["claim"] == c) for c in CLAIMS}
    if not rows:
        return {"rows": [], "n_sentences": 0, "counts": counts, "files": []}
    out.mkdir(parents=True, exist_ok=True)
    for name, data, columns in ((CURATION_GENES, rows, GENE_TABLE_COLUMNS),
                                (CURATION_EVIDENCE, evidence, EVIDENCE_COLUMNS)):
        with (out / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
            w.writeheader()
            w.writerows(data)
    return {"rows": rows, "n_sentences": len(evidence), "counts": counts,
            "files": [CURATION_GENES, CURATION_EVIDENCE]}


def curation_plan_step(question: str, when: str) -> str:
    """The one step of a curation plan, as the plan card shows it. ``when``: how long it takes on
    this session, and what it costs (the gateway's ``_curation_time_words``)."""
    q = " ".join(str(question or "").split())
    if len(q) > 300:
        q = q[:297].rstrip() + "…"
    return (f"**Corpus curation** — Read the whole RetiGene corpus for every gene its papers report as "
            f"haploinsufficient in retinal disease, and every gene they argue against, to answer: “{q}”. "
            "`extract_haploinsufficient_genes` has a model write search requests, reads every passage they "
            "retrieve and every passage that states a dosage mechanism in words (not only the best "
            "passages), and keeps each paper's own sentence; every quote is checked against its paper and "
            f"every gene symbol against HGNC. The result is two tables: `{CURATION_GENES}` (one row per gene: "
            "its claim, explicit or hedged, negated or not, the phenotype as written, the strongest sentence "
            f"verbatim, its paper and page) and `{CURATION_EVIDENCE}` (one row per sentence), summarised in "
            f"the chat. Only the RetiGene corpus is read; PubMed is not searched. Time: {when}.")
