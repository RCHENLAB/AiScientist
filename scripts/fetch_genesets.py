#!/usr/bin/env python3
"""Download gene-set libraries (GMT) for OFFLINE enrichment (run_enrichment).

The analysis line runs pathway enrichment inside a network-OFF Slurm/Singularity container,
so it CANNOT call the Enrichr web API. Instead it reads local ``.gmt`` files. Run this ONCE
on a machine WITH internet (the eye server, or your laptop) and point it at the gene-set dir
the container will read.

Where to put the files
----------------------
The tool resolves the gene-set dir as:  ``$BIOAGENT_GENESETS_DIR``  else
``<...>/bioagent/tools/genesets/`` (next to the module — rides along with the dfs3b source
bind, so the container sees it with no extra mount).

So either:
  * write into the dfs3b source tree the Slurm jobs bind, e.g.
        python scripts/fetch_genesets.py /dfs3b/ruic20_lab/software/bioagent/app/src/bioagent/tools/genesets
    (the container already binds that source read-only + puts it on PYTHONPATH), OR
  * write anywhere and set BIOAGENT_GENESETS_DIR to it (must be bind-mounted into the job).

Usage
-----
    python scripts/fetch_genesets.py [DEST_DIR] [--libs A B C]

Defaults to the module-adjacent genesets dir and the three freely-redistributable libraries
used by run_enrichment. KEGG is NOT downloaded by default (its GMT redistribution is
license-restricted) — pass it explicitly if you've cleared that for your use.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

# Enrichr serves every library as GMT text at this endpoint (public, no key).
_ENRICHR_GMT = "https://maayanlab.cloud/Enrichr/geneSetLibrary?mode=text&libraryName={name}"

_DEFAULT_LIBS = ("GO_Biological_Process_2023", "Reactome_2022", "MSigDB_Hallmark_2020")


def _default_dest() -> Path:
    # Mirror scrna_pack._genesets_dir()'s module-adjacent default.
    return Path(__file__).resolve().parent.parent / "src" / "bioagent" / "tools" / "genesets"


def fetch(name: str, dest: Path, *, timeout: float = 60.0) -> int:
    url = _ENRICHR_GMT.format(name=name)
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed trusted host
        data = r.read()
    if not data or b"\t" not in data:
        raise RuntimeError(f"{name}: empty/invalid GMT (is the library name exact?)")
    out = dest / f"{name}.gmt"
    out.write_bytes(data)
    return data.count(b"\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download GMT gene-set libraries for offline enrichment.")
    ap.add_argument("dest", nargs="?", default=None, help="destination dir (default: module genesets/)")
    ap.add_argument("--libs", nargs="+", default=list(_DEFAULT_LIBS), help="Enrichr library names")
    args = ap.parse_args(argv)

    dest = Path(args.dest).expanduser().resolve() if args.dest else _default_dest()
    dest.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {len(args.libs)} librar(ies) into {dest}")
    failed = []
    for name in args.libs:
        try:
            n = fetch(name, dest)
            print(f"  ✓ {name}.gmt  ({n} term sets)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {name}: {exc}", file=sys.stderr)
            failed.append(name)
    if failed:
        print(f"\n{len(failed)} failed: {failed}", file=sys.stderr)
        return 1
    print("\nDone. run_enrichment will now find these offline (no network needed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
