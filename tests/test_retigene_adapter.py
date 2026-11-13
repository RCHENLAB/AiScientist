"""The retigene-rag bridge: its adapter, the job's collector, and the relay that answers a curation's model
calls with the session's own model (an API key, the lab's shared model or the session's GPU). Offline: the
job is a fake runner, the relay folder a local one, the model a fake ``complete_fn``."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

import pytest

from aiscientist.agents import mcp_bridge as bridge, retigene
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import make_run_in_environment_tool
from aiscientist.gateway import llm_relay, mcp_model_relay

IMAGE = "docker://ghcr.io/example/mcp-retigene-rag@sha256:" + "b" * 64
INDEX = "/dfs3b/lab/software/AiScientist/retigene"
OBJ = {"type": "object", "additionalProperties": False}
TOOLS = [
    {"name": "search_sentences", "description": "Keyword search.", "inputSchema": {**OBJ, "properties": {
        "terms": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}}}},
    {"name": "submit_curation", "description": "The curation.", "inputSchema": {**OBJ, "properties": {
        "question": {"type": "string"}, "terms": {"type": "array", "items": {"type": "string"}},
        "estimate_only": {"type": "boolean"}, "max_cost_usd": {"type": "number"}}, "required": ["question"]}},
    {"name": "evaluate", "description": "Score it.", "inputSchema": {**OBJ, "properties": {
        "curation_id": {"type": "string"}, "reference": {"type": "string"}}, "required": ["curation_id"]}},
    {"name": "write_report", "description": "The report.", "inputSchema": {**OBJ, "properties": {
        "curation_id": {"type": "string"}}, "required": ["curation_id"]}},
]


# --- the relay: one job's prompts answered with the session's model ------------------------------------


def job_side(relay_dir: Path, messages, max_tokens=16000, timeout=10.0) -> dict:
    """What retigene-rag's RETIGENE_PROVIDER=relay does in the job: write one prompt, wait for its reply."""
    req, resp = relay_dir / "req", relay_dir / "resp"
    req.mkdir(parents=True, exist_ok=True)
    resp.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex
    (req / f".{rid}.tmp").write_text(json.dumps({"id": rid, "messages": messages, "max_tokens": max_tokens}))
    (req / f".{rid}.tmp").replace(req / f"{rid}.json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (resp / f"{rid}.json").is_file():
            return json.loads((resp / f"{rid}.json").read_text())
        time.sleep(0.01)
    raise AssertionError("no reply")


class Held:
    def __init__(self, usd):
        self.usd, self.closed = usd, False

    def close(self):
        self.closed = True


def fake_complete(seen, usage):
    def complete(messages, *, role=None, max_tokens=None, usage_out=None):
        seen.append({"role": role, "max_tokens": max_tokens, "messages": messages})
        if usage_out is not None:
            usage_out.update(usage)
        return '{"items": []}'
    return complete


def session_relay(tmp_path, binding, **kw):
    return mcp_model_relay.SessionModelRelay(lambda: binding, llm_relay.LocalFS, str(tmp_path / "work"),
                                             poll_s=0.01, log=lambda s: None, **kw)


@pytest.mark.parametrize("kind", ["api_key", "shared_model", "session_gpu"])
def test_relay_answers_with_the_sessions_model(tmp_path, kind):
    seen, held = [], []
    usage = {"prompt_tokens": 1000, "completion_tokens": 200, "input_tokens": 300, "model": "m"}
    if kind == "api_key":
        binding = mcp_model_relay.Binding(
            complete=fake_complete(seen, usage), model="claude-haiku-5-5", metered=True, rates=(0.1, 0.5),
            cap=2.0, price=lambda u: 0.004, allowance=lambda usd: held.append(Held(usd)) or held[-1])
    else:  # the shared model and the session's GPU: the same complete_fn path, free, no key to guard
        binding = mcp_model_relay.Binding(complete=fake_complete(seen, usage), model="served-model", metered=False)
    call = session_relay(tmp_path, binding).open("retigene")
    assert call.folder.startswith(str(tmp_path / "work") + "/retigene-relay/")
    msgs = [{"role": "system", "content": "rules"}, {"role": "user", "content": "GENE: PAX6"}]
    with call.serving():
        reply = job_side(Path(call.folder), msgs)
    assert reply["text"] == '{"items": []}'
    assert reply["usage"] == {"prompt_tokens": 1000, "completion_tokens": 200, "model": "m"}
    assert seen == [{"role": "curation", "max_tokens": 16000, "messages": msgs}]
    assert call.stats.calls == 1 and not call.stats.stopped
    if kind == "api_key":
        assert reply["cost_usd"] == 0.004 and call.stats.cost_usd == pytest.approx(0.004)
        assert held[0].usd == 2.0 and held[0].closed  # the spike allowance ends with the job
    else:
        assert reply["cost_usd"] == 0.0 and not held


def test_relay_stops_at_the_cap_and_at_the_runs_stop(tmp_path):
    seen, stop = [], threading.Event()
    binding = mcp_model_relay.Binding(complete=fake_complete(seen, {}), model="claude-haiku-5-5", metered=True,
                                      cap=0.002, price=lambda u: 0.001)
    call = session_relay(tmp_path, binding, should_stop=stop.is_set).open("retigene")
    msgs = [{"role": "user", "content": "x"}]
    with call.serving():
        assert "text" in job_side(Path(call.folder), msgs)
        assert "text" in job_side(Path(call.folder), msgs)
        third = job_side(Path(call.folder), msgs)
    assert third["kind"] == "stop" and "cap" in third["error"] and len(seen) == 2
    free = mcp_model_relay.Binding(complete=fake_complete(seen, {}), model="m", metered=False)
    call = session_relay(tmp_path, free, should_stop=stop.is_set).open("retigene")
    stop.set()
    with call.serving():
        assert job_side(Path(call.folder), msgs) == {"error": "the run was stopped", "kind": "stop"}


def test_no_model_to_read_with_is_a_refusal(tmp_path):
    from aiscientist.gateway.errors import GatewayError

    def unbound():
        raise GatewayError("This session's model is not running", stage="mcp_relay")

    relay = mcp_model_relay.SessionModelRelay(unbound, llm_relay.LocalFS, str(tmp_path))
    with pytest.raises(ValueError, match="not running"):
        relay.open("retigene")


@pytest.mark.parametrize("kind", ["api_key", "shared_model", "session_gpu"])
def test_the_gateway_binds_the_sessions_model(monkeypatch, kind):
    from types import SimpleNamespace

    from aiscientist.gateway import app

    complete = object()
    creds = {"api_key": {"id": "k1", "model": "claude-sonnet-5-5", "lab_model": "claude-haiku-5-5",
                         "provider": "anthropic"},
             "shared_model": {"id": "shared", "builtin": "shared", "model": "deepseek-v4.1-flash"},
             "session_gpu": None}
    monkeypatch.setattr(app, "_model_ready", lambda conn: True)
    monkeypatch.setattr(app, "_lab_llm", lambda conn: SimpleNamespace(complete_fn=complete, model="served"))
    monkeypatch.setattr(app, "_bound_credential", lambda conn: creds[kind])
    monkeypatch.setattr(app, "_corpus_scan_cost_cap", lambda conn: (3.5, "what is left of your monthly credit"))
    keys = []
    monkeypatch.setattr(app, "_key_use", lambda conn, cred, **kw: keys.append((cred, kw)) or Held(0))
    b = app._retigene_relay_binding(SimpleNamespace())
    assert b.complete is complete
    if kind == "api_key":
        assert (b.model, b.metered, b.cap) == ("claude-haiku-5-5", True, 3.5)  # the lab-role model, as complete_fn
        assert b.rates == (0.1, 0.5)
        b.allowance(3.5).close()
        assert keys[0][0]["id"] == "k1" and keys[0][1]["allowance_usd"] == 3.5
        assert b.price({"input_tokens": 10_000, "completion_tokens": 0, "model": "claude-haiku-5-5"}) == \
            pytest.approx(0.001)                                  # the price table, as the corpus scan
    else:
        assert (b.model, b.metered, b.cap, b.allowance) == ("served", False, None, None)
        assert b.price({"prompt_tokens": 10**6}) == 0.0
    monkeypatch.setattr(app, "_model_ready", lambda conn: False)
    with pytest.raises(app.GatewayError, match="not running"):
        app._retigene_relay_binding(SimpleNamespace())


def test_the_run_monitor_passes_the_callers_ceiling_and_usage_through():
    from aiscientist.gateway import app

    seen = []
    complete = fake_complete(seen, {"prompt_tokens": 5})
    watched, _ = app._watch_llm(None, complete, lambda m, t: None)
    usage: dict = {}
    assert watched([{"role": "user", "content": "x"}], role="curation", max_tokens=16000, usage_out=usage)
    assert seen[0]["role"] == "curation" and seen[0]["max_tokens"] == 16000 and usage == {"prompt_tokens": 5}


# --- the adapter --------------------------------------------------------------------------------------


class FakeCall:
    def __init__(self, binding):
        self.binding, self.folder, self.serving_now, self.stats = binding, "/hpc/run/work/retigene-relay/abc", False, None

    def serving(self):
        import contextlib

        @contextlib.contextmanager
        def cm():
            self.serving_now = True
            try:
                yield self
            finally:
                self.serving_now = False
        return cm()


class FakeRelay:
    def __init__(self, binding):
        self.binding, self.calls = binding, []

    def open(self, name):
        self.calls.append(FakeCall(self.binding))
        return self.calls[-1]


def adapter():
    return retigene.RetigeneAdapter(planner_name=lambda n: "retigene_" + n if n == "write_report" else n)


def test_adapter_environment_for_a_curation(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_RETIGENE_INDEX", INDEX)
    b = mcp_model_relay.Binding(complete=None, model="claude-haiku-5-5", metered=True, rates=(0.1, 0.5), cap=1.25)
    executor = type("E", (), {"model_relay": FakeRelay(b)})()
    p = adapter().prepare({"question": "q", "max_cost_usd": 5}, executor, tool="submit_curation")
    assert p.read_only == (INDEX,) and p.tool_cache and p.around is not None
    assert p.arguments["max_cost_usd"] == 1.25            # never past what is left of the user's budget
    for line in (f"export RETIGENE_INDEX={INDEX}", "RETIGENE_PROVIDER=relay",
                 "RETIGENE_RELAY_DIR=/hpc/run/work/retigene-relay/abc", "RETIGENE_CHECK_MODEL=claude-haiku-5-5",
                 "RETIGENE_REVIEW_MODEL=claude-haiku-5-5", "RETIGENE_PRICE_IN=0.1 RETIGENE_PRICE_OUT=0.5",
                 'RETIGENE_CACHE="$AISCIENTIST_TOOL_CACHE/retigene-rag/cache"'):
        assert line in p.setup
    estimate = adapter().prepare({"question": "q", "estimate_only": True}, executor, tool="submit_curation")
    assert estimate.around is None and "RETIGENE_PROVIDER=relay" in estimate.setup  # priced for this model
    free = mcp_model_relay.Binding(complete=None, model="served", metered=False)
    p = adapter().prepare({"question": "q"}, type("E", (), {"model_relay": FakeRelay(free)})(), tool="submit_curation")
    assert "max_cost_usd" not in p.arguments and "RETIGENE_PRICE_IN=0.0" in p.setup


def test_adapter_clamps_and_refusals(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_RETIGENE_INDEX", INDEX)
    a, executor = adapter(), object()
    search = a.prepare({"terms": ["haploinsufficien"], "limit": 200}, executor, tool="search_sentences")
    assert search.arguments["limit"] == retigene.MAX_SENTENCE_LINES and search.around is None
    assert "RETIGENE_PROVIDER" not in search.setup and search.read_only == (INDEX,)
    assert a.prepare({"curation_id": "c", "limit": 500}, executor, tool="get_evidence").arguments["limit"] == 20
    assert a.prepare({"curation_id": "c"}, executor, tool="get_genes").arguments["top"] == 100
    with pytest.raises(ValueError, match="bundled curator reference"):
        a.prepare({"curation_id": "c", "reference": "/etc/passwd"}, executor, tool="evaluate")
    with pytest.raises(ValueError, match="cannot answer the curation"):
        a.prepare({"question": "q"}, executor, tool="submit_curation")
    monkeypatch.setenv("AISCIENTIST_RETIGENE_INDEX", "relative/retigene")
    with pytest.raises(ValueError, match="not set up"):
        a.prepare({"terms": ["x"]}, executor, tool="search_sentences")


def run_collector(tmp_path, monkeypatch, capsys, result, tool="submit_curation"):
    data = tmp_path / "env" / "retigene-rag"
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(result))
    for key, value in {"AISCI_MCP_RESULT": result_file, "AISCIENTIST_ARTIFACTS": tmp_path / "artifacts",
                       "AISCI_DATA_DIR": tmp_path / "env", "AISCI_MCP_TOOL": tool,
                       "AISCIENTIST_ENV_DIR": tmp_path / "env", "RETIGENE_INDEX": tmp_path / "index",
                       "RETIGENE_CACHE": tmp_path / "cache" / "retigene-rag" / "cache",
                       "AISCIENTIST_TOOL_CACHE": tmp_path / "cache"}.items():
        monkeypatch.setenv(key, str(value))
    exec(retigene.COLLECT, {})
    out = capsys.readouterr().out
    return data, out, json.loads(out)["result"]


def test_collector_names_no_server_path_and_copies_the_tables(tmp_path, monkeypatch, capsys):
    cid = "haploinsufficiency-0123456789ab"
    folder = tmp_path / "env" / "retigene-rag" / cid
    folder.mkdir(parents=True)
    for name in ("genes.csv", "sentences.csv", "report.md", "request.json"):
        (folder / name).write_text(name)
    result = {"curation_id": cid, "state": "COMPLETED", "genes_A": ["PAX6"],
              "files": {"genes": str(folder / "genes.csv"), "sentences": str(folder / "sentences.csv")},
              "note": f"index {tmp_path / 'index'}; cache {tmp_path / 'cache' / 'retigene-rag' / 'cache'}"}
    _, out, shown = run_collector(tmp_path, monkeypatch, capsys, result)
    assert str(tmp_path) not in out
    assert shown["files"] == {"genes": f"retigene/{cid}/genes.csv", "sentences": f"retigene/{cid}/sentences.csv"}
    assert shown["note"] == "index the RetiGene index; cache the reply cache"
    copied = sorted(p.name for p in (tmp_path / "artifacts" / "retigene" / cid).iterdir())
    assert copied == ["genes.csv", "report.md", "sentences.csv"]  # deliverables only, not the request
    assert json.loads((tmp_path / "artifacts" / "retigene" / "submit_curation.json").read_text()) == shown


def test_collector_bounds_long_results_and_keeps_them_whole(tmp_path, monkeypatch, capsys):
    rows = [{"gene": f"G{i}", "quote": "x" * 400} for i in range(200)]
    _, out, shown = run_collector(tmp_path, monkeypatch, capsys, {"total": 200, "rows": rows}, tool="get_evidence")
    assert len(out) < 14000 and shown["rows"] and shown["rows_omitted"] == 200 - len(shown["rows"])
    assert shown["full_result"] == "retigene/get_evidence.json"
    assert len(json.loads((tmp_path / "artifacts" / shown["full_result"]).read_text())["rows"]) == 200
    lines = "\n".join(f"PMID {i} p1 c0: " + "y" * 300 for i in range(120))
    _, out, shown = run_collector(tmp_path, monkeypatch, capsys, {"sentences": lines, "total_sentences": 120},
                                  tool="search_sentences")
    assert len(out) < 14000 and shown["sentences_omitted"] > 0
    assert shown["sentences"].count("\n") + 1 + shown["sentences_omitted"] == 120


# --- the bridge ---------------------------------------------------------------------------------------


def write_config(root: Path, **overrides):
    snapshot = root / "retigene_tools.json"
    snapshot.write_text(json.dumps(TOOLS))
    row = {"name": "retigene-rag", "version": "0.0.1", "release_tag": "retigene-rag-v0.0.1", "image": IMAGE,
           "entrypoint": "retigene-rag-mcp", "skill": retigene.SKILL, "runs_on": "hpc:literature",
           "tools_file": snapshot.name, "tools_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
           "adapter": "retigene", "timeout": 3600, "aliases": {"write_report": "retigene_write_report"},
           "resources": {"submit_curation": [2, 16, "01:00:00"]}}
    row.update(overrides)
    config = root / "mcp_servers.json"
    config.write_text(json.dumps({"schema_version": 1, "servers": [row]}))
    return config


def write_skill(root: Path):
    folder = root / retigene.SKILL
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {retigene.SKILL}\ndescription: Query the RetiGene papers.\nmetadata:\n"
        "  category: literature\n  version: '0.0.1'\n  status: verified\n"
        f"  image: '{IMAGE}'\n  cpus: '2'\n  mem_gb: '8'\n  time_limit: '01:00:00'\n"
        "  requires-network: none\n  requires-gpu: 'false'\n  requires-secrets: none\n"
        "  scheduler: none\n---\nCall the retigene-rag tools.\n")


@pytest.mark.parametrize("overrides, message", [
    ({"adapter": "nope"}, "Unknown MCP domain adapter"),
    ({"adapter": "json"}, "require the retigene domain adapter"),
    ({"name": "other-rag", "release_tag": "other-rag-v0.0.1"}, "require the retigene domain adapter"),
    ({"aliases": {}}, "write_report is reserved"),
])
def test_the_bridge_refuses_unknown_and_mismatched_adapters(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        bridge.load_servers(write_config(tmp_path, **overrides))


class Runner:
    supports_skill_policy = True

    def __init__(self, relay):
        self.model_relay, self.calls = relay, []
        self.result = {"curation_id": "haploinsufficiency-0123456789ab", "state": "COMPLETED", "genes_A": ["PAX6"],
                       "index": {"dataset": "retigene", "version": "v2026.11", "fingerprint": "sha256:abc"}}

    def run_in_image(self, command, image, **kwargs):
        relay = self.model_relay.calls[-1] if self.model_relay.calls else None
        self.calls.append((command, image, kwargs, bool(relay and relay.serving_now)))
        return {"status": "ok", "returncode": 0, "stdout": json.dumps({"result": copy.deepcopy(self.result)}),
                "artifacts": ["retigene/haploinsufficiency-0123456789ab/genes.csv"]}


@pytest.fixture
def registry(tmp_path, monkeypatch, skill_release):
    monkeypatch.setenv("AISCIENTIST_MCP_CONFIG", str(write_config(tmp_path)))
    monkeypatch.setenv("AISCIENTIST_RETIGENE_INDEX", INDEX)
    write_skill(tmp_path / "skills")
    skill_release(tmp_path / "skills")


def test_the_curation_runs_inside_the_sessions_relay(registry, tmp_path, monkeypatch):
    b = mcp_model_relay.Binding(complete=None, model="claude-haiku-5-5", metered=True, rates=(0.1, 0.5), cap=4.0)
    runner = Runner(FakeRelay(b))
    tools = {t.name: t for t in bridge.build_mcp_tools(runner, make_run_in_environment_tool(runner))}
    assert {"search_sentences", "submit_curation", "evaluate", "retigene_write_report"} == set(tools)
    assert "session's own model" in tools["submit_curation"].description
    ctx = HarnessContext(workspace=tmp_path / "run")
    out = tools["submit_curation"].executor({"question": "What are haploinsufficient genes?"}, ctx)
    assert out["status"] == "ok" and out["result"]["genes_A"] == ["PAX6"]
    command, image, kw, serving = runner.calls[0]
    assert serving and image == IMAGE                       # the job ran while the relay answered
    assert kw["read_only"] == (INDEX,) and kw["tool_cache"] is True
    assert (kw["cpus"], kw["mem_gb"], kw["time_limit"]) == (2, 16, "01:00:00")
    assert "RETIGENE_PROVIDER=relay" in command and "AISCI_EXECUTOR=inline" in command
    assert json.loads((ctx.workspace / "manifest.json").read_text())["retigene_index"]["version"] == "v2026.11"
    # a search needs no model: no relay, and the index still bound
    tools["search_sentences"].executor({"terms": ["haploinsufficien"]}, ctx)
    assert runner.calls[1][2]["read_only"] == (INDEX,) and not runner.calls[1][3]
    assert len(runner.model_relay.calls) == 1
    runner.result = {"curation_id": "c", "state": "FAILED", "error": "RelayStopped: the run was stopped"}
    failed = tools["submit_curation"].executor({"question": "q"}, ctx)
    assert failed["status"] == "error" and "the run was stopped" in failed["error"]


def test_without_an_index_the_tools_are_not_offered(registry, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_RETIGENE_INDEX")
    runner = Runner(FakeRelay(None))
    assert bridge.build_mcp_tools(runner, make_run_in_environment_tool(runner)) == []
