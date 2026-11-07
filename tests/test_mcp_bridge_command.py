"""Run the generated bridge shell command offline, with only the MCP CLI replaced."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from aiscientist.agents import skills_registry
from aiscientist.agents.research_harness import HarnessContext
from test_mcp_bridge import IMAGE, build, write_config, write_skill


class LocalCommandRunner:
    """Image-runner double: execute the complete command against a fixture CLI."""

    supports_skill_policy = True

    def __init__(self, root: Path):
        self.root = root
        self.work = root / "work with spaces"
        self.artifacts = root / "artifacts with spaces"
        self.bin = root / "bin"
        self.capture = root / "argv.json"
        self.fixture = root / "response.json"
        self.exit_code = 0
        self.calls = []
        for directory in (self.work, self.artifacts, self.bin):
            directory.mkdir(parents=True)
        (self.bin / "python").symlink_to(sys.executable)
        cli = self.bin / "aiscientist-mcp"
        cli.write_text('''#!/usr/bin/env python
import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_CAPTURE"]).write_text(json.dumps({
    "argv": sys.argv[1:], "executor": os.environ["AISCI_EXECUTOR"],
    "data_dir": os.environ["AISCI_DATA_DIR"], "result_file": os.environ["AISCI_MCP_RESULT"]}))
sys.stdout.write(Path(os.environ["FAKE_RESPONSE"]).read_text())
code = int(os.environ["FAKE_EXIT"])
if code:
    sys.stderr.write("tool error: rejected fixture\\n")
sys.exit(code)
''')
        cli.chmod(0o755)

    def run_in_image(self, command, image, **kwargs):
        self.calls.append((image, kwargs, skills_registry.POLICY.get()))
        # Use only this test's environment: no login profile, credentials or BASH_ENV.
        env = {
            "PATH": os.pathsep.join((str(self.bin), "/usr/bin", "/bin")),
            "AISCIENTIST_ENV_DIR": str(self.work), "AISCIENTIST_ARTIFACTS": str(self.artifacts),
            "FAKE_CAPTURE": str(self.capture), "FAKE_RESPONSE": str(self.fixture),
            "FAKE_EXIT": str(self.exit_code),
        }
        process = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", command],
                                 cwd=self.work, env=env, capture_output=True, text=True,
                                 check=False, timeout=10)
        return {"status": "ok" if process.returncode == 0 else "error", "returncode": process.returncode,
                "stdout": process.stdout, "stderr": process.stderr,
                "artifacts": [path.relative_to(self.artifacts).as_posix()
                              for path in self.artifacts.rglob("*") if path.is_file()]}


@pytest.fixture
def command_bridge(tmp_path, monkeypatch, skill_release):
    config = write_config(tmp_path)
    monkeypatch.setenv("AISCIENTIST_MCP_CONFIG", str(config))
    source = tmp_path / "skills"
    write_skill(source)
    skill_release(source)
    runner = LocalCommandRunner(tmp_path / "local runner")
    tool, = build(runner)
    return runner, tool, HarnessContext(workspace=tmp_path / "run")


@pytest.mark.parametrize("raw, expected", [
    ({"rows": 7}, {"rows": 7}),
    (["{\"rows\":", "7}"], {"rows": 7}),
    (["ordinary text"], {"content": [{"type": "text", "text": "ordinary text"}]}),
    (["42"], {"content": [{"type": "text", "text": "42"}]}),
    (["true"], {"content": [{"type": "text", "text": "true"}]}),
    (["[1,2]"], {"content": [{"type": "text", "text": "[1,2]"}]}),
    (["null"], {"content": [{"type": "text", "text": "null"}]}),
    (["4", "2"], {"content": [{"type": "text", "text": "4"}, {"type": "text", "text": "2"}]}),
])
def test_generated_command_quotes_arguments_collects_results_and_cleans_up(command_bridge, raw, expected):
    runner, tool, ctx = command_bridge
    runner.fixture.write_text(json.dumps(raw))
    args = {"text": "a'; touch shell-injection; echo ' $(touch shell-injection) "
                    "`touch shell-injection`\n\"quotes\" \\ end", "count": 3}
    result = tool.executor(args, ctx)

    assert result["status"] == "ok", result
    assert result["result"] == expected
    recorded = json.loads(runner.capture.read_text())
    assert recorded["argv"][:6] == ["--server", "table-check-mcp", "--timeout", "90", "call", "check"]
    assert len(recorded["argv"]) == 7
    assert json.loads(recorded["argv"][6]) == args
    assert not (runner.work / "shell-injection").exists()
    assert recorded["executor"] == "inline" and recorded["data_dir"] == str(runner.work)
    assert not Path(recorded["result_file"]).exists()
    assert not list(runner.work.glob(".mcp-result.*"))
    assert json.loads((runner.artifacts / "mcp/table-check/check.json").read_text()) == expected
    assert result["artifacts"] == ["mcp/table-check/check.json"]
    assert "stdout" not in result
    image, resources, policy = runner.calls[0]
    assert image == IMAGE and resources["cpus"] == 2 and policy["network"] == "none"


def test_nonzero_cli_exit_preserves_error_skips_collection_and_cleans_up(command_bridge):
    runner, tool, ctx = command_bridge
    # Valid stdout must still be discarded if the CLI also reports an MCP error.
    runner.fixture.write_text(json.dumps({"rows": 7}))
    runner.exit_code = 19
    result = tool.executor({"text": "fixture"}, ctx)

    assert result["status"] == "error" and result["returncode"] == 19
    assert result["stderr"] == "tool error: rejected fixture\n"
    assert result["stdout"] == ""
    assert result["mcp"]["version"] == "1.2.3"
    assert not result["artifacts"]
    assert not (runner.artifacts / "mcp").exists()
    recorded = json.loads(runner.capture.read_text())
    assert not Path(recorded["result_file"]).exists()
    assert not list(runner.work.glob(".mcp-result.*"))


def test_long_json_array_text_is_saved_verbatim_and_bounded(command_bridge):
    runner, tool, ctx = command_bridge
    text = json.dumps(list(range(10000)))
    runner.fixture.write_text(json.dumps([text]))
    result = tool.executor({"text": "fixture"}, ctx)
    assert result["status"] == "ok" and result["result"]["response_omitted"]
    evidence = runner.artifacts / result["result"]["full_result"]
    assert json.loads(evidence.read_text()) == {"content": [{"type": "text", "text": text}]}
