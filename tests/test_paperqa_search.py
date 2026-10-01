"""deep_literature (PaperQA2) tool — tested WITHOUT installing paper-qa.

paper-qa is a heavy optional dep that only lives on the eye-server. These tests fake it via
``sys.modules`` (the same idea as ``test_literature_search`` mocking ``httpx.get``) so the
whole suite runs locally with no network, no GPU, and no real model — a "mock Qwen". What we
verify here is the PLUMBING: the question reaches PaperQA unchanged, the cited answer is
parsed back out, graceful degradation works, and — importantly — the models are pinned to the
LOCAL endpoint + LOCAL embedding (the privacy contract). Answer QUALITY can only be checked on
the server with the real Qwen; no unit test can assert that.
"""

from __future__ import annotations

import sys
import types

from bioagent.tools.deep_literature.tool import make_paperqa_tool, run_paperqa


class _Ctx:
    """Minimal stand-in for HarnessContext (only the attrs the tool reads)."""

    def __init__(self, tunnel_port=9000, model="qwen3.6:35b-a3b", workspace=None):
        self.tunnel_port = tunnel_port
        self.model = model
        self.workspace = workspace


# --- a fake PaperQA response object the parser must read ----------------------


class _FakeDoc:
    formatted_citation = "Smith et al. (2024) Nature. doi:10.1/x"


class _FakeText:
    doc = _FakeDoc()


class _FakeContext:
    text = _FakeText()
    context = "RHO downregulation precedes photoreceptor apoptosis."
    score = 7


class _FakeResp:
    formatted_answer = "Yes — RHO loss is linked to apoptosis (Smith2024)."
    answer = "Yes, linked."
    contexts = [_FakeContext()]


def _install_fake_paperqa(monkeypatch, captured, *, ask_result=None, ask_raises=None):
    """Put a fake ``paperqa`` package into sys.modules so the lazy imports resolve to it."""
    paperqa = types.ModuleType("paperqa")
    settings_mod = types.ModuleType("paperqa.settings")

    class Settings:
        def __init__(self, **kwargs):
            captured["settings"] = kwargs

    class AgentSettings:
        def __init__(self, **kwargs):
            captured["agent"] = kwargs

    def ask(question, settings=None):
        captured["question"] = question
        captured["settings_obj"] = settings
        if ask_raises is not None:
            raise ask_raises
        return ask_result

    paperqa.Settings = Settings
    paperqa.ask = ask
    paperqa.settings = settings_mod
    class IndexSettings:
        def __init__(self, **kwargs):
            for _k, _v in kwargs.items():
                setattr(self, _k, _v)

    class ParsingSettings:
        def __init__(self, **kwargs):
            for _k, _v in kwargs.items():
                setattr(self, _k, _v)

    class MultimodalOptions:
        OFF = "off"

    class AnswerSettings:
        def __init__(self, **kwargs):
            captured["answer"] = kwargs
            for _k, _v in kwargs.items():
                setattr(self, _k, _v)

    settings_mod.AgentSettings = AgentSettings
    settings_mod.AnswerSettings = AnswerSettings
    settings_mod.IndexSettings = IndexSettings
    settings_mod.ParsingSettings = ParsingSettings
    settings_mod.MultimodalOptions = MultimodalOptions
    monkeypatch.setitem(sys.modules, "paperqa", paperqa)
    monkeypatch.setitem(sys.modules, "paperqa.settings", settings_mod)
    return captured


# --- tests --------------------------------------------------------------------


def test_empty_question_is_an_error():
    out = run_paperqa({"question": "   "}, _Ctx())
    assert out["status"] == "error"


def test_dependency_missing_when_paperqa_absent(monkeypatch):
    # sys.modules["paperqa"] = None makes `import paperqa` raise ImportError deterministically,
    # so the test holds whether or not paper-qa happens to be installed.
    monkeypatch.setitem(sys.modules, "paperqa", None)
    out = run_paperqa({"question": "anything"}, _Ctx())
    assert out["status"] == "dependency_missing"
    assert out["dependency"] == "paper-qa"


