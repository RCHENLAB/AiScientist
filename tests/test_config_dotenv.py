"""Regression tests for the .env loader — the inline-comment bug that silently ran prod vLLM at
32K instead of the configured 128K (and flipped BIOAGENT_RUN_CODE_ON_HPC to False), because
systemd's EnvironmentFile keeps inline comments and settings._int / the bool parse then fall back
to defaults on the unparseable value."""
from __future__ import annotations

import os

import pytest

from bioagent.core.config import _clean_value, load_dotenv


def _write_env(tmp_path, body: str):
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def test_inline_comment_is_stripped_from_unquoted_value(tmp_path, monkeypatch):
    monkeypatch.delenv("BIOAGENT_VLLM_MAX_MODEL_LEN", raising=False)
    env = _write_env(tmp_path, "BIOAGENT_VLLM_MAX_MODEL_LEN=131072      # 80GB A100 MAX_MODEL_Len\n")
    loaded = load_dotenv(env)
    assert loaded["BIOAGENT_VLLM_MAX_MODEL_LEN"] == "131072"
    assert os.environ["BIOAGENT_VLLM_MAX_MODEL_LEN"] == "131072"
    assert int(os.environ["BIOAGENT_VLLM_MAX_MODEL_LEN"]) == 131072  # parses now, no fallback


def test_bool_flag_with_inline_comment_reads_truthy(tmp_path, monkeypatch):
    # The prod line: `BIOAGENT_RUN_CODE_ON_HPC=1   # 1=send run_code to HPC3 …`
    monkeypatch.delenv("BIOAGENT_RUN_CODE_ON_HPC", raising=False)
    env = _write_env(tmp_path, "BIOAGENT_RUN_CODE_ON_HPC=1   # 1=send run_code to HPC3 (real cap)\n")
    load_dotenv(env)
    val = os.environ["BIOAGENT_RUN_CODE_ON_HPC"].strip().lower()
    assert val in ("1", "true", "yes", "on")   # the exact check settings.from_env uses


def test_systemd_polluted_value_is_repaired(tmp_path, monkeypatch):
    # Simulate systemd EnvironmentFile having already exported the SAME line WITH its comment.
    monkeypatch.setenv("BIOAGENT_VLLM_MAX_MODEL_LEN", "131072      # 80GB A100 MAX_MODEL_Len")
    env = _write_env(tmp_path, "BIOAGENT_VLLM_MAX_MODEL_LEN=131072      # 80GB A100 MAX_MODEL_Len\n")
    load_dotenv(env)
    assert os.environ["BIOAGENT_VLLM_MAX_MODEL_LEN"] == "131072"   # repaired, not left polluted


def test_genuine_external_override_is_respected(tmp_path, monkeypatch):
    # A REAL external override (different value, no comment) must still win over the .env.
    monkeypatch.setenv("BIOAGENT_VLLM_MAX_MODEL_LEN", "65536")
    env = _write_env(tmp_path, "BIOAGENT_VLLM_MAX_MODEL_LEN=131072   # default\n")
    load_dotenv(env)
    assert os.environ["BIOAGENT_VLLM_MAX_MODEL_LEN"] == "65536"


def test_hash_inside_quoted_value_survives(tmp_path, monkeypatch):
    # A '#' that is part of the value (e.g. a password) must NOT be treated as a comment when quoted.
    monkeypatch.delenv("BIOAGENT_SECRET", raising=False)
    env = _write_env(tmp_path, 'BIOAGENT_SECRET="p@ss#word with space"\n')
    loaded = load_dotenv(env)
    assert loaded["BIOAGENT_SECRET"] == "p@ss#word with space"


def test_hash_without_leading_space_is_kept(tmp_path, monkeypatch):
    # A URL fragment / token with '#' and NO preceding whitespace is part of the value, not a comment.
    monkeypatch.delenv("BIOAGENT_URL", raising=False)
    env = _write_env(tmp_path, "BIOAGENT_URL=http://h/p#frag\n")
    loaded = load_dotenv(env)
    assert loaded["BIOAGENT_URL"] == "http://h/p#frag"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("131072      # comment", "131072"),
        ("  64\t# tab-spaced comment", "64"),
        ('"quoted # keep"', "quoted # keep"),
        ("plain", "plain"),
        ("01:00:00   # wall clock", "01:00:00"),
    ],
)
def test_clean_value_cases(raw, expected):
    assert _clean_value(raw) == expected
