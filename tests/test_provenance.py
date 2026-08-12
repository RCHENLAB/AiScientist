"""Provenance stamping for run_code executions (reproducibility layer)."""
from __future__ import annotations

import hashlib

from bioagent.agents import provenance as prov
from bioagent.agents.research_harness import HarnessContext
from bioagent.agents.research_lab import make_run_code_tool


def test_sha256_and_git_and_seed_helpers():
    assert prov.sha256_text("x") == prov.sha256_text("x")
    assert prov.sha256_text("x") != prov.sha256_text("y")
    # git_sha is a 40-char hex or None (this repo is a checkout, so expect a SHA), cached.
    sha = prov.git_sha()
    assert sha is None or (len(sha) == 40 and prov.git_sha() == sha)
    pre = prov.seed_preamble(7)
    assert "seed(7)" in pre and "manual_seed(7)" in pre


def test_git_sha_prefers_deploy_marker():
    """On an rsync-deployed server the checked-in .git is a stale relic; git_sha() must
    report the truthful `.deployed_sha` marker (repo root) over `git rev-parse`."""
    from pathlib import Path

    marker = Path(prov.__file__).resolve().parents[3] / ".deployed_sha"
    existed = marker.exists()
    backup = marker.read_text() if existed else None
    fake = "a" * 40
    try:
        marker.write_text(f"{fake} main 2026-07-03T00:00:00Z\n")
        prov._git_sha_cache = prov._UNSET          # bust the per-process cache
        assert prov.git_sha() == fake              # marker wins over the live checkout
    finally:
        if existed:
            marker.write_text(backup)
        else:
            marker.unlink()
        prov._git_sha_cache = prov._UNSET          # leave the cache clean for other tests


def test_seed_env_toggles(monkeypatch):
    monkeypatch.setenv(prov.SEED_ENV, "42")
    assert prov.resolve_seed() == 42
    monkeypatch.setenv(prov.SEED_ENABLE_ENV, "0")
    assert prov.seeding_enabled() is False
    monkeypatch.setenv(prov.SEED_ENABLE_ENV, "1")
    assert prov.seeding_enabled() is True


def test_dataset_hashes_content_and_absent(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world")
    fp, content, method = prov.dataset_hashes(str(f))
    assert method == "content"
    assert content == hashlib.sha256(b"hello world").hexdigest()
    assert fp and len(fp) == 16
    # missing file -> absent, no crash
    assert prov.dataset_hashes(str(tmp_path / "nope")) == (None, None, "absent")
    assert prov.dataset_hashes(None) == (None, None, "absent")


def test_dataset_hash_falls_back_to_fingerprint_over_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(prov, "MAX_CONTENT_HASH_BYTES", 4)
    prov._dataset_hash_cache.clear()
    f = tmp_path / "big.bin"
    f.write_bytes(b"0123456789")           # 10 bytes > 4-byte cap
    fp, content, method = prov.dataset_hashes(str(f))
    assert method == "fingerprint" and content is None and fp


class _FakeSandbox:
    """Minimal run_code executor exposing the sandbox path attributes."""
    def __init__(self, dataset_path=None):
        self.dataset_path = dataset_path
        self.work_dir = None
        self.artifacts_dir = None
        self.received = None

    def __call__(self, code: str) -> dict:
        self.received = code
        return {"status": "ok", "stdout": "done", "returncode": 0}


def test_run_code_tool_attaches_provenance_and_seeds(monkeypatch, tmp_path):
    monkeypatch.setenv(prov.SEED_ENABLE_ENV, "1")
    monkeypatch.setenv(prov.SEED_ENV, "0")
    ds = tmp_path / "adata.h5ad"
    ds.write_bytes(b"anndata-bytes")
    prov._dataset_hash_cache.clear()

    ex = _FakeSandbox(dataset_path=str(ds))
    tool = make_run_code_tool(ex)
    original = "import numpy as np\nprint(np.random.rand())"
    out = tool.executor({"code": original}, HarnessContext(model="qwen-test"))

    pv = out["provenance"]
    # hashes the ORIGINAL snippet, not the seed-prefixed one that actually ran
    assert pv["code_sha256"] == prov.sha256_text(original)
    assert pv["seed"] == 0
    assert pv["model"] == "qwen-test"
    assert pv["execution_mode"] == "local" and pv["returncode"] == 0
    assert pv["dataset_hash_method"] == "content"
    assert pv["dataset_sha256"] == hashlib.sha256(b"anndata-bytes").hexdigest()
    assert pv["git_sha"] is None or len(pv["git_sha"]) == 40
    assert pv["duration_ms"] >= 0 and pv["started_at"] and pv["finished_at"]
    # the executor actually ran the SEEDED code (preamble prepended)
    assert ex.received.endswith(original) and "seed(0)" in ex.received


def test_run_code_tool_seed_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv(prov.SEED_ENABLE_ENV, "0")
    ex = _FakeSandbox()
    tool = make_run_code_tool(ex)
    out = tool.executor({"code": "print(1)"}, HarnessContext())
    assert out["provenance"]["seed"] is None
    assert ex.received == "print(1)"        # no preamble injected


def test_provenance_never_breaks_a_run(monkeypatch):
    # An executor returning a non-dict must not crash _exec (provenance simply skipped).
    tool = make_run_code_tool(lambda code: "plain string result")
    out = tool.executor({"code": "print(1)"}, HarnessContext())
    assert out == "plain string result"