def test_unavailable_without_local_endpoint(monkeypatch):
    _install_fake_paperqa(monkeypatch, {}, ask_result=_FakeResp())
    out = run_paperqa({"question": "q"}, _Ctx(tunnel_port=None))
    assert out["status"] == "unavailable"
    assert "tunnel_port" in out["error"]


def test_success_parses_cited_answer(monkeypatch):
    captured = _install_fake_paperqa(monkeypatch, {}, ask_result=_FakeResp())
    out = run_paperqa(
        {"question": "Is RHO downregulation linked to photoreceptor apoptosis?"},
        _Ctx(tunnel_port=9000, model="qwen3.6:35b-a3b"),
    )
    assert out["status"] == "ok"
    assert out["answer"] == "Yes, linked."
    assert "Smith2024" in out["formatted_answer"]
    ctx0 = out["contexts"][0]
    assert ctx0["citation"].startswith("Smith et al.")
    assert ctx0["summary"].startswith("RHO downregulation")
    # the public question reaches PaperQA unchanged (only the question ever leaves)
    assert captured["question"].startswith("Is RHO")


def test_success_pins_models_to_local_endpoint(monkeypatch):
    """The privacy regression test: LLM -> loopback vLLM, embedding -> local sentence-transformers."""
    captured = _install_fake_paperqa(monkeypatch, {}, ask_result=_FakeResp())
    run_paperqa({"question": "q"}, _Ctx(tunnel_port=9000, model="qwen3.6:35b-a3b"))
    s = captured["settings"]
    assert s["llm"] == "openai/qwen3.6:35b-a3b"
    assert s["embedding"].startswith("st-")  # local; never a cloud embedding API
    cfg = s["llm_config"]["model_list"][0]["litellm_params"]
    assert cfg["api_base"] == "http://127.0.0.1:9000/v1"  # loopback tunnel, stays on host


def test_ask_failure_is_reported_not_fatal(monkeypatch):
    _install_fake_paperqa(monkeypatch, {}, ask_raises=RuntimeError("boom"))
    out = run_paperqa({"question": "q"}, _Ctx())
    assert out["status"] == "error"
    assert "boom" in out["error"]


def test_tool_self_describes():
    tool = make_paperqa_tool()
    assert tool.name == "deep_literature"
    assert tool.category == "literature"
    assert tool.reads_private_data is False
    assert "question" in tool.parameters["properties"]


# --- LLM call budget: timeout + reasoning effort (2026-09-30, Qwen3.8) -------------------------


_BUDGET_ENV = ("BIOAGENT_PAPERQA_LLM_TIMEOUT", "BIOAGENT_PAPERQA_REASONING_EFFORT",
               "BIOAGENT_PAPERQA_SUMMARY_REASONING_EFFORT", "BIOAGENT_PAPERQA_AGENT_TIMEOUT")


def _litellm_params(monkeypatch, env=None):
    """litellm_params of the three configs PaperQA gets: (llm, summary, agent), plus AgentSettings."""
    for name in _BUDGET_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, val in (env or {}).items():
        monkeypatch.setenv(name, val)
    captured = _install_fake_paperqa(monkeypatch, {}, ask_result=_FakeResp())
    run_paperqa({"question": "q"}, _Ctx())
    s, a = captured["settings"], captured["agent"]
    get = lambda cfg: cfg["model_list"][0]["litellm_params"]  # noqa: E731
    return get(s["llm_config"]), get(s["summary_llm_config"]), get(a["agent_llm_config"]), a


def test_llm_calls_get_a_timeout_sized_for_a_reasoning_model(monkeypatch):
    """lmi's own default is 60 s per request. Qwen3.8's answer over 35 passages never fit in it, and
    each attempt was retried until the job had run 21 minutes for an empty answer."""
    llm, summary, agent, _ = _litellm_params(monkeypatch)
    assert llm["timeout"] == summary["timeout"] == agent["timeout"] == 600.0


