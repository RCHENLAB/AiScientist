"""Brand env-var migration compatibility (BioAgent -> AiScientist).

The code reads ~1000 ``BIOAGENT_*`` env vars and the prod ``.env`` is full of them. To rename the
brand without breaking every deployed ``.env``, the two prefixes are aliased at startup: ops can set
EITHER ``AISCIENTIST_X`` or ``BIOAGENT_X`` and both reads resolve. When both are set, the new brand wins.
"""

from __future__ import annotations

from bioagent.core.config import apply_brand_env_aliases, env, load_project_env


def test_old_prefix_is_mirrored_to_new():
    e = {"BIOAGENT_RESULTS_DIR": "/data/x"}
    apply_brand_env_aliases(e)
    assert e["AISCIENTIST_RESULTS_DIR"] == "/data/x"    # new form now works too
    assert e["BIOAGENT_RESULTS_DIR"] == "/data/x"        # old form unchanged


def test_new_prefix_is_mirrored_to_old():
    e = {"AISCIENTIST_SECRET_KEY": "s3cret"}
    apply_brand_env_aliases(e)
    assert e["BIOAGENT_SECRET_KEY"] == "s3cret"          # legacy read sees the new-brand value
    assert e["AISCIENTIST_SECRET_KEY"] == "s3cret"


def test_new_prefix_wins_when_both_set():
    e = {"BIOAGENT_LLM_MODEL": "old", "AISCIENTIST_LLM_MODEL": "new"}
    apply_brand_env_aliases(e)
    assert e["BIOAGENT_LLM_MODEL"] == "new"              # both converge on the new-brand value
    assert e["AISCIENTIST_LLM_MODEL"] == "new"


def test_bare_prefixes_are_ignored():
    e = {"BIOAGENT_": "x", "AISCIENTIST_": "y", "UNRELATED": "z"}
    apply_brand_env_aliases(e)
    assert e == {"BIOAGENT_": "x", "AISCIENTIST_": "y", "UNRELATED": "z"}   # untouched


def test_idempotent():
    e = {"BIOAGENT_HPC_HOST": "hpc3"}
    apply_brand_env_aliases(e)
    once = dict(e)
    apply_brand_env_aliases(e)
    assert e == once


def test_env_helper_prefers_new_then_old(monkeypatch):
    monkeypatch.delenv("BIOAGENT_DATABASE_URL", raising=False)
    monkeypatch.delenv("AISCIENTIST_DATABASE_URL", raising=False)
    # legacy only -> found via fallback
    monkeypatch.setenv("BIOAGENT_DATABASE_URL", "legacy")
    assert env("DATABASE_URL") == "legacy"
    assert env("BIOAGENT_DATABASE_URL") == "legacy"      # a prefixed name is accepted too
    # new present -> wins
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", "new")
    assert env("DATABASE_URL") == "new"
    assert env("MISSING", default="d") == "d"


def test_load_project_env_aliases_a_new_brand_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("BIOAGENT_FOO", raising=False)
    monkeypatch.delenv("AISCIENTIST_FOO", raising=False)
    (tmp_path / ".env").write_text("AISCIENTIST_FOO=bar\n", encoding="utf-8")
    load_project_env(tmp_path)
    import os
    # A .env written with the NEW prefix satisfies the legacy os.environ read (and vice-versa).
    assert os.environ["BIOAGENT_FOO"] == "bar"
    assert os.environ["AISCIENTIST_FOO"] == "bar"
