"""Update the Eye Server's copy of the RetiGene corpus index, and rebuild the index on HPC3 first when
papers were added. One command, run on the Eye Server as the service account, from the running
release:

    sudo -u aiscientist /data/aiscientist/app/current/.venv/bin/python \\
        /data/aiscientist/app/current/scripts/update_corpus_index.py [--rebuild] [--check]

1. ``--rebuild``: one CPU Slurm job on HPC3, inside ``paperqa.sif``, extracts and embeds the papers
   added to the corpus since the last build (``build_mechanism_cache.py --replace``) and puts the new
   index in place, keeping the previous one as ``mechanism_cache.prev``. Nothing changes when no
   paper was added.
2. The copy (``gateway/corpus_index.sync``): only the files that changed, through RCIC's transfer
   node, into a new version under the copy's root that becomes current in one step.
3. The check: whether the copy is now the HPC3 index, the comparison the gateway makes before it
   searches the copy (``local_corpus.differences``).

``--check`` runs step 3 alone: exit status 0 when the copy is the HPC3 index, 1 when it is stale or
missing. ``--rollback`` makes the previous version of the copy current again.

Settings come from the ``.env`` beside ``scripts/``, in a release the link to ``config/.env``: the
corpus (``AISCIENTIST_PAPERQA_PAPERS``, ``_MANIFEST``, ``_IMAGE``), HPC3 (host, transfer host, CPU
partition and account, Apptainer module) and the copy's root (``AISCIENTIST_LOCAL_CORPUS_ROOT``,
default ``<data root>/reference/retigene``). The HPC3 login
is ``--hpc-user`` with ``--ssh-key``, by default the shared model server's (the saved key of
``AISCIENTIST_SHARED_LLM_OWNER``, or ``AISCIENTIST_SHARED_LLM_HPC_USER`` + ``_KEY_PATH``): a key
without a passphrase, since nobody is there to type one or answer Duo.
"""
from __future__ import annotations

import argparse
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from aiscientist.gateway import corpus_index, local_corpus  # noqa: E402

JOB_CPUS, JOB_MEM_GB, JOB_TIME = 8, 32, "04:00:00"


def _say(msg: str) -> None:
    print(msg, flush=True)


def corpus_settings(hpc_root: str = "") -> tuple[str, str, str, str]:
    """(root, papers, manifest, image) of the corpus on HPC3."""
    papers = os.environ.get("AISCIENTIST_PAPERQA_PAPERS", "").rstrip("/")
    root = (hpc_root or (os.path.dirname(papers) if papers else "")).rstrip("/")
    if not root:
        raise SystemExit("No corpus on HPC3: set AISCIENTIST_PAPERQA_PAPERS or pass --hpc-root.")
    papers = papers if papers and not hpc_root else f"{root}/papers"
    manifest = os.environ.get("AISCIENTIST_PAPERQA_MANIFEST", "") if not hpc_root else ""
    image = os.environ.get("AISCIENTIST_PAPERQA_IMAGE", "") or f"{root}/paperqa.sif"
    return root, papers, manifest or f"{root}/paperqa_manifest.csv", image


def identity(user: str, key: str, hpc: Any) -> tuple[str, str]:
    """(HPC3 user, private key path) the command logs in with."""
    if user and key:
        return user, key
    if user or key:
        raise SystemExit("--hpc-user and --ssh-key go together.")
    from aiscientist.gateway.shared_llm import SharedLLMSettings, resolve_identity
    s = SharedLLMSettings.from_env(hpc)
    if not (s.owner or s.key_path):
        raise SystemExit("No HPC3 login: pass --hpc-user and --ssh-key, or set the shared model "
                         "server's login (AISCIENTIST_SHARED_LLM_OWNER).")
    return resolve_identity(s, hpc)


