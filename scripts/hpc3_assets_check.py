#!/usr/bin/env python3
"""Compare configs/hpc3_assets.yaml with the files on HPC3, and print the values to pin.

Standard library only, so it runs on HPC3 without the repository (there is no copy of it there):

    python3 -c 'import json, yaml, sys; json.dump(yaml.safe_load(open("configs/hpc3_assets.yaml")), sys.stdout)' > /tmp/hpc3_assets.json
    scp scripts/hpc3_assets_check.py /tmp/hpc3_assets.json hpc3:/tmp/
    ssh hpc3 python3 /tmp/hpc3_assets_check.py /tmp/hpc3_assets.json            # revisions, weight files
    ssh hpc3 srun -p standard -c 2 --mem 4G -t 1:00:00 \\
        python3 /tmp/hpc3_assets_check.py /tmp/hpc3_assets.json --hash             # + SHA-256 of images

A model in the Hugging Face cache costs no hashing: each weight file in ``snapshots/<revision>/`` links
to a blob named by its SHA-256; one downloaded with ``--local-dir`` has its revision and each file's
SHA-256 in ``.cache/huggingface/download/``. ``--hash`` reads the images (gigabytes): run it on a compute node,
never on a login node. Exit 1 when something on the list is missing or differs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SHARED_ROOT = os.environ.get("AISCIENTIST_HPC_SHARED_ROOT", "/dfs3b/ruic20_lab/software/AiScientist")
WEIGHTS = (".safetensors", ".bin", ".pt", ".pth", ".gguf")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_image(root: Path, name: str, entry: dict, hash_it: bool) -> tuple[list[str], dict]:
    """Problems with one image, and the values found (``sha256`` with ``hash_it``)."""
    path = root / entry["path"]
    if not path.is_file():
        return [f"image {name}: {path} is missing"], {}
    found = {"size": path.stat().st_size}
    problems = []
    if hash_it:
        found["sha256"] = sha256_file(path)
        if entry.get("sha256") and entry["sha256"] != found["sha256"]:
            problems.append(f"image {name}: {path} has SHA-256 {found['sha256']}, the list pins {entry['sha256']}")
    return problems, found


def hf_snapshot(model_dir: Path, revision: str | None) -> tuple[str | None, Path | None]:
    """The revision a Hugging Face cache folder serves (``refs/main``), and its snapshot folder."""
    rev = revision
    if not rev:
        try:
            rev = (model_dir / "refs" / "main").read_text().strip()
        except OSError:
            snaps = sorted(p.name for p in (model_dir / "snapshots").glob("*"))
            rev = snaps[-1] if len(snaps) == 1 else None
    snap = model_dir / "snapshots" / rev if rev else None
    return rev, snap if snap is not None and snap.is_dir() else None


def weight_files(folder: Path, hash_it: bool) -> dict[str, str]:
    """Weight file name -> SHA-256: the blob name it links to in an HF cache, else (``hash_it``) its hash."""
    out = {}
    for f in sorted(folder.rglob("*")):
        if not f.name.endswith(WEIGHTS) or not f.is_file():
            continue
        rel = f.relative_to(folder).as_posix()
        blob = os.path.basename(os.readlink(f)) if f.is_symlink() else ""
        if re.fullmatch(r"[0-9a-f]{64}", blob):
            out[rel] = blob
        elif hash_it:
            out[rel] = sha256_file(f)
    return out


def local_dir_metadata(folder: Path) -> tuple[str | None, dict[str, str]]:
    """What ``hf download --local-dir`` recorded in ``.cache/huggingface/download/<file>.metadata``
    (commit, etag, time): the revision, and the SHA-256 of each weight file (an LFS file's etag)."""
    rev, files = None, {}
    meta = folder / ".cache" / "huggingface" / "download"
    for m in sorted(meta.rglob("*.metadata")) if meta.is_dir() else []:
        lines = m.read_text().split("\n")
        name = m.relative_to(meta).as_posix()[: -len(".metadata")]
        if len(lines) >= 2 and re.fullmatch(r"[0-9a-f]{40}", lines[0].strip()):
            rev = rev or lines[0].strip()
            if name.endswith(WEIGHTS) and re.fullmatch(r"[0-9a-f]{64}", lines[1].strip()):
                files[name] = lines[1].strip()
    return rev, files


def check_model(root: Path, repo: str, entry: dict, hash_it: bool) -> tuple[list[str], dict]:
    folder = root / entry.get("path", "")
    if not folder.is_dir():
        return [f"model {repo}: {folder} is missing"], {}
    problems: list[str] = []
    if (folder / "snapshots").is_dir():
        rev, snap = hf_snapshot(folder, entry.get("revision"))
        if snap is None:
            return [f"model {repo}: no snapshot {rev or '(refs/main unreadable)'} in {folder}"], {}
        found = {"revision": rev, "files": weight_files(snap, hash_it)}
    else:          # `hf download --local-dir`: the revision and each file's SHA-256 are in its metadata
        rev, recorded = local_dir_metadata(folder)
        files = weight_files(folder, hash_it)
        for name, digest in recorded.items():
            if name in files and files[name] != digest:
                problems.append(f"model {repo}: {name} has SHA-256 {files[name]}, its download recorded {digest}")
            files.setdefault(name, digest)
        found = {"revision": rev or entry.get("revision"), "files": files}
    pinned = entry.get("files") or {}
    for name, digest in pinned.items():
        if name not in found["files"]:
            if hash_it or (folder / "snapshots").is_dir():
                problems.append(f"model {repo}: {name} is missing")
        elif found["files"][name] != digest:
            problems.append(f"model {repo}: {name} has SHA-256 {found['files'][name]}, the list pins {digest}")
    for name in found["files"]:
        if pinned and name not in pinned:
            problems.append(f"model {repo}: {name} is not on the list")
    return problems, found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("list", help="configs/hpc3_assets.yaml as JSON (or YAML, when PyYAML is installed)")
    ap.add_argument("--root", default=SHARED_ROOT, help=f"the shared root (default {SHARED_ROOT})")
    ap.add_argument("--hash", action="store_true", help="hash the images and copied weights (compute node)")
    args = ap.parse_args(argv)
    text = Path(args.list).read_text()
    try:
        doc = json.loads(text)
    except ValueError:
        import yaml  # noqa: PLC0415 - only when given YAML
        doc = yaml.safe_load(text)
    root = Path(args.root)
    problems: list[str] = []
    found: dict = {"images": {}, "models": {}}
    for name, entry in (doc.get("images") or {}).items():
        p, f = check_image(root, name, entry, args.hash)
        problems += p
        found["images"][name] = f
    for repo, entry in (doc.get("models") or {}).items():
        p, f = check_model(root, repo, entry, args.hash)
        problems += p
        found["models"][repo] = f
    json.dump(found, sys.stdout, indent=1, sort_keys=True)
    print()
    for line in problems:
        print("DIFFERS: " + line, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
