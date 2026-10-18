#!/usr/bin/env python3
"""Validate croissant.json files the way AiScientist does (docs/development/metadata.md).

    python scripts/check_croissant.py path/to/croissant.json [more.json ...]

Runs AiScientist's own rules (required properties, a checksum on every FileObject, a dataType and a
resolvable source on every Field, no server path or credential anywhere) and, when it is installed,
mlcroissant, the reference validator (``pip install -e ".[metadata]"``). Exits 1 when any file fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiscientist.metadata import validate  # noqa: E402


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    failed = 0
    for name in paths:
        try:
            doc = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"FAIL {name}: not readable JSON ({exc})")
            failed += 1
            continue
        report = validate(doc)
        print(f"{'ok  ' if report['ok'] else 'FAIL'} {name} ({report['validator']})")
        for line in report["errors"]:
            print(f"     error: {line}")
        for line in report["warnings"]:
            print(f"     warning: {line}")
        failed += 0 if report["ok"] else 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
