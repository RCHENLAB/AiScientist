#!/usr/bin/env python3
"""Build the public snapshot for RCHENLAB/AiScientist, check it for credentials, and sync it into a
local clone of that repository.

What is published (decided 2026-09-30): everything in this repository except credentials, meaning
database accounts and passwords, API keys, tokens and private keys. Hostnames, paths, account
names, handoffs, experiment records, reports and decks are all published. Credentials never belong
in git at all; they live in the deployment's ``.env`` and ``AISCIENTIST_STATE_DIR``. The scan below
normally finds nothing; it exists so that a pasted key cannot ride along unnoticed. A path that has
to stay private goes in ``.publicexclude`` and is dropped from the snapshot.

The mirror is a snapshot, not a git remote of this repository: each publish is one commit on the
mirror holding the tree of one commit here, so this repository's history is never published.

    python scripts/publish_public_mirror.py --scan-only                  # check HEAD, publish nothing
    python scripts/publish_public_mirror.py --mirror ../AiScientist      # sync HEAD into a clone
    python scripts/publish_public_mirror.py --mirror ../AiScientist --ref main

It never commits or pushes; it prints the commands for that. Exit status 1 when the scan finds
something: remove the credential from the file (and rotate it, since it is in this repository's
history), or list the path in ``.publicexclude``.
"""

from __future__ import annotations

import argparse
import gzip
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# High-precision credential formats: a match is a credential unless it is a documented example.
PATTERNS = {
    "private key": r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    "Anthropic API key": r"sk-ant-[A-Za-z0-9]{3,8}-[A-Za-z0-9_-]{20,}",
    "OpenRouter API key": r"sk-or-v1-[0-9a-f]{32,}",
    "OpenAI project key": r"sk-proj-[A-Za-z0-9_-]{20,}",
    "sk- API key": r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9]{32,}",
    "AWS access key": r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])",
    "GitHub token": r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "Slack token": r"xox[abprs]-[A-Za-z0-9-]{10,}",
    "Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "Hugging Face token": r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}",
    "JWT": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "password in a URL": r"[a-z][a-z0-9+.-]{1,30}://[^\s/:@'\"<>()]+:(?P<secret>[^\s/@'\"<>()]+)@",
}
DOCUMENTED_EXAMPLES = {"AKIAIOSFODNN7EXAMPLE"}  # AWS's own documentation key, used in a test

# NAME=value / NAME: value where NAME says it holds a secret. Checked outside tests/ (test fixtures
# are fake by construction) and only for values that are not placeholders. Names that merely
# describe a secret (a regex, a file holding it, the variable it is read from) and sudoers'
# NOPASSWD are not secrets; neither is a value that is code (a call).
ASSIGNMENT = re.compile(
    r"\b(?P<name>[A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY)"
    r"[A-Z0-9_]*)\s*[=:]\s*[\"']?(?P<secret>[^\s\"'`,;)}\]]+)")
NOT_A_SECRET_NAME = re.compile(r"^NOPASSWD$|_(?:PATTERN|RE|REGEX|FILE|PATH|DIR|NAME|ENV|VAR)$")
PLACEHOLDER = re.compile(
    r"(?i)^$|^[<$%{\[(]|\(|…|\.\.\.|^x{3,}|\*{3,}|changeme|change-me|example|your|dummy|placeholder"
    r"|redacted|local-dev|^none$|^null$|^true$|^false$|^os\.|^env|^sk-or-$")
MIN_SECRET_LEN = 8

TEXT_LIMIT = 50 * 1024 * 1024
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".h5ad"}


def snapshot(ref: str, dest: Path) -> None:
    """Extract the tree of ``ref`` into ``dest`` and drop the paths ``.publicexclude`` lists."""
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", ref], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python without extraction filters; the archive is our own commit
            tar.extractall(dest)
    exclude = dest / ".publicexclude"
    if not exclude.exists():
        return
    for line in exclude.read_text(encoding="utf-8").splitlines():
        rel = line.strip()
        if not rel or rel.startswith("#"):
            continue
        target = (dest / rel).resolve()
        if dest.resolve() not in target.parents:
            sys.exit(f".publicexclude: {rel!r} points outside the repository")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            print(f"note: .publicexclude lists {rel!r}, which is not in {ref}")


