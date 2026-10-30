"""Copy the corpus index and manifest from HPC3 to the Eye Server: only the files that changed, into a
new version that becomes current in one step (``gateway/corpus_index.py`` describes the layout).
``scripts/update_corpus_index.py`` runs this copy with the deployment's settings, and can rebuild
the index on HPC3 first; use this script for a copy from another source or into another folder:

    python scripts/sync_corpus_index.py \\
        --source <user>@access-hpc3.rcic.uci.edu:/dfs3b/ruic20_lab/software/AiScientist/retigene \\
        --with-model --with-hgnc [--ssh-key <key>] [--destination <dir>]
    python scripts/sync_corpus_index.py --rollback [--destination <dir>]

The copy goes through RCIC's transfer node: the script refuses an HPC3 login node as the source.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.gateway import corpus_index  # noqa: E402
from aiscientist.gateway.local_corpus import local_root  # noqa: E402

TRANSFER_HOST = corpus_index.TRANSFER_HOST


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", help=f"[user@]host:path of the corpus root, through {TRANSFER_HOST}")
    parser.add_argument("--destination", type=Path, default=None, help="the copy's root (default: the gateway's)")
    parser.add_argument("--manifest", default="paperqa_manifest.csv", help="the manifest's file name at the source")
    parser.add_argument("--with-model", action="store_true", help="also copy the offline hf_cache")
    parser.add_argument("--with-hgnc", action="store_true", help="also copy hgnc_complete_set.txt")
    parser.add_argument("--ssh-key", default="", help="private key for the transfer node")
    parser.add_argument("--rollback", action="store_true", help="make the previous version current")
    args = parser.parse_args(argv)
    dest = args.destination or local_root()
    try:
        if args.rollback:
            print(f"current: {corpus_index.rollback(dest)}")
            return 0
        if not args.source:
            parser.error("--source is required")
        res = corpus_index.sync(args.source, dest, manifest=args.manifest, with_model=args.with_model,
                                with_hgnc=args.with_hgnc,
                                ssh=corpus_index.ssh_command(key=args.ssh_key) if args.ssh_key else None)
    except corpus_index.SyncError as exc:
        print(f"not copied: {exc}", file=sys.stderr)
        return 1
    state = "copied" if res.changed else "unchanged"
    print(f"{state}: {res.version} ({res.papers} papers, {res.chunks} chunks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
