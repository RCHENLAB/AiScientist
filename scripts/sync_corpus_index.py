"""Incrementally copy a corpus index and manifest from HPC3 to the Eye Server using rsync."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.gateway.local_corpus import local_root

# RCIC allows no data transfer on the HPC3 login nodes: copies go through its transfer node.
TRANSFER_HOST = "access-hpc3.rcic.uci.edu"
_LOGIN_HOST = re.compile(r"(?:hpc3|login-i\d+)(?:\.rcic\.uci\.edu)?", re.IGNORECASE)


def _source_host(source: str) -> str:
    """The host of an rsync source ``[user@]host:path``; empty for a local path."""
    head, sep, _ = source.partition(":")
    return head.rsplit("@", 1)[-1] if sep and "/" not in head else ""


def sync(source: str, destination: Path, *, manifest: str = "paperqa_manifest.csv",
         with_model: bool = False, with_hgnc: bool = False) -> None:
    """source is an rsync source directory, e.g. access-hpc3.rcic.uci.edu:/dfs3b/.../retigene."""
    if source.startswith("-") or Path(manifest).name != manifest:
        raise ValueError("source must be a directory and manifest must be a filename")
    if _LOGIN_HOST.fullmatch(_source_host(source)):
        raise ValueError(f"{_source_host(source)} is an HPC3 login node, where RCIC allows no data "
                         f"transfer: copy through {TRANSFER_HOST} instead")
    destination.mkdir(parents=True, exist_ok=True)
    source = source.rstrip("/")
    pairs = [("mechanism_cache/", destination / "mechanism_cache"),
             (manifest, destination / "manifest.csv")]
    if with_model:
        pairs.append(("hf_cache/", destination / "hf_cache"))
    if with_hgnc:
        pairs.append(("hgnc_complete_set.txt", destination / "hgnc_complete_set.txt"))
    for name, target in pairs:
        if name.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        # Checksums detect changed contents even when timestamps/sizes match; unchanged files
        # are not transferred. Delay replacement until each transfer completes.
        subprocess.run(["rsync", "-rltc", "--delay-updates", "--protect-args", "--",
                        f"{source}/{name}", str(target)], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True,
                        help=f"[user@]host:path of the corpus parent directory, through {TRANSFER_HOST}")
    parser.add_argument("--destination", type=Path, default=None)
    parser.add_argument("--manifest", default="paperqa_manifest.csv")
    parser.add_argument("--with-model", action="store_true", help="also sync the offline hf_cache")
    parser.add_argument("--with-hgnc", action="store_true", help="also sync hgnc_complete_set.txt")
    args = parser.parse_args(argv)
    sync(args.source, args.destination or local_root(), manifest=args.manifest,
         with_model=args.with_model, with_hgnc=args.with_hgnc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