def pack_code(out: Path) -> None:
    """The code the build job runs: the aiscientist package and the build script."""
    def skip(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if "__pycache__" in info.name or info.name.endswith(".pyc") else info

    with tarfile.open(out, "w:gz") as tar:
        tar.add(REPO / "src" / "aiscientist", arcname="src/aiscientist", filter=skip)
        tar.add(REPO / "scripts" / "build_mechanism_cache.py", arcname="scripts/build_mechanism_cache.py")


def rebuild(ex: Any, hpc: Any, user: str, root: str, papers: str, manifest: str, image: str) -> dict[str, Any]:
    """Step 1: the build job on HPC3; its summary (``build_mechanism_cache.py --replace``)."""
    from aiscientist.gateway.slurm_job import (AcquireConfig, RunConfig, SlurmJobSpec,
                                               build_analysis_script, run_batch_job, slurm_time_to_seconds)
    stage = f"{hpc.shared_root.rstrip('/')}/Temp/{user}/corpus_index/{time.strftime('%Y%m%d-%H%M%S')}"
    made = ex.exec(f"mkdir -p {stage}")
    if not made.ok:
        raise SystemExit(f"Could not create {stage} on HPC3: {made.stderr.strip()}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "code.tgz"
        pack_code(archive)
        ex.put_file(str(archive), f"{stage}/code.tgz")       # through the transfer node
    job = f"bioagent_corpus_index_{user}"
    script = build_analysis_script(
        job, corpus_index.rebuild_inner_command(stage=stage, archive=f"{stage}/code.tgz", image=image,
                                                root=root, papers=papers, manifest=manifest,
                                                container_bin=hpc.container_bin),
        partition=hpc.cpu_partition, account=hpc.cpu_account or "", cpus=JOB_CPUS, mem_gb=JOB_MEM_GB,
        time_limit=JOB_TIME, container_module=hpc.container_module, log_dir=stage)
    _say(f"Rebuilding the index on HPC3 ({root}): Slurm job {job}, log in {stage}")
    res = run_batch_job(ex, SlurmJobSpec(script=script, job_name=job, submit_dir=stage),
                        acquire=AcquireConfig(startup_timeout_s=1800, max_attempts=2, poll_interval_s=15),
                        run=RunConfig(run_timeout_s=slurm_time_to_seconds(JOB_TIME) + 300, poll_interval_s=30),
                        emit=lambda level, stage_, msg: _say(f"  {msg}"))
    log = f"{stage}/{job}-{res.job_id}.log"
    try:
        text = ex.read_bytes(log).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - reported below as a failed build
        text = ""
    summary = corpus_index.parse_rebuild_log(text)
    if not res.completed or summary is None:
        raise SystemExit(f"The rebuild failed (Slurm {res.state}, job {res.job_id}); nothing was copied. "
                         f"Log {log}:\n{text[-3000:]}")
    _say(f"  {'index updated' if summary.get('changed') else 'no new papers; index unchanged'}: "
         f"{summary.get('papers')} papers, {summary.get('chunks')} chunks ({summary.get('new_chunks')} new)")
    return summary


def check(ex: Any, dest: Path, papers: str, manifest: str) -> int:
    """Step 3: 0 when the copy is the HPC3 index."""
    from aiscientist.gateway.corpus_scan import corpus_fingerprint
    fp = corpus_fingerprint(ex, papers, manifest)
    if fp is None:
        _say(f"The HPC3 index next to {papers} could not be read.")
        return 1
    index = local_corpus.index_dir(dest)
    if not (index / "mechanism_cache" / "chunks.jsonl").is_file():
        _say(f"No copy at {dest}.")
        return 1
    diff = local_corpus.differences(fp, index)
    if diff:
        _say(f"The copy {index} is stale: {', '.join(diff)} differ from HPC3. Searches run on HPC3 "
             "until it is updated.")
        return 1
    _say(f"The copy {index} is the HPC3 index: searches use it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rebuild", action="store_true", help="rebuild the index on HPC3 first (new papers)")
    ap.add_argument("--check", action="store_true", help="only say whether the copy is the HPC3 index")
    ap.add_argument("--rollback", action="store_true", help="make the previous version of the copy current")
    ap.add_argument("--with-model", action="store_true",
                    help="copy the embedding model again (it is copied anyway when the copy has none)")
    ap.add_argument("--hpc-user", default="")
    ap.add_argument("--ssh-key", default="", help="private key without a passphrase")
    ap.add_argument("--hpc-root", default="", help="the corpus root on HPC3 (default: the papers' parent)")
    ap.add_argument("--destination", type=Path, default=None, help="the copy's root on this server")
    args = ap.parse_args(argv)

    from aiscientist.core.config import load_project_env
    load_project_env(REPO)
    dest = args.destination or local_corpus.local_root()
    if args.rollback:
        _say(f"Current version: {corpus_index.rollback(dest).name}")
        return 0

    from aiscientist.gateway.settings import HPCSettings
    from aiscientist.gateway.ssh_gateway import SSHExecutor
    hpc = HPCSettings.from_env()
    root, papers, manifest, image = corpus_settings(args.hpc_root)
    user, key = identity(args.hpc_user, args.ssh_key, hpc)
    ex = SSHExecutor(host=hpc.host, username=user, port=hpc.ssh_port, key_path=key,
                     transfer_host=hpc.transfer_host)
    try:
        if args.check:
            return check(ex, dest, papers, manifest)
        if args.rebuild:
            rebuild(ex, hpc, user, root, papers, manifest, image)
        transfer = hpc.transfer_host or corpus_index.TRANSFER_HOST
        t0 = time.monotonic()
        res = corpus_index.sync(f"{user}@{transfer}:{root}", dest, manifest=os.path.basename(manifest),
                                with_hgnc=True, with_model=args.with_model or not (dest / "hf_cache").is_dir(),
                                ssh=corpus_index.ssh_command(key=key))
        took = time.monotonic() - t0
        if res.changed:
            _say(f"Copied in {took:.0f} s: version {res.version.name} is current "
                 f"({res.papers} papers, {res.chunks} chunks; changed: {', '.join(res.files) or 'all'}).")
        else:
            _say(f"The copy was already up to date ({took:.0f} s): version {res.version.name}.")
        return check(ex, dest, papers, manifest)
    except corpus_index.SyncError as exc:
        _say(f"Not copied: {exc}. The current version is unchanged.")
        return 1
    finally:
        ex.close()


if __name__ == "__main__":
    raise SystemExit(main())