def test_llm_timeout_is_env_overridable_and_never_zero(monkeypatch):
    llm, summary, _, _ = _litellm_params(monkeypatch, {"BIOAGENT_PAPERQA_LLM_TIMEOUT": "900"})
    assert llm["timeout"] == summary["timeout"] == 900.0
    # A zero or garbage value would fail every call at once; it falls back to the default.
    for bad in ("0", "-5", "ten minutes"):
        llm, _, _, _ = _litellm_params(monkeypatch, {"BIOAGENT_PAPERQA_LLM_TIMEOUT": bad})
        assert llm["timeout"] == 600.0


def test_prose_calls_think_at_low_effort_through_extra_body(monkeypatch):
    """The effort rides in extra_body (merged into vLLM's JSON as top-level ``reasoning_effort``).
    As a top-level litellm param, litellm rejects it for a model it has not mapped."""
    llm, _, agent, _ = _litellm_params(monkeypatch)
    assert llm["extra_body"] == {"reasoning_effort": "low"}
    assert agent["extra_body"] == {"reasoning_effort": "low"}
    assert "reasoning_effort" not in llm and "reasoning_effort" not in agent


def test_per_passage_summaries_do_not_think(monkeypatch):
    _, summary, _, _ = _litellm_params(monkeypatch)
    assert summary["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_effort_env_overrides(monkeypatch):
    llm, summary, _, _ = _litellm_params(monkeypatch, {
        "BIOAGENT_PAPERQA_REASONING_EFFORT": "Medium",
        "BIOAGENT_PAPERQA_SUMMARY_REASONING_EFFORT": "low"})
    assert llm["extra_body"] == {"reasoning_effort": "medium"}
    assert summary["extra_body"] == {"reasoning_effort": "low"}
    # A value outside the known levels sends nothing: the served model's own default applies.
    llm, summary, _, _ = _litellm_params(monkeypatch, {
        "BIOAGENT_PAPERQA_REASONING_EFFORT": "default",
        "BIOAGENT_PAPERQA_SUMMARY_REASONING_EFFORT": "default"})
    assert "extra_body" not in llm and "extra_body" not in summary
    # The rest of the endpoint config is unchanged by the effort.
    assert llm["api_base"] == "http://127.0.0.1:9000/v1" and llm["temperature"] == 0.0


def test_agent_time_budget_is_set_and_env_overridable(monkeypatch):
    _, _, _, agent = _litellm_params(monkeypatch)
    assert agent["timeout"] == 500.0
    _, _, _, agent = _litellm_params(monkeypatch, {"BIOAGENT_PAPERQA_AGENT_TIMEOUT": "1200"})
    assert agent["timeout"] == 1200.0


# --- results that look like success but are not --------------------------------------------


class _Status:
    """Stand-in for paperqa's AgentStatus (a StrEnum)."""

    def __init__(self, value):
        self.value = value


class _FakeRespNoAnswer:
    """Run f3731e0b7136, job 7: 35 passages gathered, then every answer call timed out."""
    formatted_answer = ""
    answer = ""
    contexts = [_FakeContext()] * 35
    status = _Status("truncated")


def test_passages_without_an_answer_are_a_failure_not_ok(monkeypatch):
    _install_fake_paperqa(monkeypatch, {}, ask_result=_FakeRespNoAnswer())
    out = run_paperqa({"question": "Does the cone degenerate before the rod in RP?"}, _Ctx())
    # "ok" here let the lab accept a bare citation list as the literature step.
    assert out["status"] == "failed"
    assert out["n_contexts"] == 35
    assert out["agent_status"] == "truncated"
    assert "no answer" in out["error"]
    # The passages stay: chat grounds on them (without them it said the corpus had nothing).
    assert len(out["contexts"]) == 35 and out["contexts"][0]["citation"].startswith("Smith")


def test_a_rollout_cut_by_the_time_budget_does_not_blame_the_corpus(monkeypatch):
    class _Resp:
        formatted_answer = answer = ""
        contexts: list = []
        status = _Status("truncated")

    _install_fake_paperqa(monkeypatch, {}, ask_result=_Resp())
    out = run_paperqa({"question": "Which genes cause Stargardt disease?"}, _Ctx())
    assert out["status"] == "failed" and out["n_contexts"] == 0
    assert "time budget" in out["error"]
    assert "bag of identifiers" not in out["error"]


def test_empty_retrieval_keeps_the_corpus_explanation(monkeypatch):
    class _Resp:
        formatted_answer = answer = "I cannot answer."
        contexts: list = []
        status = _Status("success")

    _install_fake_paperqa(monkeypatch, {}, ask_result=_Resp())
    out = run_paperqa({"question": "What is the retinal phenotype of DDX41 mutations?"}, _Ctx())
    assert out["status"] == "failed" and "corpus does not cover" in out["error"]


def test_success_reports_how_the_rollout_ended(monkeypatch):
    class _Resp(_FakeResp):
        status = _Status("success")

    _install_fake_paperqa(monkeypatch, {}, ask_result=_Resp())
    out = run_paperqa({"question": "q"}, _Ctx())
    assert out["status"] == "ok" and out["agent_status"] == "success"


# --- the knobs reach the job: gateway env -> job args -> container env ---------------------


def test_cli_turns_budget_args_into_env_before_paperqa_reads_it(monkeypatch):
    """The job runs with --containall, so the knobs travel as --args and paperqa_cli turns them
    back into the env vars paperqa_search reads."""
    from bioagent.tools import paperqa_cli
    from bioagent.tools.deep_literature import tool as paperqa_search

    for name in _BUDGET_ENV:
        monkeypatch.delenv(name, raising=False)
    seen = {}
    monkeypatch.setattr(paperqa_search, "run_paperqa",
                        lambda args, ctx: seen.update({n: paperqa_cli.os.environ.get(n) for n in _BUDGET_ENV})
                        or {"status": "ok"})
    paperqa_cli.run_tool("deep_literature", "/tmp/ws", {
        "question": "q", "llm_timeout": 900, "reasoning_effort": "medium",
        "summary_reasoning_effort": "off", "agent_timeout": 1200})
    assert seen == {"BIOAGENT_PAPERQA_LLM_TIMEOUT": "900",
                    "BIOAGENT_PAPERQA_REASONING_EFFORT": "medium",
                    "BIOAGENT_PAPERQA_SUMMARY_REASONING_EFFORT": "off",
                    "BIOAGENT_PAPERQA_AGENT_TIMEOUT": "1200"}
    for name in _BUDGET_ENV:          # run_tool writes os.environ directly; do not leak
        monkeypatch.delenv(name, raising=False)


def test_gateway_forwards_every_cli_knob_from_its_env(monkeypatch, tmp_path):
    """Every env var paperqa_cli can set in the container must be forwarded by the gateway from the
    same env var on the eyeserver. A knob added on one side only is silently dropped by
    --containall. llm_base_url is the exception: it comes from the live allocation."""
    import threading

    from bioagent.gateway import app as gw
    from bioagent.gateway.settings import HPCSettings
    from bioagent.tools.paperqa_cli import _ARG_TO_ENV

    monkeypatch.setenv("BIOAGENT_PAPERQA_ON_HPC", "1")
    monkeypatch.setenv("BIOAGENT_PAPERQA_IMAGE", "/shared/containers/paperqa.sif")
    expected = {}
    for arg_key, env_key in _ARG_TO_ENV.items():
        if arg_key == "llm_base_url":
            continue
        expected[arg_key] = f"value-of-{arg_key}"
        monkeypatch.setenv(env_key, expected[arg_key])
    conn = types.SimpleNamespace(
        settings=HPCSettings(), executor=types.SimpleNamespace(username="tester"), mock=False,
        alloc=types.SimpleNamespace(node="hpc3-gpu-m54-01", port=39124), owner="tester",
        workspace=tmp_path, selected_model="RedHatAI/Qwen3.8-27B-INT4",
        chat_stop=threading.Event(), hpc_pysrc="/shared/pysrc/tester")
    executor = gw._build_literature_executor(conn)
    assert executor is not None
    missing = {k: v for k, v in expected.items() if executor.inject_args.get(k) != v}
    assert not missing, f"CLI knobs the gateway does not forward: {sorted(missing)}"
    assert executor.inject_args["llm_base_url"] == "http://hpc3-gpu-m54-01:39124/v1"
