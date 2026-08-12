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

from bioagent.tools.paperqa_search import make_paperqa_tool, run_paperqa


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
