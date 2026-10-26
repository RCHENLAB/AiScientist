"""Generate the browser upload acceptance fixtures without keeping them in Git.

Examples:
  python scripts/generate_upload_test_data.py single --out D:/upload-fixtures --gb 25 --execute
  python scripts/generate_upload_test_data.py folder --out D:/upload-fixtures --gb 25 --files 5000 --execute
  python scripts/generate_upload_test_data.py archive --out D:/upload-fixtures --gb 20 --execute
Use --gb 0.01 for a quick local rehearsal. Omit --execute to print the disk estimate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import zipfile
from pathlib import Path

BLOCK = 4 * 1024 * 1024


def write_random(path: Path, size: int) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open("wb") as stream:
        while stream.tell() < size:
            block = os.urandom(min(BLOCK, size - stream.tell()))
            stream.write(block)
            digest.update(block)
    return {"path": str(path), "size": size, "sha256": digest.hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("single", "folder", "archive"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gb", type=float, default=25)
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    total = int(args.gb * 1024**3)
    if total <= 0 or args.files <= 0:
        parser.error("--gb and --files must be positive")
    if not args.execute:
        print(json.dumps({"kind": args.kind, "bytes_to_generate": total,
                          "estimated_free_space_required": total * (2 if args.kind == "archive" else 1),
                          "out": str(args.out), "dry_run": True}))
        return
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = []
    if args.kind == "single":
        manifest.append(write_random(args.out / "single_large.bin", total))
    elif args.kind == "folder":
        root = args.out / "folder_large"
        large = min(total, 10 * 1024**3)
        manifest.append(write_random(root / "large.bin", large))
        rest = total - large
        for index in range(args.files):
            size = rest // args.files + (1 if index < rest % args.files else 0)
            manifest.append(write_random(root / f"batch_{index // 100:03d}" / f"sample_{index:05d}.bin", size))
    else:
        source = args.out / "archive_payload.bin"
        manifest.append(write_random(source, total))
        with zipfile.ZipFile(args.out / "archive_large.zip", "w", allowZip64=True) as archive:
            archive.write(source, arcname=source.name, compress_type=zipfile.ZIP_STORED)
    (args.out / f"{args.kind}_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"kind": args.kind, "files": len(manifest), "manifest": str(args.out / f"{args.kind}_manifest.json")}))


if __name__ == "__main__":
    main()
