"""Session model settings remain trusted metadata and credentials use skill policy."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aiscientist.agents.mcp_model import ModelBinding, prepare_model, run_with_model
from aiscientist.agents.skills_registry import POLICY


def test_guarded_relay_binding_has_no_provider_credentials_and_stops_on_failure(tmp_path):
    from aiscientist.agents.mcp_model import RelayBinding, model_files
    events = []
    relay = SimpleNamespace(stop=lambda: events.append("stop"))
    def start(directory):
        events.append(directory)
        return relay
    executor = SimpleNamespace(work_dir=str(tmp_path), supports_skill_policy=True,
                               mcp_model_binding=lambda: RelayBinding("selected-native-model", start))
    values = {"PHENOTYPE_MODEL_ENDPOINT": "https://global.invalid/v1", "PHENOTYPE_MODEL": "global"}
    bound, setup = prepare_model("phenotype-diagnosis", "map_phenotype_to_hpo", executor, values)
    assert values == {} and "selected-native-model" in setup
    assert bound.relay_directory.startswith(str(tmp_path) + "/.mcp-model-")
    assert "_aisci_mcp_relay.py" in model_files(bound)
    assert "MODEL_API_KEY_FILE" in setup and not hasattr(bound.binding, "api_key")
    token = POLICY.set({"network": "runtime", "secret_values": {}})
    try:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            run_with_model(bound, executor, lambda: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
        assert events == [bound.relay_directory, "stop"]
        assert POLICY.get()["secret_values"] == {}
    finally:
        POLICY.reset(token)


def test_extraction_budget_only_binds_to_known_price_relay(tmp_path):
    from aiscientist.agents.mcp_model import RelayBinding
    starts = []
    def start(directory, max_cost_usd=None):
        starts.append((directory, max_cost_usd))
        return SimpleNamespace(stop=lambda: None)
    executor = SimpleNamespace(work_dir=str(tmp_path), supports_skill_policy=True,
                               mcp_model_binding=lambda: RelayBinding("priced-model", start, priced=True))
    bound, setup = prepare_model("literature", "extract_haploinsufficient_genes", executor, {},
                                  arguments={"max_cost_usd": 0.5})
    assert "--priced" in setup and bound.max_cost_usd == 0.5
    token = POLICY.set({"network": "runtime", "secret_values": {}})
    try:
        assert run_with_model(bound, executor, lambda: "ok") == "ok"
    finally:
        POLICY.reset(token)
    assert starts == [(bound.relay_directory, 0.5)]
    executor.mcp_model_binding = lambda: RelayBinding("unpriced-model", start)
    with pytest.raises(ValueError, match="no known price"):
        prepare_model("literature", "extract_haploinsufficient_genes", executor, {}, arguments={"max_cost_usd": 0.5})


def test_live_session_binding_overrides_global_without_persisting_key():
    bindings = [ModelBinding("http://node-a:1234/v1", "session-a", "synthetic-secret"),
                ModelBinding("http://node-b:2345/v1", "session-b", "new-synthetic-secret")]
    executor = SimpleNamespace(mcp_model_binding=lambda: bindings.pop(0))
    values = {"LITERATURE_MODEL_ENDPOINT": "https://global.invalid/v1", "LITERATURE_MODEL": "global"}
    bound, setup = prepare_model("literature", "deep_literature", executor, values)
    assert values == {"LITERATURE_MODEL_ENDPOINT": "http://node-a:1234/v1", "LITERATURE_MODEL": "session-a"}
    assert "synthetic-secret" not in setup + repr(bound) + json.dumps(values)
    second, _ = prepare_model("literature", "deep_literature", executor, values)
    assert second.binding.model == "session-b" and values["LITERATURE_MODEL_ENDPOINT"] == "http://node-b:2345/v1"


def test_no_session_model_does_not_switch_to_global():
    values = {"PHENOTYPE_MODEL_ENDPOINT": "https://global.invalid/v1", "PHENOTYPE_MODEL": "global"}
    binding, setup = prepare_model("phenotype-diagnosis", "map_phenotype_to_hpo", SimpleNamespace(mcp_model_binding=lambda: None), values)
    assert binding is None and values == {}
    assert "unset PHENOTYPE_MODEL_ENDPOINT PHENOTYPE_MODEL PHENOTYPE_MODEL_API_KEY" in setup


def test_nonmodel_tools_never_resolve_or_read_credentials():
    def forbidden():
        raise AssertionError("nonmodel call resolved a credential")
    executor = SimpleNamespace(mcp_model_binding=forbidden)
    for server, tool in [("literature", "search_corpus"), ("literature", "literature_search"),
                         ("phenotype-diagnosis", "run_lirical"), ("data-utilities", "make_schematic")]:
        assert prepare_model(server, tool, executor, {}) == (None, "")


def test_native_anthropic_and_credential_urls_are_refused():
    for endpoint, protocol in [("https://proxy.invalid/v1", "anthropic"),
                               ("https://api.anthropic.com/v1", "openai"),
                               ("https://name:placeholder@example.org/v1", "openai"),
                               ("https://example.org/v1?key=private", "openai")]:
        with pytest.raises(ValueError) as exc:
            ModelBinding(endpoint, "model", "private", protocol)
        assert "private" not in str(exc.value)


def test_user_key_callback_refusal_is_not_a_global_fallback():
    def unavailable():
        raise ValueError("user-owned keys require the gateway metering and credential proxy")
    with pytest.raises(ValueError, match="gateway metering"):
        prepare_model("literature", "deep_literature", SimpleNamespace(mcp_model_binding=unavailable),
                      {"LITERATURE_MODEL_ENDPOINT": "https://global.invalid/v1", "LITERATURE_MODEL": "global"})


def test_operator_file_priority_without_file_path_in_setup(monkeypatch, tmp_path):
    key_file = tmp_path / "credential"
    key_file.write_text("file-secret\n")
    monkeypatch.setenv("LITERATURE_MODEL_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("LITERATURE_MODEL_API_KEY", "ignored-secret")
    values = {"LITERATURE_MODEL_ENDPOINT": "http://cluster-model:1234/v1", "LITERATURE_MODEL": "operator"}
    bound, setup = prepare_model("literature", "deep_literature", SimpleNamespace(), values)
    assert bound.binding.api_key == "file-secret"
    assert str(key_file) not in setup and "file-secret" not in setup + json.dumps(values)


def test_per_call_policy_injects_key_and_restores_without_mutation():
    binding, _ = prepare_model("literature", "deep_literature",
                              SimpleNamespace(mcp_model_binding=lambda: ModelBinding("http://node:1234/v1", "model", "synthetic-secret")), {})
    original = {"network": "runtime", "secret_values": {"OTHER_TOKEN": "other"}, "gpu": False}
    token = POLICY.set(original)
    try:
        def worker():
            current = POLICY.get()
            assert current is not original
            assert current["secret_values"] == {"OTHER_TOKEN": "other", "LITERATURE_MODEL_API_KEY": "synthetic-secret"}
            return {"stdout": "accidental synthetic-secret echo", "nested": [{"synthetic-secret": "synthetic-secret"}]}
        result = run_with_model(binding, SimpleNamespace(supports_skill_policy=True), worker)
        assert "synthetic-secret" not in json.dumps(result)
        assert POLICY.get() is original and original["secret_values"] == {"OTHER_TOKEN": "other"}
        with pytest.raises(ValueError, match=r"\[REDACTED\]"):
            run_with_model(binding, SimpleNamespace(supports_skill_policy=True), lambda: (_ for _ in ()).throw(RuntimeError("synthetic-secret")))
        assert POLICY.get() is original
    finally:
        POLICY.reset(token)


def test_credentials_require_protected_executor_and_declared_network():
    binding, _ = prepare_model("phenotype-diagnosis", "map_phenotype_to_hpo",
                              SimpleNamespace(mcp_model_binding=lambda: ModelBinding("http://node:1234/v1", "model", "secret")), {})
    token = POLICY.set(None)
    try:
        with pytest.raises(ValueError, match="cannot protect"):
            run_with_model(binding, SimpleNamespace(supports_skill_policy=True), lambda: None)
    finally:
        POLICY.reset(token)
    token = POLICY.set({"network": "none", "secret_values": {}})
    try:
        with pytest.raises(ValueError, match="network access"):
            run_with_model(binding, SimpleNamespace(supports_skill_policy=True), lambda: None)
    finally:
        POLICY.reset(token)


def test_analysis_setup_includes_dynamic_key_for_full_result_redaction(monkeypatch):
    from aiscientist.agents.mcp_analysis import AnalysisAdapter
    from aiscientist.agents import skills
    monkeypatch.setattr(skills, "get_skill", lambda _: SimpleNamespace(requirements={"secrets": []}))
    server = SimpleNamespace(name="literature", settings_env=(), skill="literature",
                             tools=[{"name": "deep_literature"}])
    executor = SimpleNamespace(mcp_model_binding=lambda: ModelBinding("http://node:1234/v1", "model", "synthetic-secret"))
    prepared = AnalysisAdapter(server).prepare({"question": "PRPF31?"}, executor, tool="deep_literature")
    assert "LITERATURE_MODEL_API_KEY" in prepared.setup
    assert "synthetic-secret" not in prepared.setup + json.dumps(prepared.arguments) + repr(prepared)
    assert prepared.context["model_binding"].binding.api_key == "synthetic-secret"


def test_dataset_optional_description_uses_its_own_prefix():
    binding, _ = prepare_model("data-utilities", "inspect_dataset",
                              SimpleNamespace(mcp_model_binding=lambda: ModelBinding("http://node:1234/v1", "model")), {})
    assert binding.prefix == "DATA_UTILITIES"


def test_diagnosis_binds_the_separate_literature_peer_model():
    values = {"PHENOTYPE_MODEL": "hpo-model"}
    binding, setup = prepare_model(
        "phenotype-diagnosis", "diagnose_disease",
        SimpleNamespace(mcp_model_binding=lambda: ModelBinding("http://node:1234/v1", "session", "test-key")), values)
    assert binding.prefix == "LITERATURE" and binding.secret_name == "LITERATURE_MODEL_API_KEY"
    assert values["LITERATURE_MODEL"] == "session" and values["PHENOTYPE_MODEL"] == "hpo-model"
    assert "test-key" not in setup + json.dumps(values)


def test_diagnosis_corpus_settings_admit_no_executable_override(monkeypatch):
    from aiscientist.agents.mcp_analysis import AnalysisAdapter, setting_paths, validate_settings
    from aiscientist.agents import skills
    monkeypatch.setattr(skills, "get_skill", lambda _: SimpleNamespace(requirements={"secrets": []}))
    monkeypatch.setenv("LITERATURE_CORPUS_ROOT", "/refs/retigene")
    server = SimpleNamespace(name="phenotype-diagnosis", settings_env=("LITERATURE_CORPUS_ROOT",), skill="phenotype",
                             tools=[{"name": "diagnose_disease"}])
    prepared = AnalysisAdapter(server).prepare({"hpo_terms": ["HP:0000662"]}, SimpleNamespace(), tool="diagnose_disease")
    assert "export LITERATURE_CORPUS_ROOT=/refs/retigene" in prepared.setup
    assert 'LITERATURE_ALLOWED_ROOTS="${PHENOTYPE_ALLOWED_ROOTS:-}"' in prepared.setup
    assert "/refs" in setting_paths(server)
    with pytest.raises(ValueError, match="approved"):
        validate_settings("phenotype-diagnosis", ("PHENOTYPE_LITERATURE_COMMAND",))
