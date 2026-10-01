#!/usr/bin/env python3
"""Regenerate ``docs/ENVIRONMENT.md`` — what this deployment has, and where it is.

The same manifest the ``describe_environment`` tool serves the agent, rendered for a person. One
source for both audiences on purpose: a hand-maintained copy would be wrong within a month, and
then the version the agent reads and the version the team reads would disagree — which is worse
than having neither.

    python scripts/write_environment_doc.py            # -> docs/ENVIRONMENT.md
    python scripts/write_environment_doc.py --stdout   # print instead

Paths for assets that live on HPC3 come from the live ``HPCSettings``; this host cannot stat them,
so they are reported without an existence claim rather than guessed at.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    ap.add_argument("-o", "--out", default=str(REPO / "docs" / "ENVIRONMENT.md"))
    args = ap.parse_args()

    from aiscientist.gateway.environment import environment_manifest, render_markdown

    md = render_markdown(environment_manifest())
    if args.stdout:
        print(md)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"wrote {out.relative_to(REPO) if out.is_relative_to(REPO) else out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
