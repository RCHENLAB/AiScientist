from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


SECRET_TOKEN_PATTERNS = (
    re.compile(r"sk-or-[A-Za-z0-9_-]{12,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
)
ENV_API_KEY_PATTERN = re.compile(
    r"(OPENROUTER|OPENAI|ANTHROPIC|GEMINI|GOOGLE|BIOAGENT_LLM)_API_KEY\s*=\s*([^#\s]+)"
)
GENERIC_API_KEY_PATTERN = re.compile(r"api[_-]?key\s*[:=]\s*['\"]([^'\"]{12,})['\"]", re.IGNORECASE)

TEXT_SUFFIXES = {
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

BLOCKED_ENV_NAMES = {".env"}
ALLOWED_ENV_NAMES = {".env.example", ".env.template", ".env.sample"}
MAX_REPO_FILE_BYTES = 25 * 1024 * 1024
MAX_EXAMPLE_DATASET_BYTES = 500 * 1024 * 1024
SKIP_DIRS = {".git", ".venv", ".pytest_cache", "__pycache__", "runs"}


@dataclass
class Finding:
    level: str
    path: str
    message: str


def run_git(args: list[str]) -> str:
    proc = subprocess.run(["git", *args], check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def changed_files(base: str | None, head: str | None) -> list[Path]:
    if base and head:
        diff = run_git(["diff", "--name-only", f"{base}...{head}"])
        if diff:
            return [Path(line) for line in diff.splitlines() if line.strip()]
    tracked = run_git(["ls-files"])
    if tracked:
        return [Path(line) for line in tracked.splitlines() if line.strip()]
    return [
        path
        for path in Path(".").rglob("*")
        if path.is_file()
        and path.name not in BLOCKED_ENV_NAMES
        and not any(part in SKIP_DIRS for part in path.parts)
    ]


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    return path.name in {"Dockerfile", "Makefile"}


def is_placeholder_secret(value: str) -> bool:
    cleaned = value.strip().strip(",'\"")
    lowered = cleaned.lower()
    if lowered in {"", "...", "your-key", "your_api_key", "local-dev-key", "placeholder", "dummy"}:
        return True
    if "placeholder" in lowered or "example" in lowered or "local-dev" in lowered:
        return True
    return bool(cleaned) and len(set(cleaned)) <= 2


def contains_secret_like(text: str) -> bool:
    if any(pattern.search(text) for pattern in SECRET_TOKEN_PATTERNS):
        return True
    for match in ENV_API_KEY_PATTERN.finditer(text):
        if not is_placeholder_secret(match.group(2)):
            return True
    for match in GENERIC_API_KEY_PATTERN.finditer(text):
        if not is_placeholder_secret(match.group(1)):
            return True
    return False


def scan_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not path.exists() or not path.is_file():
        return findings

    name = path.name
    if name in BLOCKED_ENV_NAMES or (name.startswith(".env.") and name not in ALLOWED_ENV_NAMES):
        findings.append(
            Finding(
                "error",
                str(path),
                "Environment files must not be committed. Use .env.example for placeholders.",
            )
        )

    size = path.stat().st_size
    if size > MAX_REPO_FILE_BYTES and "examples" not in path.parts:
        findings.append(
            Finding(
                "error",
                str(path),
                f"File is {size} bytes; keep large generated artifacts and datasets out of git.",
            )
        )
    if "examples" in path.parts and "datasets" in path.parts and size > MAX_EXAMPLE_DATASET_BYTES:
        findings.append(
            Finding(
                "error",
                str(path),
                "Example dataset exceeds the 500MB collaboration limit.",
            )
        )

    if is_text_file(path):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return findings
        if contains_secret_like(text):
            findings.append(
                Finding(
                    "error",
                    str(path),
                    "Secret-like token or API key assignment detected.",
                )
            )
        if "raw_expression_matrix" in text and "raw_data_to_llm" in text and "false" not in text.lower():
            findings.append(
                Finding(
                    "warning",
                    str(path),
                    "Raw-expression and LLM wording appear together; verify no raw dataset rows can leave the lab boundary.",
                )
            )
    return findings


def required_files() -> list[Finding]:
    findings: list[Finding] = []
    for path in (
        Path("README.md"),
        Path("README.zh-CN.md"),
        Path("handoff/yijun/HANDOFF.md"),
        Path("handoff/yijun/HANDOFF.zh-CN.md"),
        Path("docs/archive/kosmos_kernel_guardrails.md"),
        Path("docs/archive/project_plan.md"),
        Path(".gitignore"),
    ):
        if not path.exists():
            findings.append(Finding("error", str(path), "Required collaboration document is missing."))
    gitignore = Path(".gitignore")
    if gitignore.exists() and ".env" not in gitignore.read_text(encoding="utf-8").splitlines():
        findings.append(Finding("error", ".gitignore", ".env must remain ignored."))
    return findings


def markdown_summary(findings: list[Finding], files: list[Path]) -> str:
    errors = [finding for finding in findings if finding.level == "error"]
    warnings = [finding for finding in findings if finding.level == "warning"]
    lines = [
        "# AiScientist PR Review Gate",
        "",
        f"- Changed files scanned: `{len(files)}`",
        f"- Errors: `{len(errors)}`",
        f"- Warnings: `{len(warnings)}`",
        "",
    ]
    if findings:
        lines.extend(["| Level | Path | Message |", "| --- | --- | --- |"])
        for finding in findings:
            lines.append(f"| {finding.level} | `{finding.path}` | {finding.message} |")
    else:
        lines.append("No policy findings detected.")
    lines.extend(
        [
            "",
            "Policy reminders:",
            "- Do not commit `.env`, API keys, private datasets, or generated run artifacts.",
            "- Literature/network tools must use sanitized public queries only.",
            "- Slurm submit remains review-gated until production approval.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="AiScientist pull request review gate.")
    parser.add_argument("--base", default=os.environ.get("PR_BASE_SHA"))
    parser.add_argument("--head", default=os.environ.get("PR_HEAD_SHA"))
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args()

    files = changed_files(args.base, args.head)
    findings = required_files()
    for path in files:
        findings.extend(scan_file(path))

    summary = markdown_summary(findings, files)
    if args.summary:
        Path(args.summary).write_text(summary, encoding="utf-8")
    print(summary)
    return 1 if any(finding.level == "error" for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
