"""Trusted native PaperQA controls survive the application/container boundary."""
import json
from pathlib import Path

import pytest
from test_mcp_analysis import analysis_bridge, fixture_result  # noqa: F401

from aiscientist.agents import mcp_analysis

VALUES = {"LITERATURE_TEMPERATURE": "0.15", "LITERATURE_LLM_TIMEOUT": "617", "LITERATURE_AGENT_TIMEOUT": "811"}


@pytest.mark.parametrize("server,operation", [("literature", "literature_search"), ("phenotype-diagnosis", "run_lirical")])
def test_native_controls_reach_image_environment_not_tool_arguments(request, monkeypatch, server, operation):
    runner, ctx, make = request.getfixturevalue("analysis_bridge")
    for name, value in VALUES.items():
        monkeypatch.setenv(name, value)
    tool = make(server, operation, settings=tuple(VALUES))
    fixture_result(runner, server=server)
    result = tool.executor({}, ctx)
    assert result["status"] == "ok", result
    capture = json.loads(runner.capture.read_text())
    assert {name: capture["env"][name] for name in VALUES} == VALUES
    assert json.loads(capture["argv"][-1]) == {}


def test_packaged_catalog_declares_native_controls_for_both_servers():
    path = Path(mcp_analysis.__file__).parent / "mcp_catalog" / "mcp_servers.json"
    servers = json.loads(path.read_text())["servers"]
    for server in servers:
        if server["name"] in {"literature", "phenotype-diagnosis"}:
            assert set(VALUES) <= set(server["settings_env"])
            mcp_analysis.validate_settings(server["name"], server["settings_env"])