def text_of(path: Path) -> str | None:
    """The searchable text of a file: OOXML parts unzipped, PDFs through pdftotext, .gz inflated."""
    suffix = path.suffix.lower()
    if suffix in SKIP_SUFFIXES or path.stat().st_size > TEXT_LIMIT:
        return None
    if suffix in {".docx", ".pptx", ".xlsx"}:
        with zipfile.ZipFile(path) as z:
            return "\n".join(re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "ignore"))
                             for n in z.namelist() if n.endswith((".xml", ".rels")))
    if suffix == ".pdf":
        if not shutil.which("pdftotext"):
            sys.exit(f"pdftotext is needed to check {path.name} (brew install poppler)")
        return subprocess.run(["pdftotext", "-q", str(path), "-"], capture_output=True, text=True).stdout
    raw = path.read_bytes()
    if suffix == ".gz":
        raw = gzip.decompress(raw)
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8", "ignore")


def scan(tree: Path) -> list[str]:
    """Every credential-looking string in ``tree``, as ``path:line: kind``."""
    compiled = {kind: re.compile(rx) for kind, rx in PATTERNS.items()}
    findings = []
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        rel = path.relative_to(tree).as_posix()
        text = text_of(path)
        if text is None:
            continue
        in_tests = rel.startswith("tests/")
        for number, line in enumerate(text.splitlines(), 1):
            for kind, rx in compiled.items():
                for m in rx.finditer(line):
                    secret = m.groupdict().get("secret") or m.group(0)
                    if m.group(0) in DOCUMENTED_EXAMPLES or PLACEHOLDER.search(secret):
                        continue
                    findings.append(f"{rel}:{number}: {kind}")
            if in_tests:
                continue
            for m in ASSIGNMENT.finditer(line):
                secret = m.group("secret")
                if NOT_A_SECRET_NAME.search(m.group("name")):
                    continue
                if len(secret) >= MIN_SECRET_LEN and not PLACEHOLDER.search(secret):
                    findings.append(f"{rel}:{number}: value assigned to {m.group('name')}")
    return findings


def sync(tree: Path, mirror: Path) -> None:
    """Make the clone's working tree equal to ``tree``, leaving its .git alone."""
    for child in mirror.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in tree.iterdir():
        if child.is_dir():
            shutil.copytree(child, mirror / child.name, symlinks=True)
        else:
            shutil.copy2(child, mirror / child.name)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="HEAD", help="commit to publish (default: HEAD)")
    ap.add_argument("--mirror", type=Path, help="a local clone of RCHENLAB/AiScientist to sync into")
    ap.add_argument("--scan-only", action="store_true", help="build and check the snapshot only")
    args = ap.parse_args()
    if not args.scan_only and not args.mirror:
        ap.error("give --mirror DIR (a clone of RCHENLAB/AiScientist) or --scan-only")
    if args.mirror and not (args.mirror / ".git").is_dir():
        ap.error(f"{args.mirror} is not a git clone")

    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", args.ref],
                         check=True, capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "snapshot"
        tree.mkdir()
        snapshot(args.ref, tree)
        findings = scan(tree)
        files = sum(1 for p in tree.rglob("*") if p.is_file())
        if findings:
            print(f"{len(findings)} possible credential(s) in the snapshot of {sha}; nothing was published:")
            print("\n".join(f"  {f}" for f in findings))
            return 1
        print(f"snapshot of {sha}: {files} files, no credentials found")
        if args.scan_only:
            return 0
        sync(tree, args.mirror)
    print(f"synced into {args.mirror}. Review, then publish:\n"
          f"  git -C {args.mirror} add -A\n"
          f"  git -C {args.mirror} commit -m 'Sync from development ({sha})'\n"
          f"  git -C {args.mirror} push origin main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
