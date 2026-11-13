"""Local MCP deployment policy stays separate from scientific retrieval code."""
import json
import os
from types import SimpleNamespace

import pytest

from aiscientist.gateway import local_literature_mcp as local


def test_docker_command_is_digest_pinned_offline_and_mounts_only_corpus_and_output(tmp_path):
    index, hf, output = (tmp_path / name for name in ("index", "hf", "output"))
    index.mkdir()
    (index / "manifest.csv").write_text("fixture")
    image = "docker://ghcr.io/example/literature@sha256:" + "a" * 64
    args = local.command("/usr/bin/docker", image, index, hf, output, "aisci-lit-fixture")
    assert "--network=none" in args and "--pull=never" in args and "--read-only" in args
    assert "--cpus=2" in args and "--memory=8g" in args and "--pids-limit=128" in args
    assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
    assert mounts == [f"type=bind,src={index},dst=/corpus,readonly",
                      f"type=bind,src={hf},dst=/hf_cache,readonly", f"type=bind,src={output},dst=/output"]
    assert image.removeprefix("docker://") in args
    assert "LITERATURE_PAPERS=/corpus" in args  # cache-only mirror has no papers/ child
    assert "docker.sock" not in " ".join(args) and "API_KEY" not in " ".join(args)
    assert 'call_tool("search_corpus"' in args[-1]


def test_mount_separator_injection_is_refused(tmp_path):
    with pytest.raises(ValueError, match="separators"):
        local.command("docker", "image", tmp_path / "corpus,readonly=false", tmp_path, tmp_path, "name")


def test_opt_in_is_required(monkeypatch):
    monkeypatch.delenv("AISCIENTIST_LOCAL_CORPUS_MCP", raising=False)
    monkeypatch.setattr(local, "registered_server", lambda: pytest.fail("deployment inspected without opt in"))
    assert local.make_tool() is None


def test_local_search_validates_schema_and_never_loads_legacy_science(tmp_path, monkeypatch):
    from aiscientist.gateway import local_corpus
    from aiscientist.agents import skills
    index, hf = tmp_path / "mechanism_cache", tmp_path / "hf_cache"
    index.mkdir()
    hf.mkdir()
    for name in ("chunks.jsonl", "chunk_vecs.npy"):
        (index / name).write_bytes(b"fixture")
    monkeypatch.setenv("AISCIENTIST_LOCAL_CORPUS_ROOT", str(tmp_path))
    monkeypatch.setenv("AISCIENTIST_LOCAL_CORPUS_MCP", "docker")
    schema = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
              "required": ["queries"], "additionalProperties": False}
    server = SimpleNamespace(skill="registered", image="docker://example@sha256:" + "a" * 64,
                             version="0.0.3", tools=({"name": "search_corpus", "description": "Search", "inputSchema": schema},),
                             identity=lambda: {"name": "literature", "version": "0.0.3"})
    monkeypatch.setattr(local, "registered_server", lambda: server)
    monkeypatch.setattr(local.shutil, "which", lambda binary: "/usr/bin/docker")
    monkeypatch.setattr(skills, "get_skill", lambda name: SimpleNamespace(image=server.image))
    monkeypatch.setattr(local_corpus, "_embedder", lambda *a: pytest.fail("gateway loaded legacy embedding model"))
    calls = []
    def execute(argv, payload, output, **kw):
        calls.append((argv, json.loads(json.dumps(payload))))
        return {"status": "ok", "contexts": [{"pmid": "123", "text": "public fixture"}]}
    monkeypatch.setattr(local, "_execute", execute)
    tool = local_corpus.make_local_tool()
    ctx = SimpleNamespace(workspace=tmp_path / "private-run")
    actual = tool.executor({"queries": ["PRPF31"]}, ctx)
    assert actual["status"] == "ok" and actual["mcp"]["execution_mode"] == "local-docker"
    assert calls[0][1] == {"version": "0.0.3", "schema": schema, "arguments": {"queries": ["PRPF31"]}}
    assert tool.executor({"queries": ["PRPF31"], "model": "other"}, ctx)["status"] == "error"
    assert len(calls) == 1
    ctx.should_cancel = lambda: True
    assert tool.executor({"queries": ["PRPF31"]}, ctx)["status"] == "cancelled"
    assert len(calls) == 1
