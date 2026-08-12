"""Data-boundary safety guard — the privacy core in front of every external/LLM call.

Extracted out of the old ``kosmos_kernel`` module: this is not Kosmos-specific, it is
the shared boundary used by the literature tool, the Biomni adapter, and the research
harness. It refuses to let raw expression tables or secrets leave the process toward an
LLM / network, and records what derived fields are allowed instead.

Design (see the "structural allowlist" discussion):

- **Secrets are a HARD, always-on block.** A secret-like value anywhere in the prompt is
  refused regardless of endpoint — that is the one thing you cannot prevent by construction
  (an API key can be accidentally interpolated), so it stays a detective control.
- **Raw tabular data is judged by STRUCTURE, not by counting commas.** The old heuristic
  ("a line with 5+ comma fields is a CSV row") false-positives on anything comma-heavy — a
  list of artifact paths, a prose sentence — and false-negatives on tab/JSON matrices. It is
  replaced by ``_looks_like_data_matrix``: several CONSECUTIVE delimited lines with a
  CONSISTENT column count where MOST fields are numeric — i.e. an actual expression dump.
- **Raw-data detection is SOURCE-SCOPED.** The real control is preventive: the pipeline
  assembles prompts from derived/allowlisted fields, so system-built scaffolding (personas,
  step text, artifact paths, tool rosters, reference code) is trusted by construction and is
  NOT sniffed. Callers pass ``raw_data_scope`` = only the UNTRUSTED user-provided spans (the
  research question, mid-run notes); a user who pastes a raw matrix there is still caught.
  When ``raw_data_scope`` is omitted the whole prompt is scanned (back-compatible default).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataBoundaryPolicy:
    """Whether sanitized prompts may carry raw local data. The guard reads only
    ``allow_raw_data_to_llm``; callers map their own policy onto it."""

    allow_raw_data_to_llm: bool = False


@dataclass(frozen=True)
class DataBoundaryReport:
    dataset_path: str | None
    dataset_exists: bool
    dataset_size_bytes: int | None
    dataset_sha256_12: str | None
    raw_data_to_llm_allowed: bool
    prompt_contains_raw_data_risk: bool
    prompt_contains_secret_risk: bool
    allowed_prompt_fields: list[str]
    blocked_prompt_fields: list[str]
    notes: list[str]


class DataBoundaryGuard:
    """Detect prompt/data boundary violations before calling any external service or LLM."""

    SECRET_PATTERNS = (
        re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
        re.compile(r"sk-or-[A-Za-z0-9_-]{12,}"),
        re.compile(r"OPENROUTER_API_KEY\s*="),
        re.compile(r"ANTHROPIC_API_KEY\s*="),
        re.compile(r"OPENAI_API_KEY\s*="),
    )

    # A "data matrix" = at least this many CONSECUTIVE rows, each with the SAME column count
    # (>= MIN_COLS) where at least NUMERIC_FRACTION of the fields parse as numbers. This is
    # what an expression/table dump looks like; prose or a path list is not (non-numeric, or
    # inconsistent column counts). Delimiters tried: comma and tab (a real matrix uses one).
    _MIN_MATRIX_ROWS = 3
    _MIN_COLS = 5
    _NUMERIC_FRACTION = 0.6

    @staticmethod
    def _is_number(token: str) -> bool:
        token = token.strip()
        if not token:
            return False
        try:
            float(token)
            return True
        except ValueError:
            return False

    @classmethod
    def _looks_like_data_matrix(cls, text: str) -> bool:
        """True if ``text`` contains a numeric data grid (consistent numeric columns across
        several consecutive lines) — a real raw table, not merely comma-heavy prose."""
        for delim in (",", "\t"):
            run = 0
            expected_cols: int | None = None
            for line in text.splitlines():
                if delim not in line:
                    run, expected_cols = 0, None
                    continue
                fields = line.split(delim)
                ncols = len(fields)
                numeric = sum(1 for f in fields if cls._is_number(f))
                is_data_row = ncols >= cls._MIN_COLS and numeric / ncols >= cls._NUMERIC_FRACTION
                if not is_data_row:
                    # A single header row (label,label,…) may precede the data — don't count
                    # it, but don't let it break a run that hasn't started.
                    run, expected_cols = 0, None
                    continue
                if expected_cols is None or ncols == expected_cols:
                    run += 1
                    expected_cols = ncols
                    if run >= cls._MIN_MATRIX_ROWS:
                        return True
                else:
                    run, expected_cols = 1, ncols  # column count changed — start a new run
        return False

    def inspect_prompt(
        self,
        prompt: str,
        dataset_path: Path | None,
        policy: DataBoundaryPolicy,
        *,
        raw_data_scope: str | None = None,
    ) -> DataBoundaryReport:
        # Secrets: always scanned across the WHOLE prompt (a leaked key anywhere is a leak).
        secret_risk = any(pattern.search(prompt) for pattern in self.SECRET_PATTERNS)
        # Raw data: scanned only in the UNTRUSTED span when the caller marks one (system-built
        # scaffolding is trusted by construction); else the whole prompt (back-compatible).
        scope = prompt if raw_data_scope is None else raw_data_scope
        raw_data_risk = self._looks_like_data_matrix(scope)
        dataset_exists = bool(dataset_path and dataset_path.exists())
        size: int | None = None
        digest: str | None = None
        if dataset_path and dataset_path.exists():
            size = dataset_path.stat().st_size
            digest = hashlib.sha256(dataset_path.read_bytes()).hexdigest()[:12]
        notes = [
            "Only dataset path, size, short hash, high-level shape, and derived metrics may enter prompts.",
            "Raw expression rows, sample identifiers, and secrets are blocked from remote LLM prompts.",
        ]
        if raw_data_risk:
            notes.append("Prompt contains a numeric data grid and must be summarized before any external/LLM call.")
        if secret_risk:
            notes.append("Prompt contains a secret-like value and must not leave the process.")
        return DataBoundaryReport(
            dataset_path=str(dataset_path) if dataset_path else None,
            dataset_exists=dataset_exists,
            dataset_size_bytes=size,
            dataset_sha256_12=digest,
            raw_data_to_llm_allowed=policy.allow_raw_data_to_llm,
            prompt_contains_raw_data_risk=raw_data_risk,
            prompt_contains_secret_risk=secret_risk,
            allowed_prompt_fields=[
                "research_question",
                "dataset_manifest",
                "qc_metrics",
                "marker_effect_summary",
                "claim_limitations",
            ],
            blocked_prompt_fields=[
                "raw_expression_matrix",
                "patient_or_sample_identifiers",
                "api_keys",
                "unreviewed_slurm_submit_command",
            ],
            notes=notes,
        )

    def assert_safe_for_prompt(self, report: DataBoundaryReport) -> None:
        if report.prompt_contains_secret_risk:
            raise ValueError("Prompt contains secret-like content.")
        if report.prompt_contains_raw_data_risk and not report.raw_data_to_llm_allowed:
            raise ValueError("Prompt appears to contain raw table data.")


def endpoint_is_off_host(base_url: "str | None") -> bool:
    """Does a bound ``base_url`` send the prompt OFF this machine?

    ``None`` (the session's own SSH tunnel) and an explicit loopback URL stay on the box; anything
    else leaves it. This is the single source of truth for the data-boundary guard's raw-data
    decision, so it is deliberately CONSERVATIVE: an unparseable URL counts as remote. A hostname
    that merely resolves to a local address is still treated as remote — proving otherwise needs
    DNS, and a wrong "local" here means research data leaves UCI."""
    if not base_url:
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return True
    return host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
